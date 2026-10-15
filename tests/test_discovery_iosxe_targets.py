"""IOS XE target selection and end-to-end mixed IOS XR + IOS XE
`discover_topology()` behavior (device collection itself is monkeypatched,
same convention as tests/test_discovery_parallel_collection.py and
tests/test_discovery_workflow.py -- never a real network/tmux operation
here). Covers: `_select_iosxe_targets()` in isolation, an IOS XE-only
definition, a mixed IOS XR + IOS XE definition (the PAGENT scenario: R1/R2
are IOS XR, PAGENT is IOS XE, CDP-only), and that `nxos`/`host` devices are
still silently skipped exactly as before."""

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


def test_select_iosxe_targets_filters_by_normalized_type():
    access_data = {
        "devices": {
            "PAGENT": {"type": "iosxe", "address": "192.0.2.30", "transport": "telnet"},
            "R1": {"type": "iosxr", "address": "192.0.2.11", "transport": "ssh"},
            "SRV1": {"type": "host", "address": "192.0.2.40"},
        }
    }
    targets = discovery._select_iosxe_targets(access_data)
    assert set(targets) == {"PAGENT"}
    assert targets["PAGENT"]["transport"] == "telnet"


def test_select_iosxe_targets_empty_when_none_present():
    access_data = {"devices": {"R1": {"type": "iosxr", "address": "192.0.2.11"}}}
    assert discovery._select_iosxe_targets(access_data) == {}


def test_nxos_and_host_devices_remain_silently_skipped_for_both_selectors():
    access_data = {
        "devices": {
            "N1": {"type": "nxos", "address": "192.0.2.50"},
            "H1": {"type": "host", "address": "192.0.2.60"},
        }
    }
    assert discovery._select_iosxr_targets(access_data) == {}
    assert discovery._select_iosxe_targets(access_data) == {}


def _fake_iosxr_collect(device_id, cfg):
    return {
        "hostname": f"HOST-{device_id}",
        "show_version": "x",
        "show_running_config": "x",
        "show_lldp_neighbors": "Device ID       Local Intf                      Hold-time  Capability      Port ID\nTotal entries displayed: 0\n",
        "show_cdp_neighbors": "% CDP is not enabled\n",
    }


def _fake_iosxe_collect_pagent(device_id, cfg):
    cdp_header = "Device ID        Local Intrfce     Holdtme    Capability  Platform    Port ID"
    row_r1 = f"{'HOST-R1':<17}{'Gig0/0':<18}{'137':<11}{'R':<12}{'ASR9K Ser':<12}Gi0/0/0/10"
    row_r2 = f"{'HOST-R2':<17}{'Gig0/1':<18}{'137':<11}{'R':<12}{'ASR9K Ser':<12}Gi0/0/0/10"
    return {
        "hostname": "PAGENT",
        "show_version": "x",
        # IOS XE also collects LLDP -- disabled here (the
        # real, documented Cisco text), which must mean zero LLDP
        # observations, never a device/collection failure.
        "show_lldp_neighbors": "% LLDP is not enabled\n",
        "show_cdp_neighbors": f"{cdp_header}\n{row_r1}\n{row_r2}\n",
    }


def test_iosxe_only_definition_discovers_via_cdp(lab_root, monkeypatch):
    _write_access_info(lab_root, {"PAGENT": {"type": "iosxe", "address": "192.0.2.30", "transport": "telnet"}})
    monkeypatch.setattr(discovery, "_bootstrap_collect_iosxe", _fake_iosxe_collect_pagent)

    result = discovery.discover_topology(lab_root)

    assert result.iosxr_target_count == 0
    assert result.iosxe_target_count == 1
    assert result.connected_count == 1
    assert result.devices == {"PAGENT": {"type": "iosxe"}}
    # PAGENT's own CDP neighbors (HOST-R1/-R2) are not managed devices
    # here (only PAGENT itself is in this definition), so they stay
    # unresolved -- never auto-created as topology devices.
    assert result.managed_links == []
    assert {u.remote_device_id_raw for u in result.unresolved} == {"HOST-R1", "HOST-R2"}


