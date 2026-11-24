# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Regression: the live 6-router IOS XR SR core (sanitized read-only
captures in tests/fixtures/live_sr_core/). R1/R2 answer `show cdp neighbors`
with `% CDP is not enabled` (CDP disabled); R3-R6 are IOS XR LNT images that
answer with `% Invalid input detected` (CDP not supported). LLDP is healthy
everywhere, so Discovery must succeed from LLDP alone -- while the
"unsupported" reading stays limited to exactly that measured condition.
Collection is faked from the fixtures (no terminal/device access)."""

from __future__ import annotations

import pathlib

import pytest

from network_lab_mcp import discovery, lab, terminal
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain

FX = pathlib.Path(__file__).parent / "fixtures" / "live_sr_core"
IDS = [f"R{i}" for i in range(1, 7)]
CDP_HDR = "Device ID        Local Intrfce     Holdtme    Capability  Platform  Port ID"
INVALID = "                                    ^\n% Invalid input detected at '^' marker.\n"


def _fx(device, command):
    return (FX / f"{device}_{command}.txt").read_text()


def _live_collect(overrides=None):
    overrides = overrides or {}

    def collect(device_id, cfg, include_l3=False):
        info = {
            "hostname": f"LAB_SR_{device_id}",
            "show_version": _fx(device_id, "show_version"),
            "show_lldp_neighbors": _fx(device_id, "show_lldp_neighbors"),
            "show_cdp_neighbors": _fx(device_id, "show_cdp_neighbors"),
        }
        info.update(overrides.get(device_id, {}))
        return info

    return collect


@pytest.fixture(autouse=True)
def _env(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    monkeypatch.setattr(terminal, "close_bootstrap_terminal", lambda d: None)


def _access(lab_root, ids=IDS, types=None):
    devices = {d: {"type": (types or {}).get(d, "iosxr"), "address": "192.0.2.1", "transport": "ssh"} for d in ids}
    lab.write_access_info("sample_lab", {"name": "sample_lab", "devices": devices}, lab_root)


def _discover(lab_root, monkeypatch, overrides=None, ids=IDS, types=None, collector="_bootstrap_collect"):
    _access(lab_root, ids, types)
    monkeypatch.setattr(discovery, collector, _live_collect(overrides))
    return discovery.discover_topology(lab_root)


# ---- per-device CDP classification on the live captures -------------------


@pytest.mark.parametrize("device", ["R1", "R2"])
def test_r1_r2_cdp_is_disabled(device):
    cdp = _fx(device, "show_cdp_neighbors")
    assert discovery.iosxr_protocol_disabled(cdp, "cdp", device) is True
    assert not discovery._cdp_table_recognized(cdp)
    assert not discovery._iosxr_lnt_cdp_unsupported(_fx(device, "show_version"), cdp)


@pytest.mark.parametrize("device", ["R3", "R4", "R5", "R6"])
def test_r3_to_r6_cdp_is_unsupported_on_lnt(device):
    cdp = _fx(device, "show_cdp_neighbors")
    assert discovery.iosxr_protocol_disabled(cdp, "cdp", device) is False
    assert discovery._iosxr_lnt_cdp_unsupported(_fx(device, "show_version"), cdp) is True


# ---- full Discovery on the live captures -----------------------------------


def _outcome(result, device_id):
    return next(o for o in result.outcomes if o.device_id == device_id)


def test_live_fixtures_discover_14_links_from_lldp_alone(lab_root, monkeypatch):
    result = _discover(lab_root, monkeypatch)
    assert result.observation_count == 28
    assert result.cdp_observation_count == 0
    assert len(result.managed_links) == 14
    assert result.unresolved == [] and result.conflicts == []
    # CDP disabled (R1, R2) and unsupported (R3-R6) are protocol states of a
    # Complete device (LLDP works) -- reported distinctly, never as warnings.
    assert {o.device_id: o.cdp for o in result.outcomes} == {
        "R1": "disabled", "R2": "disabled", "R3": "unsupported", "R4": "unsupported",
        "R5": "unsupported", "R6": "unsupported",
    }
    assert all(o.lldp == "enabled" and o.status == "Complete" for o in result.outcomes)
    assert result.warnings == []
    pairs = {tuple(sorted(((l.a_device, l.a_interface), (l.b_device, l.b_interface)))) for l in result.managed_links}
    G = "GigabitEthernet0/0/0/"
    assert (("R1", G + "10"), ("R5", G + "6")) in pairs
    assert (("R1", G + "11"), ("R5", G + "7")) in pairs
    assert (("R2", G + "10"), ("R6", G + "6")) in pairs
    assert (("R2", G + "11"), ("R6", G + "7")) in pairs
    assert not any({"R1", "R6"} == {l.a_device, l.b_device} for l in result.managed_links)
    assert not any({"R2", "R5"} == {l.a_device, l.b_device} for l in result.managed_links)


def _write_committed_links(lab_root, links):
    lab.write_topology(
        "sample_lab",
        {
            "name": "sample_lab",
            "description": "",
            "devices": {d: {"type": "iosxr"} for d in IDS},
            "links": [{"a": a, "a_interface": ai, "b": b, "b_interface": bi} for a, ai, b, bi in links],
        },
        lab_root,
    )


def test_live_discovery_refuses_links_that_contradict_the_committed_topology(lab_root, monkeypatch, capsys):
    """The committed links claim R1 Gi10 <-> R6 Gi6, but the live LLDP says
    R1 Gi10 <-> R5 Gi6: a contradiction is never resolved by deleting the
    stored link -- the whole candidate update is refused (M13)."""
    stale = [
        ("R1", "GigabitEthernet0/0/0/10", "R6", "GigabitEthernet0/0/0/6"),
        ("R2", "GigabitEthernet0/0/0/10", "R5", "GigabitEthernet0/0/0/6"),
    ]
    _write_committed_links(lab_root, stale)
    _access(lab_root)
    monkeypatch.setattr(discovery, "_bootstrap_collect", _live_collect())
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    capsys.readouterr()
    assert climain.execute_command_line(session, "discover topology") is False
    out = capsys.readouterr().out
    assert "contradicts the existing topology" in out and "% Topology candidate not updated." in out
    assert "Device Discovery Summary" not in out  # no success-looking summary on a fatal error
    assert session.definition_candidate is None
    assert len(lab.load_topology("sample_lab", lab_root)["links"]) == 2


def test_live_discovery_keeps_non_contradicting_committed_links(lab_root, monkeypatch, capsys):
    extra = ("R1", "GigabitEthernet0/0/0/20", "R6", "GigabitEthernet0/0/0/20")  # not seen by the live devices
    _write_committed_links(lab_root, [extra])
    _access(lab_root)
    monkeypatch.setattr(discovery, "_bootstrap_collect", _live_collect())
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    capsys.readouterr()
    assert climain.execute_command_line(session, "discover topology")
    out = capsys.readouterr().out
    links = session.definition_candidate["links"]
    assert len(links) == 15  # the stored link is retained, the 14 live links are added
    assert links[0] == {"a": extra[0], "a_interface": extra[1], "b": extra[2], "b_interface": extra[3]}
    assert "Discovered links         : 14" in out
    assert "Links added              : 14" in out and "Links retained           : 1" in out
    assert "Topology candidate not updated" not in out
    assert len(lab.load_topology("sample_lab", lab_root)["links"]) == 1  # committed unchanged


def test_live_cli_output_shows_protocol_states_and_success(lab_root, monkeypatch, capsys):
    _access(lab_root)
    monkeypatch.setattr(discovery, "_bootstrap_collect", _live_collect())
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    capsys.readouterr()
    assert climain.execute_command_line(session, "discover topology")
    out = capsys.readouterr().out
    assert "Device   Type    Connection   LLDP      CDP           Status" in out
    assert "R1       iosxr   Connected    Enabled   Disabled      Complete" in out
    assert "R3       iosxr   Connected    Enabled   Unsupported   Complete" in out
    assert "Devices registered       : 6" in out and "Devices without protocol : 0" in out
    assert "Warnings:" not in out and "with limitations" not in out
    assert out.rstrip().endswith("Commit is required to save the topology.")


def test_real_warning_still_uses_warnings_section(lab_root, monkeypatch, capsys):
    """A genuine limitation (no usable neighbor protocol at all) keeps the
    Warnings section; the committed topology is left as it was."""
    _access(lab_root, ["R1"])
    monkeypatch.setattr(
        discovery, "_bootstrap_collect", _only_lldp_collect("% CDP is not enabled\n", lldp_text="% LLDP is not enabled\n")
    )
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    capsys.readouterr()
    assert climain.execute_command_line(session, "discover topology")
    out = capsys.readouterr().out
    assert "Warnings:\n  R1: No usable neighbor discovery protocol." in out
    assert "Devices without protocol : 1" in out
    assert out.rstrip().endswith("Topology candidate unchanged.")
    assert session.definition_candidate["links"] == [{"a": "R1", "b": "R2"}]  # nothing removed


# ---- normal / empty / broken CDP tables ------------------------------------


def _lldp_empty():
    return "Device ID       Local Intf                      Hold-time  Capability      Port ID\nTotal entries displayed: 0\n"


def _only_lldp_collect(cdp_text, show_version=None, lldp_text=None):
    def collect(device_id, cfg, include_l3=False):
        return {
            "hostname": f"LAB_SR_{device_id}",
            "show_version": show_version if show_version is not None else _fx("R1", "show_version"),
            "show_lldp_neighbors": lldp_text if lldp_text is not None else _lldp_empty(),
            "show_cdp_neighbors": cdp_text,
        }

    return collect


def test_valid_cdp_table_with_neighbors_is_success(lab_root, monkeypatch):
    _access(lab_root, ["R1", "R2"])

    def collect(device_id, cfg, include_l3=False):
        remote = "R2" if device_id == "R1" else "R1"
        row = f"{'LAB_SR_' + remote:<17}{'Gi0/0/0/1':<18}{'144':<11}{'R':<12}{'ASR9K':<12}Gi0/0/0/1"
        return {
            "hostname": f"LAB_SR_{device_id}",
            "show_version": _fx("R1", "show_version"),
            "show_lldp_neighbors": _lldp_empty(),
            "show_cdp_neighbors": f"{CDP_HDR}\n{row}\n",
        }

    monkeypatch.setattr(discovery, "_bootstrap_collect", collect)
    result = discovery.discover_topology(lab_root)
    assert result.cdp_observation_count == 2 and result.warnings == []
    assert all(o.cdp == "enabled" and o.status == "Complete" for o in result.outcomes)
    assert len(result.managed_links) == 1


def test_valid_empty_cdp_table_is_success(lab_root, monkeypatch):
    _access(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _only_lldp_collect(f"{CDP_HDR}\n"))
    result = discovery.discover_topology(lab_root)
    assert result.cdp_observation_count == 0 and result.warnings == []
    assert _outcome(result, "R1").status == "Complete"


@pytest.mark.parametrize(
    "cdp_text",
    [
        "% Something unexpected happened\n",
        "",
        "Device ID        Local Intrfce     Holdtme\n",  # truncated header (no Port ID column)
        f"{CDP_HDR}\nthis row is garbage\nso is this one\n",  # header, but no row parses
        INVALID + "% extra unexpected line\n",  # invalid-input mixed with other content
        "% Invalid input detected at '^' marker.\n% CDP is not enabled\n",
    ],
)
def test_unknown_or_broken_cdp_output_is_unusable_not_fatal(lab_root, monkeypatch, cdp_text):
    """Parser fail-closed is kept: unrecognized/broken CDP output yields no
    observation and a warning (Partial), never a guess -- and no longer
    fails the run or the device's registration."""
    _access(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _only_lldp_collect(cdp_text))
    result = discovery.discover_topology(lab_root)
    o = _outcome(result, "R1")
    assert not o.cdp_ok and o.cdp in ("unknown", "enabled") and o.cdp != "unsupported"
    assert o.status == "Partial" and o.warnings and result.cdp_observation_count == 0
    assert "R1" in result.devices


