# SPDX-License-Identifier: Apache-2.0
"""Name-less Running access-info submode (`config-running-access-info`).

`config-running# access-info` (bare) enters a shared submode where typing a
definition name selects it through the same Selection method as the existing
`access-info <name>` (which keeps its named submode, `description`, etc.).
Access-info only: topology/scenario/reference are unchanged.

First section = characterization of the pre-existing named path (Case A:
`access-info <name>` itself performs the Selection; Mode Transition is a
separate step). Isolated `lab_root` fixture only; synthetic definitions."""

from __future__ import annotations

import os

import pytest

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain

COMMON = "running_access_info_common"
real_replace = os.replace


@pytest.fixture()
def root(lab_root):
    for name in ("ai_b", "show", "description", "no", "c", "topology"):
        lab.write_access_info(name, {"name": name, "devices": {}}, lab_root)
    return lab_root


def _session(root, *lines, capsys=None):
    s = cfgmod.CliSession(root)
    s.enter_configure()
    for line in ("running-config", *lines):
        climain.execute_command_line(s, line)
    return s


def _run(s, line, capsys):
    capsys.readouterr()
    ok = climain.execute_command_line(s, line)
    out = capsys.readouterr().out
    if line == "commit":
        ok = "Commit complete." in out or "No changes to commit." in out
    return ok, out


def _help(s, text):
    r = grammar.help(s.mode, text, climain.build_context(s))
    return [l.token for l in r.lines], r.show_cr


def _tab(s, text):
    return grammar.complete(s.mode, text, climain.build_context(s)).candidates


def _selected(s):
    return s.settings_candidate.get("active_access_info")


def _paste(s, text, capsys):
    capsys.readouterr()
    climain.execute_input_block(s, text)
    return capsys.readouterr().out


# ------------------------------------------------ characterization (old path)


def test_old_path_selection_is_performed_by_access_info_name_and_enters_named_mode(root, capsys):
    s = _session(root)
    assert s.settings_candidate["active_access_info"] == "sample_lab"
    _run(s, "access-info ai_b", capsys)
    assert _selected(s) == "ai_b" and s.mode == "running_access_info"
    assert s.current_running_entry_name == "ai_b"
    assert climain.prompt_text(s) == "network-lab(config-running-access-info-ai_b)# "


def test_old_path_same_name_and_a_b_a_are_clean(root, capsys):
    s = _session(root)
    _run(s, "access-info sample_lab", capsys)
    assert not s.settings_dirty()
    _run(s, "exit", capsys)
    _run(s, "access-info ai_b", capsys)
    assert s.settings_dirty()
    assert climain.render_configuration_candidate(s) == "!\n access-info\n  ai_b\n!"
    _run(s, "exit", capsys)
    _run(s, "access-info sample_lab", capsys)
    assert not s.settings_dirty() and climain.render_configuration_candidate(s) == ""


def test_old_path_description_targets_entered_name_only(root, capsys):
    s = _session(root, "access-info ai_b")
    _run(s, "description hello", capsys)
    assert s.settings_candidate["running_descriptions"] == {"access_info": {"ai_b": "hello"}}
    _run(s, "no description", capsys)
    assert not s.settings_candidate["running_descriptions"].get("access_info")


def test_old_path_commit_and_clear_stay_in_named_mode(root, capsys):
    s = _session(root, "access-info ai_b")
    _run(s, "commit", capsys)
    assert s.mode == "running_access_info"
    s2 = _session(root, "access-info ai_b")  # ai_b is committed-active now
    _run(s2, "clear", capsys)
    assert s2.mode == "running_access_info"


def test_keyword_named_definitions_selectable_from_parent(root, capsys):
    s = _session(root)
    for name in ("show", "description", "no", "c"):
        assert _run(s, f"access-info {name}", capsys)[0]
        assert _selected(s) == name and s.mode == "running_access_info"
        _run(s, "exit", capsys)


# ------------------------------------------------------------- grammar / help


def test_bare_access_info_enters_common_submode_without_touching_candidate(root, capsys):
    s = _session(root)
    before = (dict(s.settings_candidate), s.settings_dirty())
    assert _run(s, "access-info", capsys)[0]
    assert s.mode == COMMON and s.current_running_entry_name is None
    assert climain.prompt_text(s) == "network-lab(config-running-access-info)# "
    assert (s.settings_candidate, s.settings_dirty()) == before


