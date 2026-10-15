"""L3 interface data (ipv4_address/vrf) must be visible in every
CLI topology rendering path, not silently omitted.

Investigation found `render_topology_block()` rendered only a device's
`type` -- `interfaces` existed in the data model (Discovery has populated
it since L3 enrichment was added) but was never shown by `show
running-config topology`/`show configuration`/candidate review. The
candidate delta renderer had the same gap: it only ever compared a
device's `type` field, so an L3-only change (device type unchanged) never
appeared in `show configuration` at all.

This file covers both the full-render and delta-render paths, plus an
end-to-end CLI scenario (commit, then a follow-up L3-only edit) proving
neither silently drops the data."""

from __future__ import annotations

import pytest

from network_lab_mcp import discovery, lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain


@pytest.fixture(autouse=True)
def _patch_lab_root(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)


# ---- full render: render_topology_block() ----


def test_render_topology_block_includes_interface_l3_data():
    data = {
        "name": "t",
        "devices": {
            "R1": {
                "type": "iosxr",
                "interfaces": {
                    "GigabitEthernet0/0/0/2": {"ipv4_address": "10.0.12.1", "vrf": "default"},
                    "BVI2000": {"ipv4_address": "10.20.0.253", "vrf": "VRF_ELAN_2000"},
                },
            }
        },
        "links": [],
    }
    lines = climain.render_topology_block(data)
    text = "\n".join(lines)
    assert "interface GigabitEthernet0/0/0/2" in text
    assert "ipv4_address 10.0.12.1" in text
    assert "vrf default" in text
    assert "interface BVI2000" in text
    assert "vrf VRF_ELAN_2000" in text


def test_render_topology_block_omits_interfaces_block_when_absent():
    data = {"name": "t", "devices": {"R1": {"type": "iosxr"}}, "links": []}
    text = "\n".join(climain.render_topology_block(data))
    assert "interface" not in text


# ---- delta render: render_topology_configuration_delta() ----