# ---- "unsupported" is not widened beyond the measured condition -------------


def test_invalid_input_on_non_lnt_iosxr_is_not_unsupported(lab_root, monkeypatch):
    _access(lab_root, ["R1"])
    monkeypatch.setattr(
        discovery, "_bootstrap_collect", _only_lldp_collect(INVALID, show_version=_fx("R1", "show_version"))
    )
    o = _outcome(discovery.discover_topology(lab_root), "R1")
    assert o.cdp == "unknown" and "not recognized as valid CDP" in o.warnings[0]


def test_invalid_input_without_identifying_show_version_is_not_unsupported(lab_root, monkeypatch):
    _access(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _only_lldp_collect(INVALID, show_version=""))
    o = _outcome(discovery.discover_topology(lab_root), "R1")
    assert o.cdp == "unknown" and o.status == "Partial"


def test_lnt_version_with_a_different_error_text_is_not_unsupported(lab_root, monkeypatch):
    _access(lab_root, ["R1"])
    monkeypatch.setattr(
        discovery,
        "_bootstrap_collect",
        _only_lldp_collect("% Ambiguous command\n", show_version=_fx("R3", "show_version")),
    )
    o = _outcome(discovery.discover_topology(lab_root), "R1")
    assert o.cdp == "unknown" and o.status == "Partial"


