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


def test_live_fixtures_discover_14_links_from_lldp_alone(lab_root, monkeypatch):
    result = _discover(lab_root, monkeypatch)
    assert result.observation_count == 28
    assert result.cdp_observation_count == 0
    assert len(result.managed_links) == 14
    assert result.unresolved == [] and result.conflicts == []
    # CDP disabled (R1, R2) and unsupported (R3-R6) are protocol statuses of a
    # successful run, reported distinctly -- not warnings.
    assert result.protocol_status == {"CDP disabled": ["R1", "R2"], "CDP unsupported": ["R3", "R4", "R5", "R6"]}
    assert result.protocol_warnings == []
    pairs = {tuple(sorted(((l.a_device, l.a_interface), (l.b_device, l.b_interface)))) for l in result.managed_links}
    G = "GigabitEthernet0/0/0/"
    assert (("R1", G + "10"), ("R5", G + "6")) in pairs
    assert (("R1", G + "11"), ("R5", G + "7")) in pairs
    assert (("R2", G + "10"), ("R6", G + "6")) in pairs
    assert (("R2", G + "11"), ("R6", G + "7")) in pairs
    assert not any({"R1", "R6"} == {l.a_device, l.b_device} for l in result.managed_links)
    assert not any({"R2", "R5"} == {l.a_device, l.b_device} for l in result.managed_links)


def test_live_discovery_replaces_stale_committed_links(lab_root, monkeypatch, capsys):
    stale = [
        ("R1", "GigabitEthernet0/0/0/10", "R6", "GigabitEthernet0/0/0/6"),
        ("R1", "GigabitEthernet0/0/0/11", "R6", "GigabitEthernet0/0/0/7"),
        ("R2", "GigabitEthernet0/0/0/10", "R5", "GigabitEthernet0/0/0/6"),
        ("R2", "GigabitEthernet0/0/0/11", "R5", "GigabitEthernet0/0/0/7"),
    ]
    lab.write_topology(
        "sample_lab",
        {
            "name": "sample_lab",
            "description": "",
            "devices": {d: {"type": "iosxr"} for d in IDS},
            "links": [{"a": a, "a_interface": ai, "b": b, "b_interface": bi} for a, ai, b, bi in stale],
        },
        lab_root,
    )
    _access(lab_root)
    monkeypatch.setattr(discovery, "_bootstrap_collect", _live_collect())
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    capsys.readouterr()
    assert climain.execute_command_line(session, "discover topology")
    out = capsys.readouterr().out
    links = session.definition_candidate["links"]
    assert len(links) == 14
    for a, ai, b, bi in stale:
        assert not any(
            {(l["a"], l["a_interface"]), (l["b"], l["b_interface"])} == {(a, ai), (b, bi)} for l in links
        )
    assert "Managed links:        14" in out and "LLDP observations:    28" in out
    assert "CDP observations:     0" in out
    assert "Discovery complete." in out and "Topology candidate not updated" not in out
    assert len(lab.load_topology("sample_lab", lab_root)["links"]) == 4  # committed unchanged


def test_live_cli_output_shows_protocol_status_and_success(lab_root, monkeypatch, capsys):
    _access(lab_root)
    monkeypatch.setattr(discovery, "_bootstrap_collect", _live_collect())
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    capsys.readouterr()
    assert climain.execute_command_line(session, "discover topology")
    out = capsys.readouterr().out
    assert (
        "Neighbor discovery protocol status:\n"
        "  CDP disabled:         2 devices (R1, R2)\n"
        "  CDP unsupported:      4 devices (R3, R4, R5, R6)\n"
    ) in out
    assert out.rstrip().endswith("Topology discovery completed successfully.")
    assert "Neighbor discovery warnings" not in out and "completed with warnings" not in out


