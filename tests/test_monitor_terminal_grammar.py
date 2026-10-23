# SPDX-License-Identifier: Apache-2.0
"""`terminal monitor <device-id>` grammar/CLI-dispatch tests.

Covers only the grammar SSOT (parsing, abbreviation, `?`, Tab, `<cr>`,
EXEC-only availability) and the dynamic target-eligibility rule
(committed-topology-or-existing-session, never an uncommitted candidate,
never Discovery). Nothing here enters the actual monitor UI -- see
test_monitor_terminal.py for that.

Cisco IOS/IOS XE/IOS XR word order: `terminal monitor <device-id>`, not
the old `monitor terminal <device-id>` (see test_old_monitor_terminal_*
below -- the old word order must no longer parse at all).

Uses the isolated `lab_root` fixture and monkeypatches
`terminal.list_device_sessions` where a test doesn't care about real tmux
state, so these stay fast and hardware-independent."""

from __future__ import annotations

from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain


def make_ctx(**kwargs) -> grammar.CliContext:
    return grammar.CliContext(**kwargs)


# ==========================================================================
# Grammar
# ==========================================================================


def test_terminal_monitor_parses():
    result = grammar.parse("exec", "terminal monitor R1")
    assert result.ok
    assert result.action == "exec.monitor_terminal"
    assert result.args == {"device_id": "R1"}


def test_terminal_monitor_abbreviation_resolves():
    result = grammar.parse("exec", "term mon R1")
    assert result.ok
    assert result.action == "exec.monitor_terminal"


def test_terminal_monitor_bare_is_incomplete():
    result = grammar.parse("exec", "terminal monitor")
    assert not result.ok
    assert result.error.kind == "incomplete"


def test_terminal_bare_is_incomplete():
    result = grammar.parse("exec", "terminal")
    assert not result.ok
    assert result.error.kind == "incomplete"


def test_terminal_monitor_unknown_device_still_parses_grammar_defers_validation():
    # Grammar accepts any identifier token -- existence is the handler's
    # job (h_monitor_terminal), matching every other object identifier in
    # this CLI (see cli/config.py).
    result = grammar.parse("exec", "terminal monitor UNKNOWN_DEVICE")
    assert result.ok
    assert result.args == {"device_id": "UNKNOWN_DEVICE"}


def test_terminal_help_lists_monitor():
    ctx = make_ctx()
    tokens = [line.token for line in grammar.help("exec", "terminal ", ctx).lines]
    assert tokens == ["monitor"]


def test_terminal_monitor_help_lists_dynamic_targets():
    ctx = make_ctx(monitor_terminal_device_ids=("R1", "R2"))
    tokens = [line.token for line in grammar.help("exec", "terminal monitor ", ctx).lines]
    assert sorted(tokens) == ["R1", "R2"]


def test_terminal_monitor_device_shows_cr():
    ctx = make_ctx(monitor_terminal_device_ids=("R1",))
    result = grammar.help("exec", "terminal monitor R1 ", ctx)
    assert result.lines == []
    assert result.show_cr is True


def test_terminal_monitor_tab_completion_uses_dynamic_targets():
    ctx = make_ctx(monitor_terminal_device_ids=("R1", "R5"))
    result = grammar.complete("exec", "terminal monitor ", ctx)
    assert set(result.candidates) == {"R1", "R5"}


def test_monitor_device_ids_are_case_sensitive():
    ctx = make_ctx(monitor_terminal_device_ids=("R1",))
    result = grammar.complete("exec", "terminal monitor r", ctx)
    assert result.candidates == []


def test_terminal_monitor_exec_only():
    for mode in ("global", "running", "topology", "access_info", "scenario", "reference"):
        assert not grammar.parse(mode, "terminal monitor R1").ok


def test_terminal_root_keyword_ambiguity_unaffected():
    # "terminal" starts with "t" -- no other EXEC root keyword does, so no
    # new ambiguity was introduced.
    result = grammar.parse("exec", "t")
    assert not result.ok
    assert result.error.kind != "ambiguous"


