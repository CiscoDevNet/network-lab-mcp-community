"""`no <name>` in the name-less `config-running-access-info` submode:
unselect the access-info currently selected in the Running *candidate*
(exact name only), through the same Candidate update as
`config-running# no access-info`. Never deletes a definition, never touches
running_descriptions, never reaches the named-mode `no description`.
Isolated `lab_root` fixture; synthetic definitions only."""

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
    for name in ("ai_b", "description"):
        lab.write_access_info(name, {"name": name, "devices": {}}, lab_root)
    return lab_root


def _session(root, *lines):
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


def _sel(s):
    return s.settings_candidate.get("active_access_info")


def _paste(s, text, capsys):
    capsys.readouterr()
    climain.execute_input_block(s, text)
    return capsys.readouterr().out


def _defs(root):
    return {n: (root / "access-info" / f"{n}.yaml").read_bytes() for n in lab.list_access_info_names(root)}


# ------------------------------------------------------------ grammar / help


def test_question_mark_shows_no_after_show_with_description(root):
    s = _session(root, "access-info")
    lines = grammar.help(s.mode, "", climain.build_context(s)).lines
    tokens = [l.token for l in lines]
    assert tokens[:3] == ["show", "no", "clear"]
    assert dict((l.token, l.description) for l in lines)["no"] == "Remove selected access-info"


def test_no_help_lists_only_candidate_selection_without_cr(root, capsys):
    s = _session(root, "access-info")
    assert _help(s, "no ") == (["sample_lab"], False)
    assert [l.description for l in grammar.help(s.mode, "no ", climain.build_context(s)).lines] == ["Currently selected access-info"]
    _run(s, "ai_b", capsys)  # candidate differs from committed
    assert _help(s, "no ") == (["ai_b"], False)
    assert _tab(s, "no ") == ["ai_b"]
    _run(s, "clear", capsys)  # back to committed -> recomputed, not cached
    assert _help(s, "no ") == (["sample_lab"], False)


def test_no_prefix_tab_exact_and_case(root, capsys):
    s = _session(root, "access-info")
    assert _help(s, "no sa") == (["sample_lab"], False)
    assert _tab(s, "no sa") == ["sample_lab"] and _tab(s, "no SA") == []
    assert _help(s, "no sample_lab") == (["sample_lab"], True)
    assert _help(s, "no SAMPLE_LAB") == ([], False)


def test_no_help_has_zero_candidates_when_unselected(root, capsys):
    s = _session(root, "access-info")
    _run(s, "no sample_lab", capsys)
    assert _help(s, "no ") == ([], False) and _tab(s, "no ") == []


def test_bare_no_is_incomplete_and_changes_nothing(root, capsys):
    s = _session(root, "access-info")
    before = dict(s.settings_candidate)
    ok, out = _run(s, "no", capsys)
    assert not ok and "Incomplete command" in out
    assert s.settings_candidate == before and s.mode == COMMON and not s.settings_dirty()


# ------------------------------------------------------------ deselection


def test_no_selected_name_unselects_stays_and_keeps_files(root, capsys):
    defs = _defs(root)
    s = _session(root, "access-info")
    ok, out = _run(s, "no sample_lab", capsys)
    assert ok and out == ""
    assert _sel(s) is None and s.mode == COMMON and s.settings_dirty()
    assert _defs(root) == defs
    assert climain.render_configuration_candidate(s) == "no access-info"


def test_same_candidate_update_as_parent_no_access_info(root, capsys):
    a = _session(root, "access-info")
    _run(a, "no sample_lab", capsys)
    b = _session(root)
    _run(b, "no access-info", capsys)
    assert a.settings_candidate == b.settings_candidate
    assert a.settings_dirty() == b.settings_dirty() is True
    assert climain.render_configuration_candidate(a) == climain.render_configuration_candidate(b)


@pytest.mark.parametrize("bad", ["ai_b", "sample", "unknown", "SAMPLE_LAB", "sample_lab_x"])
def test_wrong_name_is_rejected_without_change(root, capsys, bad):
    defs = _defs(root)
    s = _session(root, "access-info")
    before = dict(s.settings_candidate)
    ok, out = _run(s, f"no {bad}", capsys)
    assert not ok and f"Access information '{bad}' is not currently selected." in out
    assert s.settings_candidate == before and not s.settings_dirty() and s.mode == COMMON
    assert _defs(root) == defs


