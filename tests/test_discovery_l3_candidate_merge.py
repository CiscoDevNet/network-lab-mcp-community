"""build_discovery_candidate()'s rebuild semantics (replaces the former
per-interface merge, which no longer exists): the candidate is built from
this run's DiscoveryResult alone, so an interface not re-observed is NOT
preserved, an interface explicitly observed as removed (unassigned, or
excluded as a management address, a `None` sentinel) is absent, and a run
without L3 data (L2-only) carries none over -- while name, description,
unknown top-level metadata and unrelated extra device fields survive."""

from __future__ import annotations

from network_lab_mcp.discovery import DiscoveryResult, ManagedLink, build_discovery_candidate


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


def test_new_device_interfaces_are_added():
    result = _result({"R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"}}}})
    devices = build_discovery_candidate(result, {})["devices"]
    assert devices["R1"]["interfaces"] == {"Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"}}


def test_reobserved_interface_is_replaced_and_unobserved_interface_is_not_preserved():
    existing = {
        "devices": {
            "R1": {
                "type": "iosxr",
                "interfaces": {
                    "Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"},
                    "Gi0/1": {"ipv4_address": "10.0.0.5", "vrf": "default"},
                },
            }
        }
    }
    result = _result({"R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.0.9", "vrf": "VRF_A"}}}})
    devices = build_discovery_candidate(result, existing)["devices"]
    assert devices["R1"]["interfaces"] == {"Gi0/0": {"ipv4_address": "10.0.0.9", "vrf": "VRF_A"}}


def test_device_with_no_l3_result_carries_no_prior_interfaces():
    """L2-only run (no 'interfaces' key in the DiscoveryResult fields):
    previous L3 data is never carried over."""
    existing = {
        "devices": {
            "R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"}}}
        }
    }
    result = _result({"R1": {"type": "iosxr"}})
    devices = build_discovery_candidate(result, existing)["devices"]
    assert devices["R1"] == {"type": "iosxr"}


def test_none_sentinel_interface_is_absent_and_others_kept():
    result = _result(
        {
            "R1": {
                "type": "iosxr",
                "interfaces": {"Gi0/0": None, "Gi0/1": {"ipv4_address": "10.0.0.5", "vrf": "default"}},
            }
        }
    )
    devices = build_discovery_candidate(result, {})["devices"]
    assert devices["R1"]["interfaces"] == {"Gi0/1": {"ipv4_address": "10.0.0.5", "vrf": "default"}}


def test_none_sentinel_for_interface_never_present_yields_empty_interfaces():
    result = _result({"R1": {"type": "iosxr", "interfaces": {"Gi0/9": None}}})
    devices = build_discovery_candidate(result, {})["devices"]
    assert devices["R1"].get("interfaces") == {}


def test_unrelated_device_fields_and_toplevel_metadata_survive_but_unmanaged_devices_do_not():
    existing = {
        "name": "sample_lab",
        "description": "hand-authored",
        "x_custom": {"k": 1},
        "devices": {"R1": {"type": "iosxr", "role": "pe"}, "PC1": {"type": "host"}},
        "links": [{"a": "R1", "b": "PC1"}],
    }
    candidate = build_discovery_candidate(_result({"R1": {"type": "iosxr"}}), existing)
    assert candidate["description"] == "hand-authored"
    assert candidate["x_custom"] == {"k": 1}
    assert candidate["devices"] == {"R1": {"type": "iosxr", "role": "pe"}}
    assert candidate["links"] == []
    assert "PC1" in existing["devices"]  # base is never mutated


def test_links_are_rebuilt_canonical_sorted_and_deterministic():
    result = _result(
        {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}, "R3": {"type": "iosxr"}},
        managed_links=[ManagedLink("R3", "Gi0/2", "R1", "Gi0/3"), ManagedLink("R2", "Gi0/1", "R1", "Gi0/1")],
    )
    existing = {"devices": {}, "links": [{"a": "R9", "b": "R8"}]}
    candidate = build_discovery_candidate(result, existing)
    assert candidate["links"] == [
        {"a": "R1", "a_interface": "Gi0/1", "b": "R2", "b_interface": "Gi0/1"},
        {"a": "R1", "a_interface": "Gi0/3", "b": "R3", "b_interface": "Gi0/2"},
    ]
    assert list(candidate["devices"]) == ["R1", "R2", "R3"]