def test_real_warning_still_uses_warnings_section(lab_root, monkeypatch, capsys):
    """A genuine warning (here: no link at all was discovered while a
    protocol is disabled) keeps the warnings section and footer."""
    _access(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _only_lldp_collect("% CDP is not enabled\n"))
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    capsys.readouterr()
    assert climain.execute_command_line(session, "discover topology")
    out = capsys.readouterr().out
    assert "  CDP disabled:         1 device (R1)" in out
    assert "Neighbor discovery warnings (1):" in out and "No physical links were discovered" in out
    assert out.rstrip().endswith("Topology discovery completed with warnings.")


# ---- normal / empty / broken CDP tables ------------------------------------


def _lldp_empty():
    return "Device ID       Local Intf                      Hold-time  Capability      Port ID\nTotal entries displayed: 0\n"


def _only_lldp_collect(cdp_text, show_version=None):
    def collect(device_id, cfg, include_l3=False):
        return {
            "hostname": f"LAB_SR_{device_id}",
            "show_version": show_version if show_version is not None else _fx("R1", "show_version"),
            "show_lldp_neighbors": _lldp_empty(),
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
    assert result.cdp_observation_count == 2 and result.protocol_warnings == [] and result.protocol_status == {}
    assert len(result.managed_links) == 1


def test_valid_empty_cdp_table_is_success(lab_root, monkeypatch):
    _access(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _only_lldp_collect(f"{CDP_HDR}\n"))
    result = discovery.discover_topology(lab_root)
    assert result.cdp_observation_count == 0 and result.protocol_warnings == []


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
def test_unknown_or_broken_cdp_output_is_fatal(lab_root, monkeypatch, cdp_text):
    _access(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _only_lldp_collect(cdp_text))
    with pytest.raises(discovery.DiscoveryError):
        discovery.discover_topology(lab_root)


# ---- "unsupported" is not widened beyond the measured condition -------------


def test_invalid_input_on_non_lnt_iosxr_is_fatal(lab_root, monkeypatch):
    _access(lab_root, ["R1"])
    monkeypatch.setattr(
        discovery, "_bootstrap_collect", _only_lldp_collect(INVALID, show_version=_fx("R1", "show_version"))
    )
    with pytest.raises(discovery.DiscoveryError, match="not recognized as valid CDP"):
        discovery.discover_topology(lab_root)


def test_invalid_input_without_identifying_show_version_is_fatal(lab_root, monkeypatch):
    _access(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _only_lldp_collect(INVALID, show_version=""))
    with pytest.raises(discovery.DiscoveryError):
        discovery.discover_topology(lab_root)


def test_lnt_version_with_a_different_error_text_is_fatal(lab_root, monkeypatch):
    _access(lab_root, ["R1"])
    monkeypatch.setattr(
        discovery,
        "_bootstrap_collect",
        _only_lldp_collect("% Ambiguous command\n", show_version=_fx("R3", "show_version")),
    )
    with pytest.raises(discovery.DiscoveryError):
        discovery.discover_topology(lab_root)


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
    with pytest.raises(discovery.DiscoveryError, match="not recognized as valid CDP"):
        discovery.discover_topology(lab_root)


def test_invalid_input_for_lldp_is_still_fatal_on_lnt(lab_root, monkeypatch):
    _access(lab_root, ["R3"])
    monkeypatch.setattr(
        discovery, "_bootstrap_collect", _live_collect({"R3": {"show_lldp_neighbors": INVALID}})
    )
    with pytest.raises(discovery.DiscoveryError):
        discovery.discover_topology(lab_root)


def test_lnt_with_lldp_unavailable_and_cdp_unsupported_is_fatal(lab_root, monkeypatch):
    _access(lab_root, ["R3"])
    monkeypatch.setattr(
        discovery,
        "_bootstrap_collect",
        _live_collect({"R3": {"show_lldp_neighbors": "% LLDP is not enabled\n"}}),
    )
    with pytest.raises(discovery.DiscoveryError, match=r"no usable neighbor discovery protocol \(LLDP disabled, CDP unsupported\)"):
        discovery.discover_topology(lab_root)