# ==========================================================================
# Old word order must no longer be a valid command
# ==========================================================================


def test_old_monitor_terminal_no_longer_parses():
    result = grammar.parse("exec", "monitor terminal R1")
    assert not result.ok


def test_old_monitor_terminal_help_not_offered():
    ctx = make_ctx()
    tokens = [line.token for line in grammar.help("exec", "", ctx).lines]
    assert "monitor" not in tokens


def test_old_monitor_bare_not_offered_at_root():
    ctx = make_ctx()
    result = grammar.complete("exec", "mon", ctx)
    assert result.candidates == []


# ==========================================================================
# Dynamic target provider
# ==========================================================================


def test_committed_topology_device_is_offered(lab_root, monkeypatch):
    monkeypatch.setattr("network_lab_mcp.terminal.list_device_sessions", lambda: [])
    session = cfgmod.CliSession(lab_root)
    ctx = climain.build_context(session)
    assert set(ctx.monitor_terminal_device_ids) == {"R1", "R2"}


def test_uncommitted_candidate_device_is_not_leaked(lab_root, monkeypatch):
    monkeypatch.setattr("network_lab_mcp.terminal.list_device_sessions", lambda: [])
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.definition_candidate["devices"]["R99_UNCOMMITTED"] = {"type": "iosxr"}
    ctx = climain.build_context(session)
    assert "R99_UNCOMMITTED" not in ctx.monitor_terminal_device_ids
    assert set(ctx.monitor_terminal_device_ids) == {"R1", "R2"}


def test_existing_session_device_outside_topology_is_offered(lab_root, monkeypatch):
    monkeypatch.setattr(
        "network_lab_mcp.terminal.list_device_sessions",
        lambda: [{"device": "R9_SESSION_ONLY", "session_name": "network-lab-device-R9_SESSION_ONLY", "state": "running"}],
    )
    session = cfgmod.CliSession(lab_root)
    ctx = climain.build_context(session)
    assert "R9_SESSION_ONLY" in ctx.monitor_terminal_device_ids
    assert set(ctx.monitor_terminal_device_ids) == {"R1", "R2", "R9_SESSION_ONLY"}


def test_discovery_only_device_is_offered(lab_root, monkeypatch):
    # A device being discovered for the first time may not yet
    # be in the committed topology or have a managed session at all -- its
    # live Discovery activity must still be a valid monitor target.
    monkeypatch.setattr("network_lab_mcp.terminal.list_device_sessions", lambda: [])
    monkeypatch.setattr("network_lab_mcp.terminal.list_discovery_device_ids", lambda: ["R9_DISCOVERY_ONLY"])
    session = cfgmod.CliSession(lab_root)
    ctx = climain.build_context(session)
    assert "R1" in ctx.monitor_terminal_device_ids  # sanity: fixture still works
    assert "R9_DISCOVERY_ONLY" in ctx.monitor_terminal_device_ids
    assert set(ctx.monitor_terminal_device_ids) == {"R1", "R2", "R9_DISCOVERY_ONLY"}


def test_no_extra_devices_offered_when_no_discovery_sessions_exist(lab_root, monkeypatch):
    monkeypatch.setattr("network_lab_mcp.terminal.list_device_sessions", lambda: [])
    monkeypatch.setattr("network_lab_mcp.terminal.list_discovery_device_ids", lambda: [])
    session = cfgmod.CliSession(lab_root)
    ctx = climain.build_context(session)
    assert set(ctx.monitor_terminal_device_ids) == {"R1", "R2"}


def test_unknown_arbitrary_device_prints_bounded_error(lab_root, monkeypatch, capsys):
    monkeypatch.setattr("network_lab_mcp.terminal.list_device_sessions", lambda: [])
    session = cfgmod.CliSession(lab_root)
    ok = climain.execute_command_line(session, "terminal monitor TOTALLY_UNKNOWN")
    assert ok is False
    out = capsys.readouterr().out
    assert "TOTALLY_UNKNOWN" in out
    assert "% " in out