def test_no_when_unselected_is_rejected(root, capsys):
    s = _session(root, "access-info")
    _run(s, "no sample_lab", capsys)
    before = dict(s.settings_candidate)
    ok, out = _run(s, "no sample_lab", capsys)
    assert not ok and "not currently selected" in out
    assert s.settings_candidate == before and s.mode == COMMON


# -------------------------------------------------- description isolation


def test_no_description_is_unselect_only_when_selected_name_is_description(root, capsys):
    s = _session(root, "access-info", "description")
    s.settings_candidate["running_descriptions"] = {"access_info": {"sample_lab": "keep me"}}
    snapshot = dict(s.settings_candidate["running_descriptions"])
    assert _sel(s) == "description"
    ok, out = _run(s, "no description", capsys)
    assert ok and _sel(s) is None and s.mode == COMMON  # Case A: selection removal
    assert s.settings_candidate["running_descriptions"] == snapshot


def test_no_description_rejected_on_name_mismatch_or_unselected(root, capsys):
    s = _session(root, "access-info")
    s.settings_candidate["running_descriptions"] = {"access_info": {"sample_lab": "keep me"}}
    snapshot = dict(s.settings_candidate)
    ok, out = _run(s, "no description", capsys)  # Case B
    assert not ok and "not currently selected" in out and "Use the named mode" not in out
    assert s.settings_candidate == snapshot and _sel(s) == "sample_lab"
    _run(s, "no sample_lab", capsys)
    snapshot = dict(s.settings_candidate)
    ok, out = _run(s, "no description", capsys)  # Case C
    assert not ok and s.settings_candidate == snapshot and s.mode == COMMON


def test_common_no_never_reaches_named_description_handlers(root, capsys, monkeypatch):
    calls = []
    monkeypatch.setitem(climain.HANDLERS, "running_access_info.clear_description", lambda *a: calls.append(a))
    s = _session(root, "access-info")
    for line in ("no description", "no sample_lab"):
        _run(s, line, capsys)
    assert calls == []
    assert grammar.parse(COMMON, "no description").action == f"{COMMON}.no_select"
    assert grammar.parse(COMMON, "no sample_lab").action != "running_access_info.clear_description"


def test_named_mode_no_description_unchanged(root, capsys):
    s = _session(root, "access-info sample_lab")
    _run(s, "description Example", capsys)
    assert s.settings_candidate["running_descriptions"] == {"access_info": {"sample_lab": "Example"}}
    ok, _ = _run(s, "no description", capsys)
    assert ok and s.mode == "running_access_info" and _sel(s) == "sample_lab"
    assert not s.settings_candidate["running_descriptions"].get("access_info")
    assert grammar.parse("running_access_info", "no description").action == "running_access_info.clear_description"


def test_parent_no_access_info_and_global_definition_delete_unchanged(root, capsys):
    s = _session(root)
    _run(s, "no access-info", capsys)
    assert _sel(s) is None and s.mode == "running"
    g = cfgmod.CliSession(root)
    g.enter_configure()
    _run(g, "no access-info ai_b", capsys)  # Definition deletion candidate, not a selection change
    assert g.definition_candidate is None and g.definition_name == "ai_b"
    assert g.settings_candidate["active_access_info"] == "sample_lab"
    assert lab.access_info_exists("ai_b", root)


# ---------------------------------------------------- dirty / net-zero / io


def test_no_then_reselect_is_net_zero(root, capsys):
    s = _session(root, "access-info")
    _run(s, "no sample_lab", capsys)
    assert s.settings_dirty()
    _run(s, "sample_lab", capsys)
    assert not s.settings_dirty() and climain.render_configuration_candidate(s) == ""


def test_unselected_baseline_select_then_no_is_net_zero(root, capsys):
    s = _session(root, "no access-info")
    _run(s, "commit", capsys)
    assert "active_access_info" not in lab.read_settings(root)
    s = _session(root, "access-info")
    _run(s, "ai_b", capsys)
    assert s.settings_dirty()
    _run(s, "no ai_b", capsys)
    assert not s.settings_dirty() and climain.render_configuration_candidate(s) == ""