def test_parent_help_lists_names_and_cr_global_does_not(root, capsys):
    s = _session(root)
    tokens, cr = _help(s, "access-info ")
    assert tokens == sorted(lab.list_access_info_names(root)) and cr is True
    g = cfgmod.CliSession(root)
    g.enter_configure()
    tokens, cr = _help(g, "access-info ")
    assert cr is False and "<name>" in tokens
    assert _run(g, "access-info", capsys) == (False, "% Incomplete command.\n")
    assert g.mode == "global"


def test_other_kinds_bare_command_in_running_enters_their_own_common_submode(root, capsys):
    """Superseded: topology/scenario/reference used to stay Incomplete in
    Running (access-info-only provisional spec); they now have their own
    name-less submodes (tests/test_running_common_submodes.py). Global
    configuration is unchanged."""
    s = _session(root)
    for kw, mode in (("topology", "running_topology_common"), ("scenario", "running_scenario_common"), ("reference", "running_reference_common")):
        assert _run(s, kw, capsys)[0] and s.mode == mode
        _run(s, "exit", capsys)
        assert _help(s, f"{kw} ")[1] is True
    g = cfgmod.CliSession(root)
    g.enter_configure()
    for kw in ("access-info", "topology", "scenario", "reference"):
        ok, out = _run(g, kw, capsys)
        assert not ok and "Incomplete command" in out and g.mode == "global"
    assert "running_topology" in cfgmod.RUNNING_ENTRY_MODES  # named modes unchanged


def test_common_mode_help_lists_names_and_only_valid_keywords(root, capsys):
    s = _session(root, "access-info")
    tokens, cr = _help(s, "")
    assert cr is False
    keywords = {"show", "no", "clear", "commit", "root", "end", "exit", "help"}
    assert keywords <= set(tokens)
    assert set(lab.list_access_info_names(root)) <= set(tokens)
    # `no` is now a real keyword; `description` is still only a definition name
    assert tokens.count("no") == 2 and tokens.count("description") == 1
    help_lines = grammar.help(s.mode, "", climain.build_context(s)).lines
    assert dict((l.token, l.description) for l in help_lines)["ai_b"] == "Access-info definition name"


def test_common_mode_prefix_tab_exact_case_and_zero_candidates(root, capsys):
    s = _session(root, "access-info")
    assert _help(s, "ai") == (["ai_b"], False)
    assert _help(s, "ai_b") == (["ai_b"], True)
    assert _help(s, "AI_B") == ([], False)
    assert _tab(s, "ai") == ["ai_b"] and _tab(s, "AI") == []
    ctx = grammar.CliContext(running_access_info_names=())
    assert [l.token for l in grammar.help(COMMON, "zz", ctx).lines] == []
    assert grammar.complete(COMMON, "zz", ctx).candidates == []


# ---------------------------------------------------- selection / candidate


def test_common_selection_matches_old_path_state_and_stays(root, capsys):
    old = _session(root, "access-info ai_b")
    new = _session(root, "access-info")
    assert _run(new, "ai_b", capsys)[0] and new.mode == COMMON
    assert new.settings_candidate == old.settings_candidate
    assert new.settings_dirty() == old.settings_dirty() is True
    assert climain.render_configuration_candidate(new) == "ai_b"  # kind-scoped Common form
    assert climain.render_configuration_candidate(old) == "!\n access-info\n  ai_b\n!"
    # replace by another name; single selection
    assert _run(new, "sample_lab", capsys)[0]
    assert _selected(new) == "sample_lab" and not new.settings_dirty()


def test_common_same_name_and_a_b_a_follow_old_path(root, capsys):
    s = _session(root, "access-info")
    _run(s, "sample_lab", capsys)
    assert not s.settings_dirty()
    _run(s, "ai_b", capsys)
    _run(s, "sample_lab", capsys)
    assert not s.settings_dirty() and climain.render_configuration_candidate(s) == ""


