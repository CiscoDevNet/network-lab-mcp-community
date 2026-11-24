# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""L3 enrichment is additive, never a Discovery blocker. A genuine L3
*parser* failure for one device (unrecognized output, not just a
missing/absent command) skips only that device's L3 observation -- with a
warning and a Partial status -- while its LLDP/CDP-discovered links and
every other device's L3 enrichment are completely unaffected (L02, L03,
L11)."""

from __future__ import annotations

import pytest

from network_lab_mcp import discovery, lab, terminal


@pytest.fixture(autouse=True)
def _patch_lab_root(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)


@pytest.fixture(autouse=True)
def _track_bootstrap_close(monkeypatch):
    monkeypatch.setattr(terminal, "close_bootstrap_terminal", lambda device_id: None)


def _write_access_info(lab_root, devices):
    lab.write_access_info("sample_lab", {"name": "sample_lab", "devices": devices}, lab_root)


_LLDP_HEADER = "Device ID       Local Intf                      Hold-time  Capability      Port ID"
_L3_HEADER = "Interface                     IP-Address   Status Protocol Vrf-Name"


def test_l3_parser_failure_on_one_device_skips_only_that_devices_l3(lab_root, monkeypatch):
    def fake_collect(device_id, cfg, include_l3=False):
        other = "R2" if device_id == "R1" else "R1"
        return {
            "hostname": f"HOST-{device_id}",
            "show_version": "x",
            "show_running_config": "x",
            "show_lldp_neighbors": (
                f"{_LLDP_HEADER}\nHOST-{other} Gi0/0/0/2 120 R Gi0/0/0/2\nTotal entries displayed: 1\n"
            ),
            "show_cdp_neighbors": "% CDP is not enabled\n",
            # R1's L3 command returns genuinely unrecognized output (not
            # merely absent) -- a real parser failure, not "unavailable".
            "show_ipv4_interface_brief": "% Invalid input detected at '^' marker.\n"
            if device_id == "R1"
            else f"{_L3_HEADER}\nGigabitEthernet0/0/0/2   10.0.12.2   Up Up default\n",
        }

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_collect)
    _write_access_info(
        lab_root,
        {
            "R1": {"type": "iosxr", "address": "192.0.2.11", "transport": "ssh"},
            "R2": {"type": "iosxr", "address": "192.0.2.12", "transport": "ssh"},
        },
    )

    # An explicit `discover topology l3` drops only R1's unusable L3
    # observation (warning, Partial); R2's L3 and the discovered link stay.
    l3 = discovery.discover_topology(lab_root, include_l3=True)
    assert "interfaces" not in l3.devices["R1"] and "interfaces" in l3.devices["R2"]
    assert len(l3.managed_links) == 1
    assert l3.l3_enriched_device_count == 1
    assert len(l3.warnings) == 1 and l3.warnings[0].startswith("R1: L3 interface observations were not used")
    statuses = {o.device_id: (o.l3_state, o.status) for o in l3.outcomes}
    assert statuses == {"R1": ("failed", "Partial"), "R2": ("ok", "Complete")}

    # ... while the default L2-only run never looks at L3 output at all:
    # the same collected data yields the link and no interface data.
    result = discovery.discover_topology(lab_root)
    assert len(result.managed_links) == 1
    assert "interfaces" not in result.devices["R1"] and "interfaces" not in result.devices["R2"]
    assert result.l3_enriched_device_count == 0 and not result.include_l3


def test_l3_empty_output_is_a_warning_in_l3_mode_and_ignored_in_l2_mode(lab_root, monkeypatch):
    """An empty/unrecognized L3 output (no table header) is a parser
    failure: a Partial-status warning for an explicit L3 request, never even
    read in L2."""

    def fake_collect(device_id, cfg, include_l3=False):
        return {
            "hostname": "HOST-R1",
            "show_version": "x",
            "show_running_config": "x",
            "show_lldp_neighbors": f"{_LLDP_HEADER}\nTotal entries displayed: 0\n",
            "show_cdp_neighbors": "% CDP is not enabled\n",
            "show_ipv4_interface_brief": "",
        }

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_collect)
    _write_access_info(lab_root, {"R1": {"type": "iosxr", "address": "192.0.2.11", "transport": "ssh"}})

    l3 = discovery.discover_topology(lab_root, include_l3=True)
    assert "interfaces" not in l3.devices["R1"] and l3.l3_enriched_device_count == 0
    assert l3.outcomes[0].status == "Partial" and "L3 interface observations were not used" in l3.warnings[0]

    result = discovery.discover_topology(lab_root)
    assert result.connected_count == 1  # the device itself succeeded
    assert "interfaces" not in result.devices["R1"]
    assert result.warnings == [] and result.outcomes[0].status == "Complete"