@pytest.mark.parametrize("os_type,collector", [("iosxe", "_bootstrap_collect_iosxe"), ("ios", "_bootstrap_collect_ios")])
def test_unsupported_is_not_extended_to_ios_xe_or_ios(lab_root, monkeypatch, os_type, collector):
    _access(lab_root, ["X1"], {"X1": os_type})

    def collect(device_id, cfg, include_l3=False):
        info = {
            "hostname": "LAB_X1",
            "show_version": _fx("R3", "show_version"),  # even an LNT banner must not matter
            "show_cdp_neighbors": INVALID,
        }
        if os_type == "iosxe":
            info["show_lldp_neighbors"] = _lldp_empty()
        return info

    monkeypatch.setattr(discovery, collector, collect)
    o = _outcome(discovery.discover_topology(lab_root), "X1")
    assert o.cdp == "unknown" and "not recognized as valid CDP" in o.warnings[0]


def test_invalid_input_for_lldp_leaves_lldp_unusable_on_lnt(lab_root, monkeypatch):
    _access(lab_root, ["R3"])
    monkeypatch.setattr(
        discovery, "_bootstrap_collect", _live_collect({"R3": {"show_lldp_neighbors": INVALID}})
    )
    o = _outcome(discovery.discover_topology(lab_root), "R3")
    assert (o.lldp, o.cdp, o.status) == ("unknown", "unsupported", "Partial")
    assert o.warnings and o.warnings[0].startswith("LLDP observations were not used")


def test_lnt_with_lldp_unavailable_and_cdp_unsupported_is_partial_with_a_warning(lab_root, monkeypatch):
    _access(lab_root, ["R3"])
    monkeypatch.setattr(
        discovery,
        "_bootstrap_collect",
        _live_collect({"R3": {"show_lldp_neighbors": "% LLDP is not enabled\n"}}),
    )
    result = discovery.discover_topology(lab_root)
    o = _outcome(result, "R3")
    assert (o.lldp, o.cdp, o.status) == ("disabled", "unsupported", "Partial")
    assert result.warnings == ["R3: No usable neighbor discovery protocol."]
