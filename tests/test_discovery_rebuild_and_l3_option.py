# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""`discover topology` / `discover topology l3`: candidate rebuild (no link
merge), optional L3, strict protocol/conflict fatality, dirty-candidate
protection and atomicity. Collection is faked at the `_run_command` /
`_bootstrap_collect*` level -- no terminal, tmux, ssh or real device;
fictional data, isolated lab_root."""

from __future__ import annotations

import random

import pytest

from network_lab_mcp import discovery, lab, terminal
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain

LLDP_HDR = "Device ID       Local Intf                      Hold-time  Capability      Port ID"
CDP_HDR = "Device ID        Local Intrfce     Holdtme    Capability  Platform  Port ID"
L3_HDR = "Interface                     IP-Address   Status Protocol Vrf-Name"
LLDP_OFF = "% LLDP is not enabled\n"
CDP_OFF = "% CDP is not enabled\n"
LLDP_EMPTY = f"{LLDP_HDR}\nTotal entries displayed: 0\n"
CDP_EMPTY = f"{CDP_HDR}\n"
SENTINEL = "SENTINEL-pw"


def lldp(*rows):
    body = "\n".join(f"{r[0]}  {r[1]}  120  R  {r[2]}" for r in rows)
    return f"{LLDP_HDR}\n{body}\n\nTotal entries displayed: {len(rows)}\n"


def cdp(*rows):
    col = CDP_HDR.index("Port ID")
    body = "\n".join((f"{r[0]}  {r[1]}  144  R  ASR9K  ").ljust(col) + r[2] for r in rows)
    return f"{CDP_HDR}\n{body}\n"


@pytest.fixture(autouse=True)
def _env(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    monkeypatch.setattr(terminal, "close_bootstrap_terminal", lambda d: None)


def _access(lab_root, ids, types=None):
    devices = {
        d: {"type": (types or {}).get(d, "iosxr"), "address": f"192.0.2.{i + 10}", "transport": "ssh", "password": SENTINEL}
        for i, d in enumerate(ids)
    }
    lab.write_access_info("sample_lab", {"name": "sample_lab", "devices": devices}, lab_root)


def _fake_xr(outputs, l3=None):
    """outputs: {device: (lldp_text, cdp_text)}; l3: {device: text}."""

    def collect(device_id, cfg, include_l3=False):
        lldp_text, cdp_text = outputs[device_id]
        info = {
            "hostname": f"HOST-{device_id}",
            "show_version": "x",
            "show_lldp_neighbors": lldp_text,
            "show_cdp_neighbors": cdp_text,
        }
        if include_l3:
            info["show_ipv4_interface_brief"] = (l3 or {}).get(device_id, f"{L3_HDR}\n")
        return info

    return collect


def _session(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    return session


def _run(session, line="discover topology"):
    return climain.execute_command_line(session, line)


def _rediscover(session, line="discover topology"):
    """`discover` exists only in global mode; a successful run leaves the
    session in the topology submode, so go back with `root` first."""
    session.go_to_global()
    return _run(session, line)


# ==========================================================================
# CLI grammar / help (A13)
# ==========================================================================


def test_help_lists_l3_option_and_cr():
    ctx = grammar.CliContext()
    bare = grammar.help("global", "discover topology ", ctx)
    assert [(l.token, l.description) for l in bare.lines] == [("l3", "Include Layer 3 interface information")]
    assert bare.show_cr is True
    l3 = grammar.help("global", "discover topology l3 ", ctx)
    assert l3.lines == [] and l3.show_cr is True


def test_parse_abbreviation_completion_and_invalid_arguments():
    ctx = grammar.CliContext()
    assert grammar.parse("global", "discover topology").action == "global.discover_topology"
    assert grammar.parse("global", "disc top l3").action == "global.discover_topology_l3"
    assert grammar.parse("global", "disc top l").action == "global.discover_topology_l3"
    assert grammar.complete("global", "discover topology l", ctx).candidates == ["l3"]
    for bad in ("discover topology x", "discover topology l3 x", "discover topology l3 l3"):
        assert not grammar.parse("global", bad).ok
    assert not grammar.parse("exec", "discover topology").ok
    assert not grammar.parse("exec", "discover topology l3").ok


# ==========================================================================
# OS-specific command contract: L2 never sends L3-only commands (A3, A17)
# ==========================================================================


def _record_commands(monkeypatch, outputs_by_cmd, login):
    sent = []

    def run(device_id, cmd, prompt_re=None, timeout=None):
        sent.append(cmd)
        return outputs_by_cmd.get(cmd, "")

    monkeypatch.setattr(discovery, "_run_command", run)
    monkeypatch.setattr(discovery, "_disable_terminal_paging", lambda d, p: sent.append("terminal length 0"))
    monkeypatch.setattr(discovery, login, lambda d, c: "HOST")
    return sent


@pytest.mark.parametrize(
    "collector,login,l2,l3",
    [
        (
            discovery._bootstrap_collect,
            "_login",
            ["terminal length 0", "show version", "show lldp neighbors", "show cdp neighbors"],
            ["show ipv4 interface brief"],
        ),
        (
            discovery._bootstrap_collect_iosxe,
            "_login_ios_style",
            ["terminal length 0", "show version", "show lldp neighbors", "show cdp neighbors"],
            ["show vrf", "show ip interface brief"],
        ),
        (
            discovery._bootstrap_collect_ios,
            "_login_ios_style",
            ["terminal length 0", "show version", "show cdp neighbors"],
            ["show vrf", "show ip interface brief"],
        ),
    ],
)
def test_collectors_send_l3_commands_only_when_requested(monkeypatch, collector, login, l2, l3):
    sent = _record_commands(monkeypatch, {}, login)
    info = collector("D1", {})
    assert sent == l2
    assert not any(key.startswith(("show_ip", "show_vrf")) for key in info)
    sent.clear()
    info = collector("D1", {}, include_l3=True)
    assert sent == l2 + l3


def test_l2_discovery_never_invokes_l3_parsers(lab_root, monkeypatch):
    _access(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr({"R1": (LLDP_EMPTY, CDP_EMPTY)}))

    def boom(*a, **k):
        raise AssertionError("L3 parser called in L2 mode")

    for name in ("parse_ipv4_interface_brief", "parse_ip_interface_brief", "parse_show_vrf"):
        monkeypatch.setattr(discovery, name, boom)
    result = discovery.discover_topology(lab_root)
    assert not result.include_l3 and "interfaces" not in result.devices["R1"]


# ==========================================================================
# Candidate rebuild: the R1/R2/R5/R6 stale-link regression (A1)
# ==========================================================================


def _committed_stale(lab_root):
    stale = [
        ("R1", "Gi0/0/0/10", "R6", "Gi0/0/0/6"),
        ("R1", "Gi0/0/0/11", "R6", "Gi0/0/0/7"),
        ("R2", "Gi0/0/0/10", "R5", "Gi0/0/0/6"),
        ("R2", "Gi0/0/0/11", "R5", "Gi0/0/0/7"),
    ]
    links = [{"a": a, "a_interface": ai, "b": b, "b_interface": bi} for a, ai, b, bi in stale]
    lab.write_topology(
        "sample_lab",
        {
            "name": "sample_lab",
            "description": "kept",
            "devices": {d: {"type": "iosxr"} for d in ("R1", "R2", "R5", "R6")},
            "links": links,
        },
        lab_root,
    )


def _r1r2r5r6_outputs():
    return {
        "R1": (lldp(("HOST-R5", "Gi0/0/0/10", "Gi0/0/0/6"), ("HOST-R5", "Gi0/0/0/11", "Gi0/0/0/7")), CDP_OFF),
        "R2": (lldp(("HOST-R6", "Gi0/0/0/10", "Gi0/0/0/6"), ("HOST-R6", "Gi0/0/0/11", "Gi0/0/0/7")), CDP_OFF),
        "R5": (lldp(("HOST-R1", "Gi0/0/0/6", "Gi0/0/0/10"), ("HOST-R1", "Gi0/0/0/7", "Gi0/0/0/11")), CDP_OFF),
        "R6": (lldp(("HOST-R2", "Gi0/0/0/6", "Gi0/0/0/10"), ("HOST-R2", "Gi0/0/0/7", "Gi0/0/0/11")), CDP_OFF),
    }


def test_contradicting_committed_links_refuse_the_update_and_nothing_is_removed(lab_root, monkeypatch, capsys):
    """The stored R1 Gi10 <-> R6 Gi6 link contradicts the live R1 Gi10 <->
    R5 Gi6 observation. Discovery never resolves that by deleting the stored
    link: the whole candidate update is refused (M13)."""
    _committed_stale(lab_root)
    _access(lab_root, ["R1", "R2", "R5", "R6"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr(_r1r2r5r6_outputs()))
    session = _session(lab_root)
    capsys.readouterr()
    assert _run(session) is False
    out = capsys.readouterr().out
    assert "contradicts the existing topology" in out and "R1 GigabitEthernet0/0/0/10" in out or "R1 Gi0/0/0/10" in out
    assert "% Topology candidate not updated." in out
    assert "no topology <name>" in out  # the way out is to delete explicitly and rediscover
    assert session.definition_candidate is None and session.mode == "global"
    assert len(lab.load_topology("sample_lab", lab_root)["links"]) == 4


def test_unobserved_committed_links_are_retained_and_new_links_added(lab_root, monkeypatch, capsys):  # M06
    kept = {"a": "R1", "a_interface": "Gi0/0/0/20", "b": "R6", "b_interface": "Gi0/0/0/20"}
    lab.write_topology(
        "sample_lab",
        {
            "name": "sample_lab",
            "description": "kept",
            "devices": {d: {"type": "iosxr"} for d in ("R1", "R2", "R5", "R6")},
            "links": [kept],
        },
        lab_root,
    )
    _access(lab_root, ["R1", "R2", "R5", "R6"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr(_r1r2r5r6_outputs()))
    session = _session(lab_root)
    capsys.readouterr()
    assert _run(session)
    out = capsys.readouterr().out
    links = session.definition_candidate["links"]
    assert links[0] == kept and len(links) == 5
    assert session.definition_candidate["description"] == "kept"
    assert "Links added              : 4" in out and "Links retained           : 1" in out
    assert "removed" not in out.lower()
    # ... and nothing is persisted before commit.
    assert lab.load_topology("sample_lab", lab_root)["links"] == [kept]
    assert session.mode == "topology"


def test_candidate_is_deterministic_under_observation_and_device_order(lab_root, monkeypatch):
    _access(lab_root, ["R1", "R2", "R5", "R6"])
    base = _r1r2r5r6_outputs()
    candidates = []
    for seed in range(6):
        rng = random.Random(seed)
        outputs = {}
        for dev in rng.sample(sorted(base), len(base)):
            lldp_text, cdp_text = base[dev]
            header, *rows = [l for l in lldp_text.splitlines() if l.strip() and not l.startswith("Total")]
            rng.shuffle(rows)
            outputs[dev] = (header + "\n" + "\n".join(rows) + f"\n\nTotal entries displayed: {len(rows)}\n", cdp_text)
        monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr(outputs))
        session = _session(lab_root)
        assert _run(session)
        candidates.append(session.definition_candidate)
    assert all(c == candidates[0] for c in candidates)
    assert list(candidates[0]["devices"]) == sorted(candidates[0]["devices"])
    # The committed R1-R2 link (no interfaces) stays first; discovered links follow, sorted.
    assert candidates[0]["links"][0] == {"a": "R1", "b": "R2"}
    discovered = candidates[0]["links"][1:]
    keys = [discovery._topology_link_key(l) for l in discovered]
    assert len(discovered) == 4 and keys == sorted(keys)
    assert all((l["a"], l["a_interface"]) <= (l["b"], l["b_interface"]) for l in discovered)


def test_reverse_link_orientation_is_one_link(lab_root, monkeypatch):
    _access(lab_root, ["R1", "R2"])
    outputs = {
        "R1": (lldp(("HOST-R2", "Gi0/0/0/1", "Gi0/0/0/2")), CDP_OFF),
        "R2": (lldp(("HOST-R1", "Gi0/0/0/2", "Gi0/0/0/1")), CDP_OFF),
    }
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr(outputs))
    session = _session(lab_root)
    assert _run(session)
    assert session.definition_candidate["links"] == [
        {"a": "R1", "b": "R2"},  # the committed link is retained, never replaced
        {"a": "R1", "a_interface": "Gi0/0/0/1", "b": "R2", "b_interface": "Gi0/0/0/2"},
    ]


def test_managed_devices_without_links_are_included(lab_root, monkeypatch):
    _access(lab_root, ["R1", "R2"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr({d: (LLDP_EMPTY, CDP_OFF) for d in ("R1", "R2")}))
    session = _session(lab_root)
    assert _run(session)
    assert session.definition_candidate["devices"] == {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}}
    assert session.definition_candidate["links"] == [{"a": "R1", "b": "R2"}]  # nothing observed, nothing removed


# ==========================================================================
# Physical-link invariant and reconciliation conflicts are fatal (A9)
# ==========================================================================


@pytest.mark.parametrize(
    "outputs,message",
    [
        # R1:Gi10 claimed by two links (one-sided observations from R5 and R6).
        (
            {
                "R1": (LLDP_EMPTY, CDP_EMPTY),
                "R5": (lldp(("HOST-R1", "Gi0/0/0/6", "Gi0/0/0/10")), CDP_EMPTY),
                "R6": (lldp(("HOST-R1", "Gi0/0/0/6", "Gi0/0/0/10")), CDP_EMPTY),
            },
            "R1 Gi0/0/0/10: interface appears in more than one link",
        ),
        # Reciprocal pair disagrees about the interface mapping.
        (
            {
                "R1": (lldp(("HOST-R5", "Gi0/0/0/10", "Gi0/0/0/6")), CDP_EMPTY),
                "R5": (lldp(("HOST-R1", "Gi0/0/0/6", "Gi0/0/0/99")), CDP_EMPTY),
                "R6": (LLDP_EMPTY, CDP_EMPTY),
            },
            "inconsistent reciprocal observation",
        ),
        # Managed link on R1:Gi10 but another observation says an unmanaged neighbor.
        (
            {
                "R1": (lldp(("HOST-R5", "Gi0/0/0/10", "Gi0/0/0/6"), ("unmanaged-sw", "Gi0/0/0/10", "Gi0/1")), CDP_EMPTY),
                "R5": (lldp(("HOST-R1", "Gi0/0/0/6", "Gi0/0/0/10")), CDP_EMPTY),
                "R6": (LLDP_EMPTY, CDP_EMPTY),
            },
            "R1 Gi0/0/0/10: observed with both a managed and an unmanaged neighbor",
        ),
        # LLDP vs CDP disagree on one local interface.
        (
            {
                "R1": (lldp(("HOST-R5", "Gi0/0/0/10", "Gi0/0/0/6")), cdp(("HOST-R6", "Gi0/0/0/10", "Gi0/0/0/6"))),
                "R5": (LLDP_EMPTY, CDP_EMPTY),
                "R6": (LLDP_EMPTY, CDP_EMPTY),
            },
            "R1 Gi0/0/0/10: conflicting neighbor observations",
        ),
    ],
)
def test_ambiguous_link_evidence_is_fatal_and_leaves_candidate_untouched(lab_root, monkeypatch, capsys, outputs, message):
    _access(lab_root, ["R1", "R5", "R6"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr(outputs))
    committed_before = lab.load_topology("sample_lab", lab_root)
    session = _session(lab_root)
    capsys.readouterr()
    assert _run(session) is False
    out = capsys.readouterr().out
    assert message in out and "% Topology candidate not updated." in out
    assert session.definition_candidate is None and session.definition_kind is None
    assert session.mode == "global"
    assert lab.load_topology("sample_lab", lab_root) == committed_before


# ==========================================================================
# Protocol status priority rules (A8) -- derived, not per-row branches
# ==========================================================================

OK = "ok"
OK0 = "ok0"
OFF = "off"


def _xr_text(proto, state):
    if proto == "lldp":
        return {OK: lldp(("HOST-R2", "Gi0/0/0/1", "Gi0/0/0/1")), OK0: LLDP_EMPTY, OFF: LLDP_OFF}[state]
    return {OK: cdp(("HOST-R2", "Gi0/0/0/1", "Gi0/0/0/1")), OK0: CDP_EMPTY, OFF: CDP_OFF}[state]


@pytest.mark.parametrize(
    "lldp_state,cdp_state,expected",
    [
        (OK, OK, "complete"),
        (OK0, OK0, "complete"),
        (OK, OFF, "complete"),
        (OFF, OK, "complete"),
        (OFF, OK0, "complete"),
        (OK0, OFF, "complete"),
        (OFF, OFF, "no-protocol"),
    ],
)
def test_xr_protocol_priority_matrix(lab_root, monkeypatch, lldp_state, cdp_state, expected):
    _access(lab_root, ["R1", "R2"])
    outputs = {"R1": (_xr_text("lldp", lldp_state), _xr_text("cdp", cdp_state)), "R2": (LLDP_EMPTY, CDP_EMPTY)}
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr(outputs))
    result = discovery.discover_topology(lab_root)
    o = next(o for o in result.outcomes if o.device_id == "R1")
    assert sorted(result.devices) == ["R1", "R2"]  # registered either way
    if expected == "complete":
        # One protocol disabled beside a working one is a protocol state, not a warning.
        assert o.status == "Complete" and result.warnings == []
    else:
        assert o.status == "Partial" and result.warnings == ["R1: No usable neighbor discovery protocol."]


@pytest.mark.parametrize(
    "lldp_text,cdp_text",
    [
        ("% Invalid input detected at '^' marker.\n", CDP_EMPTY),  # LLDP command error/unrecognized
        (LLDP_EMPTY, "% Invalid input detected at '^' marker.\n"),  # CDP unrecognized (parser failure)
        (LLDP_EMPTY, ""),  # CDP no output at all
        (f"{LLDP_HDR}\nHOST-R2  Gi0/0/0/1  120  R  Gi0/0/0/1\nTotal entries displayed: 4\n", CDP_EMPTY),
    ],
)
def test_unrecognized_protocol_output_is_dropped_while_the_other_protocol_is_used(lab_root, monkeypatch, lldp_text, cdp_text):
    _access(lab_root, ["R1", "R2"])
    outputs = {"R1": (lldp_text, cdp_text), "R2": (LLDP_EMPTY, CDP_EMPTY)}
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr(outputs))
    result = discovery.discover_topology(lab_root)
    o = next(o for o in result.outcomes if o.device_id == "R1")
    # Parser fail-closed: the unusable protocol contributes nothing; the
    # other one is still used; the device is Partial with a warning.
    assert o.status == "Partial" and len(o.warnings) == 1 and o.protocol_usable
    assert o.observations == [] and result.managed_links == []


@pytest.mark.parametrize(
    "failure,session_ended",
    [
        (terminal.TerminalError("Timed out waiting for the device prompt."), False),
        (terminal.TerminalError("Session 'network-lab-discovery-R1' exited while waiting for expected output."), True),
    ],
)
def test_command_failure_mid_collection_keeps_the_completed_results(lab_root, monkeypatch, failure, session_ended):  # D05, C15
    _access(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_login", lambda d, c: "HOST-R1")
    monkeypatch.setattr(discovery, "_disable_terminal_paging", lambda d, p: None)

    def run(device_id, cmd, prompt_re=None, timeout=None):
        if cmd == "show cdp neighbors":
            raise failure
        return LLDP_EMPTY if cmd == "show lldp neighbors" else "x"

    monkeypatch.setattr(discovery, "_run_command", run)
    result = discovery.discover_topology(lab_root)
    o = result.outcomes[0]
    # Login and `show lldp neighbors` completed before the failure: the device
    # stays Connected, its LLDP result is kept, only CDP is unknown.
    assert (o.connection, o.lldp, o.lldp_ok, o.cdp, o.status) == ("connected", "enabled", True, "unknown", "Partial")
    assert o.session_ended is session_ended and len(result.warnings) == 1
    assert result.warnings[0].startswith("R1: ") and "Device 'R1'" not in result.warnings[0]


def test_iosxe_matrix(lab_root, monkeypatch):
    _access(lab_root, ["X1"], {"X1": "iosxe"})
    cases = [
        (LLDP_EMPTY, CDP_EMPTY, "Complete"),
        (LLDP_OFF, CDP_EMPTY, "Complete"),
        (LLDP_EMPTY, CDP_OFF, "Complete"),
        (LLDP_OFF, CDP_OFF, "Partial"),
    ]
    for lldp_text, cdp_text, expected in cases:
        monkeypatch.setattr(
            discovery,
            "_bootstrap_collect_iosxe",
            lambda d, c, include_l3=False, a=lldp_text, b=cdp_text: {
                "hostname": "HOST-X1",
                "show_version": "x",
                "show_lldp_neighbors": a,
                "show_cdp_neighbors": b,
            },
        )
        result = discovery.discover_topology(lab_root)
        assert result.outcomes[0].status == expected and list(result.devices) == ["X1"]


# ==========================================================================
# Optional L3 (A4, A5, A6, A10)
# ==========================================================================

L3_OK = f"{L3_HDR}\nGigabitEthernet0/0/0/1   10.0.12.1   Up Up default\n"


def test_l3_mode_collects_and_reports_interfaces(lab_root, monkeypatch, capsys):
    _access(lab_root, ["R1", "R2"])
    outputs = {
        "R1": (lldp(("HOST-R2", "Gi0/0/0/1", "Gi0/0/0/1")), CDP_OFF),
        "R2": (LLDP_EMPTY, CDP_OFF),
    }
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr(outputs, {"R1": L3_OK}))
    session = _session(lab_root)
    capsys.readouterr()
    assert _run(session, "discover topology l3")
    out = capsys.readouterr().out
    assert "L3 enrichment            : 2/2 devices, 1 interfaces" in out
    assert "L3 interfaces added      : 1" in out and "L3 enrichment            : Disabled" not in out
    assert session.definition_candidate["devices"]["R1"]["interfaces"] == {
        "GigabitEthernet0/0/0/1": {"ipv4_address": "10.0.12.1", "vrf": "default"}
    }


def test_l2_mode_output_shows_l3_disabled_and_existing_l3_kept(lab_root, monkeypatch, capsys):
    _access(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr({"R1": (LLDP_EMPTY, CDP_OFF)}))
    session = _session(lab_root)
    capsys.readouterr()
    assert _run(session)
    out = capsys.readouterr().out
    assert "L3 interfaces retained   : 0\nL3 enrichment            : Disabled (existing L3 data is kept)" in out


@pytest.mark.parametrize(
    "l3_text",
    [
        "",  # nothing
        "% Invalid input detected at '^' marker.\n",  # unsupported command
        "garbage without header\n",  # malformed
    ],
)
def test_l3_failure_is_a_warning_and_never_removes_or_blocks_anything(lab_root, monkeypatch, capsys, l3_text):  # L02, L03
    _access(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr({"R1": (LLDP_EMPTY, CDP_OFF)}, {"R1": l3_text}))
    session = _session(lab_root)
    capsys.readouterr()
    assert _run(session, "discover topology l3") is True
    out = capsys.readouterr().out
    assert "R1: L3 interface observations were not used" in out
    assert "L3 enrichment            : 0/1 devices, 0 interfaces" in out
    assert "% Topology candidate not updated." not in out and "Topology candidate unchanged." in out
    assert "interfaces" not in session.definition_candidate["devices"]["R1"]


def test_l3_valid_zero_interfaces_is_success(lab_root, monkeypatch):
    _access(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr({"R1": (LLDP_EMPTY, CDP_OFF)}, {"R1": f"{L3_HDR}\n"}))
    result = discovery.discover_topology(lab_root, include_l3=True)
    assert result.l3_enriched_device_count == 1 and result.l3_interface_count == 0


def test_l3_then_l2_keeps_the_l3_data(lab_root, monkeypatch):  # L04
    _access(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr({"R1": (LLDP_EMPTY, CDP_OFF)}, {"R1": L3_OK}))
    session = _session(lab_root)
    assert _run(session, "discover topology l3")
    l3_interfaces = session.definition_candidate["devices"]["R1"]["interfaces"]
    assert l3_interfaces
    assert _rediscover(session)  # discovery-generated candidate: re-discovery allowed
    assert session.definition_candidate["devices"]["R1"] == {"type": "iosxr", "interfaces": l3_interfaces}


def test_committed_l3_definition_then_l2_discovery(lab_root, monkeypatch):
    _access(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr({"R1": (LLDP_EMPTY, CDP_OFF)}, {"R1": L3_OK}))
    session = _session(lab_root)
    assert _run(session, "discover topology l3")
    assert _run(session, "commit")
    committed = lab.load_topology("sample_lab", lab_root)
    assert "interfaces" in committed["devices"]["R1"]

    session = _session(lab_root)
    assert _run(session)
    assert session.definition_candidate["devices"]["R1"]["interfaces"] == committed["devices"]["R1"]["interfaces"]
    assert lab.load_topology("sample_lab", lab_root) == committed  # unchanged before commit
    assert _run(session, "clear")
    assert session.definition_candidate == committed  # clear restores committed state
    assert lab.load_topology("sample_lab", lab_root) == committed

    assert _rediscover(session)
    assert _run(session, "commit")
    assert lab.load_topology("sample_lab", lab_root) == committed  # an L2 run never drops L3 data


# ==========================================================================
# Dirty-candidate protection / mutation tracking (A7, A16)
# ==========================================================================


@pytest.fixture
def one_device(lab_root, monkeypatch):
    _access(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr({"R1": (LLDP_EMPTY, CDP_OFF)}, {"R1": L3_OK}))
    return lab_root


def _edit(session, line):
    assert _run(session, line)


def test_manual_edit_after_discovery_is_rejected_even_if_reverted(one_device, capsys):
    session = _session(one_device)
    # An L3 run adds interface data, so the candidate really differs from the
    # committed topology (a no-change Discovery leaves nothing to protect).
    assert _run(session, "discover topology l3")
    assert session.definition_dirty()
    discovered_description = session.definition_candidate["description"]
    _edit(session, "description edited")
    capsys.readouterr()
    before = session.definition_candidate
    assert _rediscover(session) is True  # rejection is a printed refusal, not an error
    out = capsys.readouterr().out
    assert "% Topology discovery rejected." in out
    assert "% Manual edits were made since the last discovery." in out
    assert "% Use 'clear' to reset the candidate before retrying." in out
    assert "Discovering topology" not in out
    assert session.definition_candidate == before
    # Revert the edit by hand: still rejected (mutation history, not net diff).
    session.set_topology_description(discovered_description)
    assert session.definition_candidate["description"] == discovered_description
    capsys.readouterr()
    _rediscover(session)
    assert "Manual edits were made since the last discovery." in capsys.readouterr().out
    # clear resets tracking -> discovery allowed again.
    _edit(session, "clear")
    capsys.readouterr()
    _rediscover(session)
    assert "Topology candidate unchanged." in capsys.readouterr().out


def test_manual_revert_stays_dirty_by_value_semantics_unchanged(one_device):
    """Net-zero semantics are untouched: dirty is still value-based
    (candidate vs committed); the revision only gates re-Discovery."""
    session = _session(one_device)
    _edit(session, "topology sample_lab")
    assert not session.definition_dirty()
    original_description = session.definition_candidate["description"]
    _edit(session, "description tmp")
    assert session.definition_dirty()
    session.set_topology_description(original_description)
    assert not session.definition_dirty()  # net-zero: nothing to commit
    assert session._mutation_rev == 2
    # clean candidate: nothing to protect, so Discovery is allowed
    assert _rediscover(session)


def test_failed_manual_command_does_not_block_discovery(one_device, capsys):
    session = _session(one_device)
    assert _run(session)
    capsys.readouterr()
    assert _run(session, "no device NOPE") is False  # fails: no change made
    capsys.readouterr()
    assert _rediscover(session)
    assert "Topology candidate unchanged." in capsys.readouterr().out


def test_unchanged_manual_value_is_not_a_mutation(one_device):
    session = _session(one_device)
    assert _run(session)
    rev = session._mutation_rev
    session.set_topology_description(session.definition_candidate["description"])
    assert session._mutation_rev == rev
    assert _rediscover(session)


def test_target_definition_with_manual_edits_is_rejected(one_device, capsys):
    session = _session(one_device)
    _edit(session, "topology sample_lab")
    _edit(session, "description hand edit")
    _edit(session, "root")
    capsys.readouterr()
    _run(session)
    out = capsys.readouterr().out
    assert "Manual edits were made since the last discovery." in out
    assert session.definition_candidate["description"] == "hand edit"


def test_different_definition_dirty_is_rejected_with_guidance(one_device, capsys):
    session = _session(one_device)
    session.enter_scenario_definition("brand_new_scenario")
    session.definition_candidate["description"] = "dirty"
    session.go_to_global()
    capsys.readouterr()
    _run(session)
    out = capsys.readouterr().out
    assert "Uncommitted changes exist. Use 'commit' or 'clear'" in out
    assert session.definition_kind == "scenario"


def test_different_clean_definition_is_switched(one_device, capsys):
    lab.write_topology("other", {"name": "other", "description": "", "devices": {}, "links": []}, one_device)
    session = _session(one_device)
    _edit(session, "topology other")
    _edit(session, "root")
    assert _run(session)
    assert session.definition_name == "sample_lab" and session.mode == "topology"


def test_discovery_rejected_for_fresh_session_does_not_persist_anything(one_device):
    before = lab.load_topology("sample_lab", one_device)
    session = _session(one_device)
    assert _run(session)
    assert lab.load_topology("sample_lab", one_device) == before  # no persistence before commit
    session = cfgmod.CliSession(one_device)  # a fresh process: candidate gone
    assert session.definition_candidate is None
    assert lab.load_topology("sample_lab", one_device) == before


def test_rebuild_is_atomic_when_candidate_validation_fails(one_device, monkeypatch):
    session = _session(one_device)
    assert _run(session)
    snapshot = (session.definition_candidate, session.mode, session._mutation_rev, session._discovery_baseline_rev)
    original = lab.validate_topology_data

    def bad(name, data):
        raise lab.LabConfigError("synthetic validation failure")

    monkeypatch.setattr(lab, "validate_topology_data", bad)
    assert _run(session) is False
    monkeypatch.setattr(lab, "validate_topology_data", original)
    assert (session.definition_candidate, session.mode, session._mutation_rev, session._discovery_baseline_rev) == snapshot


def test_fatal_discovery_keeps_previous_candidate(one_device, monkeypatch):
    session = _session(one_device)
    assert _run(session)
    snapshot = (session.definition_candidate, session.mode, session._mutation_rev)
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr({"R1": (LLDP_OFF, CDP_OFF)}))
    assert _run(session) is False
    assert (session.definition_candidate, session.mode, session._mutation_rev) == snapshot


def test_commit_and_clear_reset_tracking(one_device):
    session = _session(one_device)
    assert _run(session)
    session.set_topology_description("m")
    assert session._mutation_rev > 0
    _edit(session, "clear")
    assert (session._mutation_rev, session._discovery_baseline_rev) == (0, 0)
    assert _rediscover(session)
    _edit(session, "commit")
    assert (session._mutation_rev, session._discovery_baseline_rev) == (0, 0)
    assert _rediscover(session)  # committed and clean: discovery allowed


def test_access_info_switch_reevaluates_target(lab_root, monkeypatch):
    lab.write_access_info("alt", {"name": "alt", "devices": {"R1": {"type": "iosxr", "address": "192.0.2.5"}}}, lab_root)
    _access(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_xr({"R1": (LLDP_EMPTY, CDP_OFF)}))
    session = _session(lab_root)
    assert _run(session)
    assert session.definition_name == "sample_lab"
    _edit(session, "clear")
    settings = lab.read_settings(lab_root)
    settings["active_access_info"] = "alt"
    lab.write_settings(settings, lab_root)
    session = _session(lab_root)
    assert _run(session)
    assert session.definition_name == "alt"
