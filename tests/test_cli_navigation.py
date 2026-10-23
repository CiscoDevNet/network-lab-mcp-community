# SPDX-License-Identifier: Apache-2.0
"""Tests for the IOS XR-style navigation refinement: `commit` staying in
the current mode, `root` (jump straight to global config, preserving
candidate state), `exit` (one level up, per cfgmod._EXIT_PARENT_MODE), and
`end` (guarded jump to EXEC, dirty-state protection unchanged). Also covers
running-config's new `access-info` selection (`no access-info` included)
and bare `show`/`<cr>` grammar-level availability outside EXEC."""

from __future__ import annotations

import pytest

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain


def _configured_access_device(lab_root, access_info_name="sample_lab", device_name="R1"):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition(access_info_name)
    session.enter_access_info_device(device_name)
    return session


# ---- commit stays in the current mode, for every mode ----


@pytest.mark.parametrize(
    "enter, mode",
    [
        (lambda s: None, "global"),
        (lambda s: setattr(s, "mode", "running"), "running"),
        (lambda s: s.apply_topology_definition_plan(s.plan_topology_definition("sample_lab")), "topology"),
        (
            lambda s: (s.apply_topology_definition_plan(s.plan_topology_definition("sample_lab")), s.enter_topology_device("R1")),
            "device",
        ),
        (lambda s: s.enter_access_info_definition("sample_lab"), "access_info"),
        (
            lambda s: (s.enter_access_info_definition("sample_lab"), s.enter_access_info_device("R1")),
            "access_device",
        ),
        (
            lambda s: (s.enter_access_info_definition("sample_lab"), s.enter_access_info_jump_host("jump1")),
            "access_jump_host",
        ),
        (lambda s: s.enter_scenario_definition("failover_test"), "scenario"),
        (lambda s: s.enter_reference_definition("iosxr_basics"), "reference"),
    ],
)
def test_commit_never_leaves_current_mode(lab_root, enter, mode):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    enter(session)
    assert session.mode == mode
    climain.h_commit(session, {})
    assert session.mode == mode  # commit never changes mode, dirty or clean


