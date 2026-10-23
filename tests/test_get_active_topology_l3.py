# SPDX-License-Identifier: Apache-2.0
"""Committed L3 topology enrichment (ipv4_address
+ vrf) must naturally appear through the existing get_active_topology()
MCP-facing read -- no new MCP tool, no filtering change needed, since
lab.get_active_topology() already returns the whole validated topology
mapping verbatim. It must still never expose access-info/credentials
(topology structurally cannot carry those fields at all -- see
lab.validate_topology_no_access_fields())."""

from __future__ import annotations

from network_lab_mcp import lab


def test_committed_l3_interfaces_are_visible_via_get_active_topology(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    lab.write_topology(
        "sample_lab",
        {
            "name": "sample_lab",
            "description": "Sample lab used for tests.",
            "devices": {
                "R1": {
                    "type": "iosxr",
                    "interfaces": {"GigabitEthernet0/0/0/2": {"ipv4_address": "10.0.12.1", "vrf": "default"}},
                },
                "PAGENT": {
                    "type": "ios",
                    "interfaces": {"GigabitEthernet0/0.2000": {"ipv4_address": "10.20.0.10", "vrf": "tgn1"}},
                },
            },
            "links": [],
        },
        lab_root,
    )

    active = lab.get_active_topology()

    assert active["active_topology"] == "sample_lab"
    r1_interfaces = active["topology"]["devices"]["R1"]["interfaces"]
    assert r1_interfaces["GigabitEthernet0/0/0/2"] == {"ipv4_address": "10.0.12.1", "vrf": "default"}
    assert active["topology"]["devices"]["PAGENT"]["type"] == "ios"
    assert active["topology"]["devices"]["PAGENT"]["interfaces"]["GigabitEthernet0/0.2000"]["vrf"] == "tgn1"


def test_get_active_topology_never_exposes_access_info_fields(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    lab.write_topology(
        "sample_lab",
        {
            "name": "sample_lab",
            "devices": {
                "R1": {
                    "type": "iosxr",
                    "interfaces": {"Gi0/0/0/2": {"ipv4_address": "10.0.12.1", "vrf": "default"}},
                }
            },
            "links": [],
        },
        lab_root,
    )

    active = lab.get_active_topology()

    serialized = repr(active)
    for forbidden in ("username", "password", "jump_host"):
        assert forbidden not in serialized
    for field_name in lab.TOPOLOGY_ACCESS_FIELDS:
        assert field_name not in active["topology"]["devices"]["R1"]