def test_common_invalid_name_keeps_candidate_mode_and_creates_nothing(root, capsys):
    s = _session(root, "access-info")
    before = dict(s.settings_candidate)
    ok, out = _run(s, "nope", capsys)
    assert not ok and "does not exist" in out
    assert s.mode == COMMON and s.settings_candidate == before
    assert not lab.access_info_exists("nope", root)
    ok, out = _run(s, "sample_lab extra", capsys)
    assert not ok and s.mode == COMMON and s.settings_candidate == before


def test_common_mode_blocks_description_editing(root, capsys):
    """`description <text>` is never an edit here. (`no ...` is now the
    selection-removal command -- see test_running_access_info_common_no.py.)"""
    s = _session(root, "access-info", )
    before = dict(s.settings_candidate)
    ok, out = _run(s, "description foo", capsys)
    assert not ok and "Use the named mode: access-info <name>" in out
    assert s.settings_candidate == before and s.mode == COMMON
    assert not [a for a in HANDLERS_KEYS() if a.startswith(COMMON) and "description" in a]
    with pytest.raises(cfgmod.ConfigError):
        s.set_current_running_description("x")
    assert s.settings_candidate == before


def HANDLERS_KEYS():
    return climain.HANDLERS.keys()


def test_keyword_collisions_keywords_win_names_via_parent(root, capsys):
    s = _session(root, "access-info")
    for word in ("show", "SHOW", "sh"):  # keyword / case-insensitive / unique prefix
        before = _selected(s)
        _run(s, word, capsys)
        assert _selected(s) == before and s.mode == COMMON
    for word in ("c", "e"):  # ambiguous keyword abbreviations are rejected
        ok, out = _run(s, word, capsys)
        assert not ok and "Ambiguous" in out and _selected(s) == "sample_lab"
    # bare `description` / `topology` are plain definition names here
    assert _run(s, "description", capsys)[0] and _selected(s) == "description"
    # a keyword-named definition is always selectable from the parent
    _run(s, "exit", capsys)
    for name in ("show", "no", "c"):
        assert _run(s, f"access-info {name}", capsys)[0] and _selected(s) == name
        _run(s, "exit", capsys)


# -------------------------------------------------------- mode transitions


def test_dirty_running_candidate_can_enter_and_exit_keeping_candidate(root, capsys):
    s = _session(root, "no access-info")
    assert _run(s, "access-info", capsys)[0] and s.mode == COMMON
    assert s.settings_dirty() and "active_access_info" not in s.settings_candidate
    assert _run(s, "exit", capsys)[0] and s.mode == "running"
    assert s.settings_dirty()
    _run(s, "access-info", capsys)
    _run(s, "ai_b", capsys)
    assert _run(s, "exit", capsys)[0] and _selected(s) == "ai_b"


def test_definition_dirty_still_enters_running_and_common_mode(root, capsys):
    s = cfgmod.CliSession(root)
    s.enter_configure()
    _run(s, "no access-info ai_b", capsys)  # pending definition deletion
    assert _run(s, "running-config", capsys)[0]
    assert _run(s, "access-info", capsys)[0] and s.mode == COMMON
    assert "ai_b" not in climain.build_context(s).running_access_info_names  # existing rule kept


def test_root_end_commit_clear_in_common_mode(root, capsys):
    s = _session(root, "access-info", "ai_b")
    ok, out = _run(s, "end", capsys)
    assert not ok or "Uncommitted" in out
    assert s.mode == COMMON  # dirty end is refused (existing rule)
    _run(s, "clear", capsys)
    assert s.mode == COMMON and _selected(s) == "sample_lab"
    _run(s, "ai_b", capsys)
    assert _run(s, "commit", capsys)[0] and s.mode == COMMON
    assert lab.read_settings(root)["active_access_info"] == "ai_b"
    _run(s, "root", capsys)
    assert s.mode == "global"
    s2 = _session(root, "access-info")
    _run(s2, "end", capsys)
    assert s2.mode == "exec"