def test_commit_on_newly_created_object_remains_valid_afterward(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("brand_new")
    session.enter_access_info_device("R1")
    session.set_device_field("type", "host")
    session.set_device_field("address", "192.0.2.50")
    climain.h_commit(session, {})
    assert session.mode == "access_device"
    assert session.definition_dirty() is False
    assert climain.render_configuration_candidate(session) == ""
    assert "192.0.2.50" in climain.render_committed_definition(session)


# ---- root: jump to global, preserving candidate ----


def test_root_preserves_candidate_and_does_not_commit_or_clear(lab_root):
    session = _configured_access_device(lab_root)
    session.set_device_field("address", "192.0.2.99")
    climain.h_root(session, {})
    assert session.mode == "global"
    assert session.current_device_name is None
    assert session.definition_kind == "access_info"  # candidate still open
    assert session.definition_dirty() is True  # not committed
    assert lab.load_access_info("sample_lab", lab_root)["devices"]["R1"]["address"] != "192.0.2.99"  # not written


def test_root_from_jump_host_mode(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.enter_access_info_jump_host("jump1")
    climain.h_root(session, {})
    assert session.mode == "global"
    assert session.current_jump_host_name is None
    assert session.definition_kind == "access_info"  # candidate still open


def test_root_from_topology_device_mode(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.enter_topology_device("R1")
    climain.h_root(session, {})
    assert session.mode == "global"
    assert session.current_device_name is None


def test_root_from_running_mode(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    climain.h_root(session, {})
    assert session.mode == "global"


def test_root_not_offered_at_global_or_exec():
    ctx = grammar.CliContext()
    global_tokens = [line.token for line in grammar.help("global", "", ctx).lines]
    assert "root" not in global_tokens
    exec_tokens = [line.token for line in grammar.help("exec", "", ctx).lines]
    assert "root" not in exec_tokens


# ---- exit: one level up ----


def test_exit_from_access_device_goes_to_access_info(lab_root):
    session = _configured_access_device(lab_root)
    climain.h_exit(session, {})
    assert session.mode == "access_info"
    assert session.current_device_name is None


def test_exit_from_access_jump_host_goes_to_access_info(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.enter_access_info_jump_host("jump1")
    climain.h_exit(session, {})
    assert session.mode == "access_info"
    assert session.current_jump_host_name is None


def test_exit_from_topology_device_goes_to_topology(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.enter_topology_device("R1")
    climain.h_exit(session, {})
    assert session.mode == "topology"


def test_exit_from_definition_mode_goes_to_global(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    climain.h_exit(session, {})
    assert session.mode == "global"


def test_exit_from_running_goes_to_global(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    climain.h_exit(session, {})
    assert session.mode == "global"


def test_exit_never_commits_or_clears(lab_root):
    session = _configured_access_device(lab_root)
    session.set_device_field("address", "192.0.2.99")
    climain.h_exit(session, {})
    assert session.mode == "access_info"
    assert session.definition_dirty() is True
    assert lab.load_access_info("sample_lab", lab_root)["devices"]["R1"]["address"] != "192.0.2.99"


# ---- end: guarded jump to EXEC ----


def test_end_from_nested_device_returns_to_exec_when_clean(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.enter_access_info_device("R1")
    climain.h_end(session, {})
    assert session.mode == "exec"


def test_end_blocked_while_dirty(lab_root, capsys):
    session = _configured_access_device(lab_root)
    session.set_device_field("address", "192.0.2.99")
    climain.h_end(session, {})
    assert session.mode == "access_device"  # unchanged: blocked
    assert "Uncommitted changes" in capsys.readouterr().out


def test_end_never_implicitly_commits(lab_root):
    session = _configured_access_device(lab_root)
    session.set_device_field("address", "192.0.2.99")
    climain.h_end(session, {})  # blocked, candidate untouched
    assert lab.load_access_info("sample_lab", lab_root)["devices"]["R1"]["address"] != "192.0.2.99"


def test_global_exit_and_end_both_guarded_to_exec(lab_root):
    # HANDLERS["global.exit"] is h_end itself (global's "exit" and "end" are
    # equivalent -- both guarded jumps to EXEC), not the generic h_exit
    # used by every other mode's "exit".
    assert climain.HANDLERS["global.exit"] is climain.h_end

    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    climain.HANDLERS["global.exit"](session, {})
    assert session.mode == "exec"


# ---- clear: whole-session discard, correct fallback ----


def test_clear_after_navigating_away_and_back_still_discards(lab_root):
    session = _configured_access_device(lab_root)
    session.set_device_field("address", "192.0.2.99")
    climain.h_root(session, {})  # candidate preserved across root
    session.clear()
    assert session.overall_dirty() is False
    assert not lab.access_info_exists("brand_new_unrelated", lab_root)


# ---- bare `show` / `<cr>` outside EXEC ----


@pytest.mark.parametrize(
    "mode",
    ["global", "running", "topology", "device", "access_info", "access_device", "access_jump_host", "scenario", "reference"],
)
def test_show_help_includes_cr_outside_exec(mode):
    ctx = grammar.CliContext()
    result = grammar.help(mode, "show ", ctx)
    assert result.show_cr is True


def test_show_cr_absent_in_exec():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "show ", ctx)
    assert result.show_cr is False


def test_bare_show_equals_show_configuration_in_access_device(lab_root):
    session = _configured_access_device(lab_root)
    session.set_device_field("address", "192.0.2.99")
    bare = grammar.parse("access_device", "show")
    explicit = grammar.parse("access_device", "show configuration")
    assert bare.ok and explicit.ok
    assert bare.action == explicit.action


def test_bare_show_not_executable_in_exec():
    result = grammar.parse("exec", "show")
    assert not result.ok
    assert result.error.kind == "incomplete"


# ---- running-config access-info selection ----


def test_running_config_selects_access_info(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    with pytest.raises(cfgmod.ConfigError):
        session.select_access_info("does-not-exist")
    session.select_access_info("sample_lab")
    assert session.settings_candidate["active_access_info"] == "sample_lab"


def test_no_access_info_clears_candidate_selection(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    assert session.settings_candidate["active_access_info"] == "sample_lab"
    session.clear_access_info_selection()
    assert "active_access_info" not in session.settings_candidate


def test_committed_view_unaffected_by_candidate_access_info_change(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    session.clear_access_info_selection()
    # Candidate change alone must not affect terminal resolution.
    _, access = lab.get_device("R1")
    assert access["address"] == "192.0.2.11"


def test_commit_persists_access_info_selection_and_resolver_picks_it_up(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    lab.write_access_info("alt_lab", {"name": "alt_lab", "devices": {"R1": {"type": "iosxr", "address": "10.0.0.9"}}}, lab_root)

    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    session.select_access_info("alt_lab")
    session.commit()

    assert lab.read_settings(lab_root)["active_access_info"] == "alt_lab"
    _, access = lab.get_device("R1")
    assert access["address"] == "10.0.0.9"


def test_commit_no_access_info_then_terminal_open_fails_closed(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    session.clear_access_info_selection()
    session.commit()

    assert "active_access_info" not in lab.read_settings(lab_root)
    with pytest.raises(lab.LabConfigError, match="No access-info is selected"):
        lab.get_device("R1")


def test_show_running_config_includes_access_info_section(lab_root):
    session = cfgmod.CliSession(lab_root)
    text = climain.render_committed_running_config(session)
    assert "access-info" in text
    assert "sample_lab" in text