def test_clear_restores_selection_and_candidate_help(root, capsys):
    s = _session(root, "access-info")
    _run(s, "no sample_lab", capsys)
    assert _help(s, "no ") == ([], False)
    _run(s, "clear", capsys)
    assert _sel(s) == "sample_lab" and s.mode == COMMON and not s.settings_dirty()
    assert _help(s, "no ") == (["sample_lab"], False)


def test_commit_persists_unselected_state_and_leaves_definitions_and_descriptions(root, capsys):
    settings = lab.read_settings(root)
    settings["running_descriptions"] = {"access_info": {"sample_lab": "d"}}
    lab.write_settings(settings, root)
    defs = _defs(root)
    s = _session(root, "access-info")
    _run(s, "no sample_lab", capsys)
    ok, out = _run(s, "commit", capsys)
    assert ok and "Commit complete." in out and s.mode == COMMON
    persisted = lab.read_settings(root)
    assert "active_access_info" not in persisted
    assert _defs(root) == defs
    # restart: a brand-new session sees unselected state and a recomputed (empty) no-candidate list
    s2 = _session(root, "access-info")
    assert _sel(s2) is None and _help(s2, "no ") == ([], False)
    assert "sample_lab" not in climain.render_committed_running_config(s2).split("topology")[0]  # no access-info section
    assert lab.load_access_info("sample_lab", root)  # definition still stored


def test_show_views_after_no(root, capsys):
    s = _session(root, "access-info")
    _run(s, "no sample_lab", capsys)
    assert _run(s, "show configuration", capsys)[1].strip() == "no access-info"
    assert "sample_lab" in _run(s, "show running-config", capsys)[1]  # committed view unaffected
    assert not (s.settings_candidate.get("running_descriptions") or {}).get("access_info")  # not shown/changed as a description


def test_commit_failure_keeps_mode_and_candidate(root, capsys, monkeypatch):
    s = _session(root, "access-info")
    _run(s, "no sample_lab", capsys)
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    ok, out = _run(s, "commit", capsys)
    assert "Commit failed" in out and s.mode == COMMON and _sel(s) is None
    monkeypatch.setattr(os, "replace", real_replace)
    assert "active_access_info" in lab.read_settings(root)


def test_exit_root_end(root, capsys):
    s = _session(root, "access-info")
    _run(s, "no sample_lab", capsys)
    assert _run(s, "exit", capsys)[0] and s.mode == "running" and s.settings_dirty()
    _run(s, "access-info", capsys)
    assert _run(s, "root", capsys)[0] and s.mode == "global"
    _run(s, "running-config", capsys)
    _run(s, "access-info", capsys)
    ok, out = _run(s, "end", capsys)
    assert "Uncommitted" in out and s.mode == COMMON  # dirty end refused (existing rule)


# ---------------------------------------------------------------- paste


def test_paste_parent_no_access_info(root, capsys):
    s = _session(root)
    out = _paste(s, "no access-info\n!", capsys)
    assert out.strip() == "" and _sel(s) is None


def test_paste_common_no_and_reselect(root, capsys):
    s = _session(root)
    out = _paste(s, "access-info\n no sample_lab\n!", capsys)
    assert out.strip() == "" and _sel(s) is None and s.mode == "running"
    s = _session(root)
    out = _paste(s, "access-info\n no sample_lab\n ai_b\n!", capsys)
    assert out.strip() == "" and _sel(s) == "ai_b" and s.mode == "running"
    assert lab.access_info_exists("sample_lab", root)


def test_paste_named_no_description_and_error_stop(root, capsys):
    s = _session(root)
    out = _paste(s, "access-info sample_lab\n description X\n no description\n exit", capsys)
    assert out.strip() == "" and s.mode == "running"
    assert not s.settings_candidate["running_descriptions"].get("access_info")
    s = _session(root)
    out = _paste(s, "access-info\n no ai_b\n no sample_lab\n ai_b", capsys)
    assert "not currently selected" in out
    assert _sel(s) == "sample_lab"  # stopped at the first error: nothing after it ran
    assert s.mode == COMMON


def test_no_does_not_leak_into_global_definition_operations(root, capsys):
    s = _session(root)
    _paste(s, "access-info\n no sample_lab\n!\n no access-info", capsys)
    # the trailing `no access-info` after `!` ran in config-running (selection), not Global
    assert s.definition_kind is None and lab.access_info_exists("sample_lab", root)
