# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""build_discovery_candidate()'s additive, non-destructive merge semantics:
Discovery adds and updates, and never deletes. An interface or link not
re-observed is kept, an interface observed as `None` (unassigned, or excluded
as a management address) never blanks stored data, a run without L3 data
(L2-only) keeps stored L3 data, devices missing from the run are kept, and a
contradiction with the stored topology is refused instead of overwritten.
Also covers topology_changes()' counts (acceptance M01-M16, L05-L08)."""

from __future__ import annotations

import copy

import pytest

from network_lab_mcp.discovery import (
    DiscoveryError,
    DiscoveryResult,
    ManagedLink,
    build_discovery_candidate,
    topology_changes,
)


def _result(devices, managed_links=None):
    return DiscoveryResult(
        access_info_name="sample_lab",
        default_topology_name="sample_lab",
        iosxr_target_count=len(devices),
        connected_count=len(devices),
        observation_count=0,
        devices=devices,
        managed_links=managed_links or [],
    )


def _iface(ip, vrf="default"):
    return {"ipv4_address": ip, "vrf": vrf}


def test_new_device_interfaces_are_added():  # L05
    result = _result({"R1": {"type": "iosxr", "interfaces": {"Gi0/0": _iface("10.0.0.1")}}})
    devices = build_discovery_candidate(result, {})["devices"]
    assert devices["R1"]["interfaces"] == {"Gi0/0": _iface("10.0.0.1")}


def test_reobserved_interface_value_is_updated_and_unobserved_interface_is_retained():  # L06, L07
    existing = {
        "devices": {
            "R1": {
                "type": "iosxr",
                "interfaces": {"Gi0/0": _iface("10.0.0.1"), "Gi0/1": _iface("10.0.0.5")},
            }
        }
    }
    result = _result({"R1": {"type": "iosxr", "interfaces": {"Gi0/0": _iface("10.0.0.9", "VRF_A")}}})
    devices = build_discovery_candidate(result, existing)["devices"]
    assert devices["R1"]["interfaces"] == {"Gi0/0": _iface("10.0.0.9", "VRF_A"), "Gi0/1": _iface("10.0.0.5")}


def test_extra_stored_fields_of_an_interface_survive_an_update():
    existing = {"devices": {"R1": {"interfaces": {"Gi0/0": {**_iface("10.0.0.1"), "note": "uplink"}}}}}
    result = _result({"R1": {"type": "iosxr", "interfaces": {"Gi0/0": _iface("10.0.0.2")}}})
    interfaces = build_discovery_candidate(result, existing)["devices"]["R1"]["interfaces"]
    assert interfaces == {"Gi0/0": {**_iface("10.0.0.2"), "note": "uplink"}}


