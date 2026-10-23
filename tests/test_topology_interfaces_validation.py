# SPDX-License-Identifier: Apache-2.0
"""Topology `interfaces` validation (lab.validate_topology_interfaces(),
called from lab.validate_topology_data()) -- L3 enrichment: a stable,
directly observed IPv4 address + VRF per interface. Like `links`,
`interfaces` has no structured CLI editing command; it is populated by
`discover topology`, the external `edit`, or by hand-editing the committed
YAML, so validation is the only guard against malformed data reaching disk
or the renderer."""

from __future__ import annotations

from network_lab_mcp import lab


def _topology(interfaces=None, devices=None):
    devices = devices if devices is not None else {"R1": {"type": "iosxr"}}
    if interfaces is not None:
        devices = {name: {**cfg, "interfaces": interfaces} for name, cfg in devices.items()}
    return {"name": "t", "devices": devices}


def test_interfaces_omitted_is_valid():
    data = {"name": "t", "devices": {"R1": {"type": "iosxr"}}}
    lab.validate_topology_data("t", data)


def test_empty_interfaces_mapping_is_valid():
    lab.validate_topology_data("t", _topology(interfaces={}))


def test_valid_interface_entry_passes():
    lab.validate_topology_data(
        "t", _topology(interfaces={"GigabitEthernet0/0/0/2": {"ipv4_address": "10.0.12.1", "vrf": "default"}})
    )


def test_interfaces_not_a_mapping_rejected():
    data = {"name": "t", "devices": {"R1": {"type": "iosxr", "interfaces": ["not", "a", "mapping"]}}}
    try:
        lab.validate_topology_data("t", data)
        assert False, "expected LabConfigError"
    except lab.LabConfigError as exc:
        assert "interfaces" in str(exc)


def test_interface_entry_not_a_mapping_rejected():
    data = {"name": "t", "devices": {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": "not-a-mapping"}}}}
    try:
        lab.validate_topology_data("t", data)
        assert False, "expected LabConfigError"
    except lab.LabConfigError as exc:
        assert "must be a mapping" in str(exc)


def test_interface_missing_ipv4_address_rejected():
    data = {"name": "t", "devices": {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"vrf": "default"}}}}}
    try:
        lab.validate_topology_data("t", data)
        assert False, "expected LabConfigError"
    except lab.LabConfigError as exc:
        assert "ipv4_address" in str(exc)


def test_interface_missing_vrf_rejected():
    data = {
        "name": "t",
        "devices": {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.12.1"}}}},
    }
    try:
        lab.validate_topology_data("t", data)
        assert False, "expected LabConfigError"
    except lab.LabConfigError as exc:
        assert "vrf" in str(exc)


def test_interface_invalid_ipv4_address_rejected():
    data = {
        "name": "t",
        "devices": {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "not-an-ip", "vrf": "default"}}}},
    }
    try:
        lab.validate_topology_data("t", data)
        assert False, "expected LabConfigError"
    except lab.LabConfigError as exc:
        assert "ipv4_address" in str(exc)


def test_interface_rejects_unsupported_extra_field():
    """The schema deliberately keeps to exactly ipv4_address/vrf -- no
    prefix length, no operational state, no address-family framework."""
    data = {
        "name": "t",
        "devices": {
            "R1": {
                "type": "iosxr",
                "interfaces": {
                    "Gi0/0": {"ipv4_address": "10.0.12.1", "vrf": "default", "ipv4_prefix_length": 30}
                },
            }
        },
    }
    try:
        lab.validate_topology_data("t", data)
        assert False, "expected LabConfigError"
    except lab.LabConfigError as exc:
        assert "ipv4_prefix_length" in str(exc)


def test_interfaces_survive_write_and_load_round_trip(lab_root):
    data = {
        "name": "with_l3",
        "devices": {
            "R1": {
                "type": "iosxr",
                "interfaces": {
                    "GigabitEthernet0/0/0/2": {"ipv4_address": "10.0.12.1", "vrf": "default"},
                    "BVI2000": {"ipv4_address": "10.20.0.253", "vrf": "VRF_ELAN_2000"},
                },
            },
            "PAGENT": {
                "type": "ios",
                "interfaces": {
                    "GigabitEthernet0/0.2000": {"ipv4_address": "10.20.0.10", "vrf": "tgn1"},
                },
            },
        },
        "links": [],
    }
    lab.write_topology("with_l3", data, lab_root)
    reloaded = lab.load_topology("with_l3", lab_root)

    assert reloaded["devices"]["R1"]["interfaces"]["GigabitEthernet0/0/0/2"] == {
        "ipv4_address": "10.0.12.1",
        "vrf": "default",
    }
    assert reloaded["devices"]["R1"]["interfaces"]["BVI2000"]["vrf"] == "VRF_ELAN_2000"
    assert reloaded["devices"]["PAGENT"]["type"] == "ios"
    assert reloaded["devices"]["PAGENT"]["interfaces"]["GigabitEthernet0/0.2000"]["ipv4_address"] == "10.20.0.10"


def test_existing_topology_without_interfaces_key_remains_valid_no_migration(lab_root):
    """Backward compatibility: a topology written before L3 interface
    enrichment existed -- devices/links only, no 'interfaces' key anywhere
    -- must remain valid with zero migration."""
    data = {
        "name": "legacy",
        "devices": {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}},
        "links": [{"a": "R1", "b": "R2"}],
    }
    lab.write_topology("legacy", data, lab_root)
    reloaded = lab.load_topology("legacy", lab_root)
    assert "interfaces" not in reloaded["devices"]["R1"]
