"""Classic IOS (`type: ios`) target selection and end-to-end
`discover topology` participation via CDP -- mirrors
tests/test_discovery_iosxe_targets.py's conventions, but for the
`ios` type. `_bootstrap_collect`/`_bootstrap_collect_iosxe`/
_bootstrap_collect_ios are all monkeypatched here (never a real
network/tmux operation), matching the project's established Discovery
test convention."""

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


def test_select_ios_targets_filters_by_normalized_type():
    access_data = {
        "devices": {
            "PAGENT": {"type": "ios", "address": "192.0.2.30", "transport": "telnet"},
            "SW1": {"type": "iosxe", "address": "192.0.2.31"},
            "R1": {"type": "iosxr", "address": "192.0.2.11"},
        }
    }
    targets = discovery._select_ios_targets(access_data)
    assert set(targets) == {"PAGENT"}
    assert targets["PAGENT"]["transport"] == "telnet"


def test_select_ios_targets_empty_when_none_present():
    access_data = {"devices": {"R1": {"type": "iosxr", "address": "192.0.2.11"}}}
    assert discovery._select_ios_targets(access_data) == {}


_CDP_HEADER = "Device ID        Local Intrfce     Holdtme    Capability  Platform    Port ID"
_IP_BRIEF_HEADER = "Interface              IP-Address      OK? Method Status                Protocol"
_VRF_HEADER = "  Name                             Default RD            Protocols   Interfaces"


def _fake_ios_collect_pagent(device_id, cfg, include_l3=False):
    row_r1 = f"{'ASR9001-R1':<17}{'Gig0/0':<18}{'137':<11}{'R':<12}{'ASR9K Ser':<12}Gi0/0/0/10"
    return {
        "hostname": "PAGENT",
        "show_version": "Cisco IOS Software, 7200 Software",
        "show_cdp_neighbors": f"{_CDP_HEADER}\n{row_r1}\n",
        "show_vrf": "\n".join(
            [
                _VRF_HEADER,
                "  mgmt                             <not set>              ipv4        Fa0/0",
                "  tgn1                             <not set>              ipv4        Gi0/0.2000",
                "",
            ]
        ),
        "show_ip_interface_brief": "\n".join(
            [
                _IP_BRIEF_HEADER,
                "FastEthernet0/0         192.0.2.30      YES NVRAM up up",
                "GigabitEthernet0/0.2000 10.20.0.10      YES NVRAM up up",
                "",
            ]
        ),
    }


def test_ios_only_definition_discovers_via_cdp(lab_root, monkeypatch):
    _write_access_info(lab_root, {"PAGENT": {"type": "ios", "address": "192.0.2.30", "transport": "telnet"}})
    monkeypatch.setattr(discovery, "_bootstrap_collect_ios", _fake_ios_collect_pagent)

    result = discovery.discover_topology(lab_root)

    assert result.ios_target_count == 1
    assert result.iosxr_target_count == 0
    assert result.iosxe_target_count == 0
    assert result.connected_count == 1
    assert result.devices["PAGENT"]["type"] == "ios"
    # PAGENT's own CDP neighbor (ASR9001-R1) is not a managed device in
    # this definition -- stays unresolved, never auto-created.
    assert result.managed_links == []
    assert {u.remote_device_id_raw for u in result.unresolved} == {"ASR9001-R1"}


def test_ios_l3_enrichment_excludes_management_address_and_keeps_data_plane(lab_root, monkeypatch):
    _write_access_info(lab_root, {"PAGENT": {"type": "ios", "address": "192.0.2.30", "transport": "telnet"}})
    monkeypatch.setattr(discovery, "_bootstrap_collect_ios", _fake_ios_collect_pagent)

    result = discovery.discover_topology(lab_root, include_l3=True)

    interfaces = result.devices["PAGENT"]["interfaces"]
    # FastEthernet0/0's address matches the access-info connection address
    # for PAGENT -- must never be copied into topology L3 data. At the raw
    # DiscoveryResult level this is an explicit `None` removal signal (see
    # _build_device_interface_fields()); build_discovery_candidate()
    # is what actually drops it from the merged candidate (covered by
    # tests/test_discovery_l3_candidate_merge.py).
    assert interfaces["FastEthernet0/0"] is None
    assert interfaces["GigabitEthernet0/0.2000"] == {"ipv4_address": "10.20.0.10", "vrf": "tgn1"}
    assert result.l3_enriched_device_count == 1
    assert result.l3_interface_count == 1

    devices = discovery.build_discovery_candidate(result, {})["devices"]
    assert "FastEthernet0/0" not in devices["PAGENT"]["interfaces"]
    assert devices["PAGENT"]["interfaces"]["GigabitEthernet0/0.2000"]["ipv4_address"] == "10.20.0.10"