def test_delta_detects_interface_added():
    original = {"name": "t", "devices": {"R1": {"type": "iosxr"}}, "links": []}
    candidate = {
        "name": "t",
        "devices": {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"}}}},
        "links": [],
    }
    delta = climain.render_topology_configuration_delta("t", original, candidate)
    assert "interface Gi0/0" in delta
    assert "ipv4_address 10.0.0.1" in delta


def test_delta_detects_interface_ipv4_changed():
    original = {
        "name": "t",
        "devices": {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"}}}},
        "links": [],
    }
    candidate = {
        "name": "t",
        "devices": {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.0.9", "vrf": "default"}}}},
        "links": [],
    }
    delta = climain.render_topology_configuration_delta("t", original, candidate)
    assert "10.0.0.9" in delta
    assert "10.0.0.1" not in delta  # only the new value is shown, not the stale one


def test_delta_detects_interface_vrf_changed():
    original = {
        "name": "t",
        "devices": {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"}}}},
        "links": [],
    }
    candidate = {
        "name": "t",
        "devices": {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "VRF_A"}}}},
        "links": [],
    }
    delta = climain.render_topology_configuration_delta("t", original, candidate)
    assert "vrf VRF_A" in delta


def test_delta_detects_interface_removed():
    original = {
        "name": "t",
        "devices": {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"}}}},
        "links": [],
    }
    candidate = {"name": "t", "devices": {"R1": {"type": "iosxr", "interfaces": {}}}, "links": []}
    delta = climain.render_topology_configuration_delta("t", original, candidate)
    assert "no interface Gi0/0" in delta


def test_delta_detects_l3_only_change_with_device_type_unchanged():
    """The device's own `type` never changed -- only its interface data
    did. This must still surface in the delta, not disappear because the
    old field-order tuple was `("type",)` only."""
    original = {"name": "t", "devices": {"R1": {"type": "iosxr"}}, "links": []}
    candidate = {
        "name": "t",
        "devices": {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"}}}},
        "links": [],
    }
    delta = climain.render_topology_configuration_delta("t", original, candidate)
    assert delta  # not empty
    assert "device R1" in delta
    assert "interface Gi0/0" in delta
    assert "type" not in delta.split("device R1")[1].split("!")[0].strip().split("\n")[0]


def test_delta_is_empty_when_nothing_changed():
    data = {
        "name": "t",
        "devices": {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"}}}},
        "links": [],
    }
    delta = climain.render_topology_configuration_delta("t", data, data)
    assert delta == ""


# ---- end-to-end CLI: commit, then a follow-up L3-only edit ----


def test_committed_topology_l3_data_visible_via_show_running_config(lab_root, capsys):
    lab.write_topology(
        "sample_lab",
        {
            "name": "sample_lab",
            "devices": {
                "R1": {
                    "type": "iosxr",
                    "interfaces": {"GigabitEthernet0/0/0/2": {"ipv4_address": "10.0.12.1", "vrf": "default"}},
                }
            },
            "links": [],
        },
        lab_root,
    )

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config topology")
    out = capsys.readouterr().out
    assert "interface GigabitEthernet0/0/0/2" in out
    assert "ipv4_address 10.0.12.1" in out


def test_l3_only_candidate_edit_appears_in_show_configuration(lab_root, capsys):
    lab.write_topology(
        "sample_lab",
        {
            "name": "sample_lab",
            "devices": {"R1": {"type": "iosxr"}},
            "links": [],
        },
        lab_root,
    )

    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    climain.execute_command_line(session, "topology sample_lab")
    capsys.readouterr()  # discard entry-mode banner
    # No structured `interface` CLI command exists (matches `links`'
    # precedent) -- simulate the same kind of edit Discovery/the external
    # editor would make, directly on the candidate.
    session.definition_candidate["devices"]["R1"]["interfaces"] = {
        "GigabitEthernet0/0/0/2": {"ipv4_address": "10.0.12.1", "vrf": "default"}
    }

    climain.execute_command_line(session, "show configuration")
    out = capsys.readouterr().out
    assert "interface GigabitEthernet0/0/0/2" in out
    assert "ipv4_address 10.0.12.1" in out


# ---- Discovery result -> candidate -> review -> commit ->
# committed topology -> get_active_topology() all carry the same L3 data ----


def test_discovery_l3_data_survives_candidate_review_commit_and_active_selection(lab_root, monkeypatch, capsys):
    """End-to-end proof that a Discovery-observed interface's ipv4_address/
    vrf is never dropped anywhere along the real pipeline -- and that
    neither `discover topology` nor `commit` auto-selects the result as
    the active topology (that remains a separate, explicit step)."""
    result = discovery.DiscoveryResult(
        access_info_name="sample_lab",
        default_topology_name="discovered_lab",
        iosxr_target_count=1,
        connected_count=1,
        observation_count=1,
        devices={
            "R1": {
                "type": "iosxr",
                "interfaces": {"GigabitEthernet0/0/0/2": {"ipv4_address": "10.0.12.1", "vrf": "default"}},
            }
        },
        managed_links=[],
        unresolved=[],
        conflicts=[],
        identity_map={"R1": "HOST-R1"},
    )
    monkeypatch.setattr(discovery, "discover_topology", lambda lab_root=None, include_l3=False: result)

    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    climain.execute_command_line(session, "discover topology")

    # 1. candidate carries the interface data.
    assert session.definition_candidate["devices"]["R1"]["interfaces"]["GigabitEthernet0/0/0/2"] == {
        "ipv4_address": "10.0.12.1",
        "vrf": "default",
    }

    # 2. candidate review (`show configuration`) surfaces it.
    capsys.readouterr()
    climain.execute_command_line(session, "show configuration")
    review = capsys.readouterr().out
    assert "interface GigabitEthernet0/0/0/2" in review
    assert "ipv4_address 10.0.12.1" in review

    # 3. commit persists it, but discover+commit alone never auto-selects
    # this topology as the active one.
    climain.execute_command_line(session, "commit")
    persisted = lab.load_topology("discovered_lab", lab_root)
    assert persisted["devices"]["R1"]["interfaces"]["GigabitEthernet0/0/0/2"]["ipv4_address"] == "10.0.12.1"
    before_active = lab.read_settings(lab_root)["active_topology"]
    assert before_active != "discovered_lab"

    # 4. an explicit active-topology selection is a separate step ...
    session.mode = "running"
    climain.execute_command_line(session, "topology discovered_lab")
    climain.execute_command_line(session, "commit")

    # 5. ... after which get_active_topology() (the exact MCP-facing call)
    # returns the same L3 data, with no schema translation in between.
    active = lab.get_active_topology()
    assert active["active_topology"] == "discovered_lab"
    interfaces = active["topology"]["devices"]["R1"]["interfaces"]
    assert interfaces["GigabitEthernet0/0/0/2"] == {"ipv4_address": "10.0.12.1", "vrf": "default"}