def test_commit_failure_keeps_common_mode_and_candidate(root, capsys, monkeypatch):
    s = _session(root, "access-info", "ai_b")
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    ok, out = _run(s, "commit", capsys)
    assert "Commit failed" in out and s.mode == COMMON and _selected(s) == "ai_b"
    monkeypatch.setattr(os, "replace", real_replace)
    assert lab.read_settings(root)["active_access_info"] == "sample_lab"
    assert _run(s, "commit", capsys)[0]
    assert lab.read_settings(root)["active_access_info"] == "ai_b"  # restart-visible


def test_show_and_show_configuration_in_common_mode(root, capsys):
    s = _session(root, "access-info", "ai_b")
    assert _run(s, "show configuration", capsys)[1].strip() == "ai_b"
    out = _run(s, "show running-config", capsys)[1]
    assert " access-info\n  sample_lab\n" in out and "ai_b" not in out  # committed selection


# -------------------------------------------------------------- paste replay


def test_old_named_script_with_description_still_replays(root, capsys):
    s = _session(root)
    out = _paste(s, "access-info ai_b\n description Example\n exit\n!", capsys)
    assert out.strip() == ""
    assert _selected(s) == "ai_b" and s.mode == "running"
    assert s.settings_candidate["running_descriptions"] == {"access_info": {"ai_b": "Example"}}


def test_new_common_block_replays_and_bang_closes_it(root, capsys):
    s = _session(root)
    out = _paste(s, "access-info\n ai_b\n!", capsys)
    assert out.strip() == "" and _selected(s) == "ai_b" and s.mode == "running"


def test_show_configuration_output_replays_from_running_and_global(root, capsys):
    src = _session(root, "access-info", "ai_b")
    text = climain.render_configuration_candidate(src)
    assert text == "ai_b"
    # Common output replays into the same Common submode ...
    s = _session(root, "access-info")
    assert _paste(s, text, capsys).strip() == "" and s.mode == COMMON
    assert _selected(s) == "ai_b" and s.settings_dirty()
    # ... while the running-scope output (taken in config-running) replays from running/global
    run_src = _session(root)
    _run(run_src, "access-info ai_b", capsys)
    _run(run_src, "exit", capsys)
    rtext = climain.render_configuration_candidate(run_src)
    for start in ("running", "global"):
        s = cfgmod.CliSession(root)
        s.enter_configure()
        if start == "running":
            climain.execute_command_line(s, "running-config")
        out = _paste(s, rtext if start == "running" else "running-config\n" + rtext, capsys)
        assert _selected(s) == "ai_b", (start, out)
        assert s.settings_dirty()


def test_leading_bang_does_not_leave_running_mode(root, capsys):
    s = _session(root)
    _paste(s, "!\n access-info\n  ai_b\n!", capsys)
    assert _selected(s) == "ai_b" and s.mode == "running"


def test_show_running_config_output_replays_as_net_zero_and_never_selects_colliding_names(root, capsys):
    """Every section heading is now a valid bare command, and each `!`
    closes its submode, so a heading is never read as a definition name --
    here an access-info literally named `topology` exists."""
    s = _session(root)
    text = "access-info\n  sample_lab\n!\n topology\n  sample_lab\n!\n scenario\n  sample\n!\n reference\n  sample\n!"
    out = _paste(s, text, capsys)
    assert out.strip() == ""
    assert _selected(s) == "sample_lab" and not s.settings_dirty() and s.mode == "running"


def test_bare_access_info_without_closing_bang_is_contained_in_common_mode(root, capsys):
    """Without a `!`, following lines are interpreted by the common submode
    (names only): an unknown word errors and stops; a word that IS a
    definition name selects it -- the documented IC-3 effect."""
    s = _session(root)
    out = _paste(s, "access-info\n ai_b\n scenario\n sample", capsys)
    assert "does not exist" in out  # `scenario` is not an access-info name
    assert _selected(s) == "ai_b" and s.mode == COMMON


def test_mode_identification_is_distinct_from_named_modes(root, capsys):
    assert COMMON not in cfgmod.RUNNING_ENTRY_MODES
    assert cfgmod._EXIT_PARENT_MODE[COMMON] == "running"
    s = _session(root, "access-info")
    assert climain.prompt_text(s) != climain.prompt_text(_session(root, "access-info ai_b"))
    assert grammar.MODE_ROOTS[COMMON] is not grammar.MODE_ROOTS["running_access_info"]