def test_interface_spelling_variants_update_the_stored_entry_instead_of_duplicating():
    existing = {"devices": {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": _iface("10.0.0.1")}}}}
    result = _result({"R1": {"type": "iosxr", "interfaces": {"GigabitEthernet0/0": _iface("10.0.0.2")}}})
    interfaces = build_discovery_candidate(result, existing)["devices"]["R1"]["interfaces"]
    assert interfaces == {"Gi0/0": _iface("10.0.0.2")}


def test_device_with_no_l3_result_keeps_prior_interfaces():  # L04
    existing = {"devices": {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": _iface("10.0.0.1")}}}}
    result = _result({"R1": {"type": "iosxr"}})
    devices = build_discovery_candidate(result, existing)["devices"]
    assert devices["R1"] == {"type": "iosxr", "interfaces": {"Gi0/0": _iface("10.0.0.1")}}


def test_none_sentinel_never_blanks_an_existing_value():  # L08, L09
    existing = {"devices": {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": _iface("10.0.0.1")}}}}
    result = _result({"R1": {"type": "iosxr", "interfaces": {"Gi0/0": None, "Gi0/1": _iface("10.0.0.5")}}})
    interfaces = build_discovery_candidate(result, existing)["devices"]["R1"]["interfaces"]
    assert interfaces == {"Gi0/0": _iface("10.0.0.1"), "Gi0/1": _iface("10.0.0.5")}


def test_none_sentinel_is_absent_for_a_new_device_and_others_kept():
    result = _result(
        {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": None, "Gi0/1": _iface("10.0.0.5")}}}
    )
    devices = build_discovery_candidate(result, {})["devices"]
    assert devices["R1"]["interfaces"] == {"Gi0/1": _iface("10.0.0.5")}


def test_none_sentinel_for_interface_never_present_yields_empty_interfaces():
    result = _result({"R1": {"type": "iosxr", "interfaces": {"Gi0/9": None}}})
    devices = build_discovery_candidate(result, {})["devices"]
    assert devices["R1"].get("interfaces") == {}


def test_metadata_extra_fields_unmanaged_devices_and_their_links_survive():  # M10, M14
    existing = {
        "name": "sample_lab",
        "description": "hand-authored",
        "x_custom": {"k": 1},
        "devices": {"R1": {"type": "iosxr", "role": "pe"}, "PC1": {"type": "host"}},
        "links": [{"a": "R1", "b": "PC1", "note": "lab cable"}],
    }
    before = copy.deepcopy(existing)
    candidate = build_discovery_candidate(_result({"R1": {"type": "iosxr"}}), existing)
    assert candidate["description"] == "hand-authored"
    assert candidate["x_custom"] == {"k": 1}
    assert candidate["devices"] == {"R1": {"type": "iosxr", "role": "pe"}, "PC1": {"type": "host"}}
    assert candidate["links"] == [{"a": "R1", "b": "PC1", "note": "lab cable"}]
    assert existing == before  # base is never mutated


def test_new_links_are_appended_canonical_sorted_after_existing_ones():  # M04
    result = _result(
        {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}, "R3": {"type": "iosxr"}},
        managed_links=[ManagedLink("R3", "Gi0/2", "R1", "Gi0/3"), ManagedLink("R2", "Gi0/1", "R1", "Gi0/1")],
    )
    existing = {"devices": {"R9": {}, "R8": {}}, "links": [{"a": "R9", "b": "R8"}]}
    candidate = build_discovery_candidate(result, existing)
    assert candidate["links"] == [
        {"a": "R9", "b": "R8"},
        {"a": "R1", "a_interface": "Gi0/1", "b": "R2", "b_interface": "Gi0/1"},
        {"a": "R1", "a_interface": "Gi0/3", "b": "R3", "b_interface": "Gi0/2"},
    ]
    assert list(candidate["devices"]) == ["R9", "R8", "R1", "R2", "R3"]  # stored order, then new sorted


def test_known_link_is_never_duplicated_in_any_orientation_or_spelling():  # M05, M12
    existing = {
        "devices": {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}},
        "links": [
            {"a": "R2", "a_interface": "Gi0/0/0/1", "b": "R1", "b_interface": "GigabitEthernet0/0/0/1", "note": "x"}
        ],
    }
    result = _result(
        {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}},
        managed_links=[ManagedLink("R1", "Gi0/0/0/1", "R2", "GigabitEthernet0/0/0/1")],
    )
    assert build_discovery_candidate(result, existing)["links"] == existing["links"]


def test_rediscovering_the_same_result_is_idempotent():  # M12
    result = _result(
        {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": _iface("10.0.0.1")}}, "R2": {}},
        managed_links=[ManagedLink("R1", "Gi0/1", "R2", "Gi0/1")],
    )
    first = build_discovery_candidate(result, {})
    second = build_discovery_candidate(result, first)
    assert second == first
    assert not topology_changes(first, second).changed


def test_link_claiming_an_interface_of_a_different_stored_link_is_refused():  # M13
    existing = {
        "devices": {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}, "R3": {"type": "iosxr"}},
        "links": [{"a": "R1", "a_interface": "Gi0/0/0/0", "b": "R2", "b_interface": "Gi0/0/0/0"}],
    }
    result = _result(
        {d: {"type": "iosxr"} for d in ("R1", "R2", "R3")},
        managed_links=[ManagedLink("R1", "GigabitEthernet0/0/0/0", "R3", "Gi0/0/0/0")],
    )
    before = copy.deepcopy(existing)
    with pytest.raises(DiscoveryError, match=r"contradicts the existing topology.*R1 Gi.*conflicts with existing link"):
        build_discovery_candidate(result, existing)
    assert existing == before


def test_stored_link_without_interfaces_cannot_conflict():
    existing = {"devices": {"R1": {}, "R2": {}, "R3": {}}, "links": [{"a": "R1", "b": "R2"}]}
    result = _result(
        {d: {"type": "iosxr"} for d in ("R1", "R3")}, managed_links=[ManagedLink("R1", "Gi0/0", "R3", "Gi0/0")]
    )
    assert len(build_discovery_candidate(result, existing)["links"]) == 2


def test_conflicting_device_type_is_refused_not_overwritten():  # M15
    existing = {"devices": {"R1": {"type": "iosxe"}}}
    with pytest.raises(DiscoveryError, match=r"Device 'R1': access-info type 'iosxr' conflicts"):
        build_discovery_candidate(_result({"R1": {"type": "iosxr"}}), existing)
    # An abbreviated spelling of the same type is not a conflict.
    same = build_discovery_candidate(_result({"R1": {"type": "iosxr"}}), {"devices": {"R1": {"type": "IOSXR"}}})
    assert same["devices"]["R1"]["type"] == "IOSXR"


def test_missing_type_in_the_result_never_removes_a_stored_type_and_a_missing_stored_type_is_filled():  # M16
    existing = {"devices": {"R1": {"type": "iosxr"}, "R2": {}}}
    result = _result({"R1": {}, "R2": {"type": "ios"}, "R3": {}})
    devices = build_discovery_candidate(result, existing)["devices"]
    assert devices == {"R1": {"type": "iosxr"}, "R2": {"type": "ios"}, "R3": {}}


def test_topology_changes_counts_added_updated_retained_and_l3():  # C11
    before = {
        "devices": {
            "R1": {"type": "iosxr", "interfaces": {"Gi0/0": _iface("10.0.0.1"), "Gi0/1": _iface("10.0.0.5")}},
            "R2": {"type": "iosxr"},
            "R9": {"type": "host"},
        },
        "links": [{"a": "R1", "a_interface": "Gi0/5", "b": "R9", "b_interface": "e0"}],
    }
    result = _result(
        {
            "R1": {"type": "iosxr", "interfaces": {"Gi0/0": _iface("10.0.0.2"), "Gi0/2": _iface("10.0.0.9")}},
            "R2": {"type": "iosxr"},
            "R3": {},
        },
        managed_links=[ManagedLink("R1", "Gi0/1", "R2", "Gi0/1")],
    )
    after = build_discovery_candidate(result, before)
    changes = topology_changes(before, after)
    assert (changes.devices_added, changes.devices_updated, changes.devices_retained) == (1, 1, 2)
    assert (changes.links_added, changes.links_retained) == (1, 1)
    assert (changes.interfaces_added, changes.interfaces_updated, changes.interfaces_retained) == (1, 1, 1)
    assert changes.changed