def test_only_ios_collector_is_invoked_for_ios_targets(lab_root, monkeypatch):
    calls = []

    def fake_ios(device_id, cfg):
        calls.append(("ios", device_id))
        return _fake_ios_collect_pagent(device_id, cfg)

    def fake_iosxr(device_id, cfg):
        calls.append(("iosxr", device_id))
        return {
            "hostname": f"HOST-{device_id}",
            "show_version": "x",
            "show_running_config": "x",
            "show_lldp_neighbors": "Device ID       Local Intf                      Hold-time  Capability      Port ID\nTotal entries displayed: 0\n",
            "show_cdp_neighbors": "% CDP is not enabled\n",
            "show_ipv4_interface_brief": "",
        }

    monkeypatch.setattr(discovery, "_bootstrap_collect_ios", fake_ios)
    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_iosxr)

    _write_access_info(
        lab_root,
        {
            "R1": {"type": "iosxr", "address": "192.0.2.11", "transport": "ssh"},
            "PAGENT": {"type": "ios", "address": "192.0.2.30", "transport": "telnet"},
        },
    )

    discovery.discover_topology(lab_root)

    assert ("ios", "PAGENT") in calls
    assert ("iosxr", "R1") in calls
    assert len(calls) == 2


def test_pagent_as_classic_ios_resolves_cdp_links_to_managed_routers(lab_root, monkeypatch):
    """The PAGENT scenario, with PAGENT correctly typed `ios` instead of
    `iosxe`: R1/R2 (IOS XR) see PAGENT via
    CDP, PAGENT sees R1/R2 back via CDP -- both should resolve as
    candidate links, and an unmanaged CDP neighbor stays unresolved."""

    def fake_iosxr_collect(device_id, cfg):
        port = "Gig0/0" if device_id == "R1" else "Gig0/1"
        row = f"{'PAGENT':<17}{'Gi0/0/0/10':<18}{'144':<11}{'R':<12}{'Cisco 720':<12}{port}"
        return {
            "hostname": f"HOST-{device_id}",
            "show_version": "x",
            "show_running_config": "x",
            "show_lldp_neighbors": "Device ID       Local Intf                      Hold-time  Capability      Port ID\nTotal entries displayed: 0\n",
            "show_cdp_neighbors": f"{_CDP_HEADER}\n{row}\n",
            "show_ipv4_interface_brief": "",
        }

    def fake_ios_collect(device_id, cfg):
        row_r1 = f"{'HOST-R1':<17}{'Gig0/0':<18}{'144':<11}{'R':<12}{'Cisco 720':<12}Gi0/0/0/10"
        row_r2 = f"{'HOST-R2':<17}{'Gig0/1':<18}{'144':<11}{'R':<12}{'Cisco 720':<12}Gi0/0/0/10"
        unmanaged = "labsw-a07.example.com"
        cont = f"{'':<17}{'Fas 0/0':<18}{'159':<11}{'S I':<12}{'WS-C2960X':<12}Gig 1/0/16"
        return {
            "hostname": "PAGENT",
            "show_version": "Cisco IOS Software, 7200 Software",
            "show_cdp_neighbors": f"{_CDP_HEADER}\n{row_r1}\n{row_r2}\n{unmanaged}\n{cont}\n",
            "show_vrf": _VRF_HEADER + "\n",
            "show_ip_interface_brief": "",
        }

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_iosxr_collect)
    monkeypatch.setattr(discovery, "_bootstrap_collect_ios", fake_ios_collect)

    _write_access_info(
        lab_root,
        {
            "R1": {"type": "iosxr", "address": "192.0.2.11", "transport": "ssh"},
            "R2": {"type": "iosxr", "address": "192.0.2.12", "transport": "ssh"},
            "PAGENT": {"type": "ios", "address": "192.0.2.30", "transport": "telnet"},
        },
    )

    result = discovery.discover_topology(lab_root)

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