def test_mixed_iosxr_and_iosxe_definition_produces_expected_managed_links(lab_root, monkeypatch):
    """The PAGENT scenario: R1 and R2 are IOS XR (LLDP+CDP-capable, but
    only reachable here via CDP in this fixture), PAGENT is IOS XE
    (CDP-only). R1 Gi0/0/0/10 <-> PAGENT Gig0/0 and R2 Gi0/0/0/10 <->
    PAGENT Gig0/1 should both resolve as candidate links; an unmanaged
    neighbor stays unresolved and is never auto-created as a device."""

    def fake_iosxr_collect(device_id, cfg):
        cdp_header = "Device ID        Local Intrfce     Holdtme    Capability  Platform    Port ID"
        if device_id == "R1":
            port = "Gig0/0"
        else:
            port = "Gig0/1"
        row = f"{'PAGENT':<17}{'Gi0/0/0/10':<18}{'144':<11}{'R':<12}{'Cisco 720':<12}{port}"
        unmanaged = "labsw-a07.example.com"
        cont = f"{'':<17}{'Fas 0/0':<18}{'159':<11}{'S I':<12}{'WS-C2960X':<12}Gig 1/0/16"
        return {
            "hostname": f"HOST-{device_id}",
            "show_version": "x",
            "show_running_config": "x",
            "show_lldp_neighbors": (
                "Device ID       Local Intf                      Hold-time  Capability      Port ID\n"
                "Total entries displayed: 0\n"
            ),
            "show_cdp_neighbors": f"{cdp_header}\n{row}\n{unmanaged}\n{cont}\n",
        }

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_iosxr_collect)
    monkeypatch.setattr(discovery, "_bootstrap_collect_iosxe", _fake_iosxe_collect_pagent)

    _write_access_info(
        lab_root,
        {
            "R1": {"type": "iosxr", "address": "192.0.2.11", "transport": "ssh"},
            "R2": {"type": "iosxr", "address": "192.0.2.12", "transport": "ssh"},
            "PAGENT": {"type": "iosxe", "address": "192.0.2.30", "transport": "telnet"},
        },
    )

    result = discovery.discover_topology(lab_root)

    assert result.iosxr_target_count == 2
    assert result.iosxe_target_count == 1
    assert result.connected_count == 3
    assert result.conflicts == []

    link_pairs = {
        tuple(sorted(((link.a_device, link.a_interface), (link.b_device, link.b_interface))))
        for link in result.managed_links
    }
    assert (("PAGENT", "Gig0/0"), ("R1", "Gi0/0/0/10")) in link_pairs
    assert (("PAGENT", "Gig0/1"), ("R2", "Gi0/0/0/10")) in link_pairs

    unresolved_ids = {u.remote_device_id_raw for u in result.unresolved}
    assert "labsw-a07.example.com" in unresolved_ids
    assert "labsw-a07.example.com" not in result.devices

    assert result.devices["PAGENT"]["type"] == "iosxe"
    assert result.devices["R1"]["type"] == "iosxr"


def test_only_iosxe_collector_is_invoked_for_iosxe_targets(lab_root, monkeypatch):
    """An IOS XE target is never routed through the
    IOS XR collector (_bootstrap_collect), and vice versa."""
    iosxr_calls = []
    iosxe_calls = []

    def fake_iosxr(device_id, cfg):
        iosxr_calls.append(device_id)
        return _fake_iosxr_collect(device_id, cfg)

    def fake_iosxe(device_id, cfg):
        iosxe_calls.append(device_id)
        return _fake_iosxe_collect_pagent(device_id, cfg)

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_iosxr)
    monkeypatch.setattr(discovery, "_bootstrap_collect_iosxe", fake_iosxe)

    _write_access_info(
        lab_root,
        {
            "R1": {"type": "iosxr", "address": "192.0.2.11", "transport": "ssh"},
            "PAGENT": {"type": "iosxe", "address": "192.0.2.30", "transport": "telnet"},
        },
    )

    discovery.discover_topology(lab_root)

    assert iosxr_calls == ["R1"]
    assert iosxe_calls == ["PAGENT"]
