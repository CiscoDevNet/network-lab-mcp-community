# SPDX-License-Identifier: Apache-2.0
"""Name-less Running selection submodes for topology / scenario / reference
(`config-running-<kind>`), modeled on the access-info one (see
test_running_access_info_common_submode.py / _no.py, which stay the
authority for access-info). Selection and `no <name>` use the same session
methods as the parent/named commands; Topology/Scenario candidates may
become unselected but commit() rejects them; Reference is multi-select with
the D3-D7/Case 12 behavior intact. Isolated `lab_root`; synthetic data."""

from __future__ import annotations

import os

import pytest

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain

real_replace = os.replace

# kind, keyword, settings key, mode, committed name, other name, writer
KINDS = {
    "topology": ("topology", "active_topology", "running_topology_common", "sample_lab", "topo_b"),
    "scenario": ("scenario", "active_scenario", "running_scenario_common", "sample", "failover_test"),
    "access_info": ("access-info", "active_access_info", "running_access_info_common", "sample_lab", "ai_b"),
}
SINGLE = ["topology", "scenario"]


def _write(kind, name, root):
    if kind == "topology":
        lab.write_topology(name, {"name": name, "description": "", "devices": {}, "links": []}, root)
    elif kind == "scenario":
        lab.write_scenario(name, {"name": name, "description": "", "objectives": []}, root)
    elif kind == "access_info":
        lab.write_access_info(name, {"name": name, "devices": {}}, root)
    else:
        lab.write_reference(name, {"name": name, "description": "", "guidance": []}, root)


@pytest.fixture()
def root(lab_root):
    for kind, (_kw, _key, _mode, _a, b) in KINDS.items():
        for name in (b, "show", "description"):
            _write(kind, name, lab_root)
    for name in ("ref_b", "ref_c", "show", "description"):
        _write("reference", name, lab_root)
    settings = lab.read_settings(lab_root)
    settings["active_references"] = ["sample", "iosxr_basics"]
    lab.write_settings(settings, lab_root)
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


def _paste(s, text, capsys):
    capsys.readouterr()
    climain.execute_input_block(s, text)
    return capsys.readouterr().out


def _dangling(root):
    st = lab.read_settings(root)
    return [n for n in st.get("active_references") or [] if not lab.reference_exists(n, root)]


ALL = ["topology", "scenario", "reference"]
MODES = {"topology": "running_topology_common", "scenario": "running_scenario_common", "reference": "running_reference_common"}
PROMPTS = {k: f"network-lab(config-running-{k})# " for k in ALL}


# ---------------------------------------------------------------- grammar


@pytest.mark.parametrize("kw", ALL)
def test_bare_command_enters_common_submode_without_candidate_change(root, capsys, kw):
    s = _session(root)
    before = (dict(s.settings_candidate), s.settings_dirty())
    assert _run(s, kw, capsys)[0]
    assert s.mode == MODES[kw] and climain.prompt_text(s) == PROMPTS[kw]
    assert (s.settings_candidate, s.settings_dirty()) == before
    assert MODES[kw] not in cfgmod.RUNNING_ENTRY_MODES and cfgmod._EXIT_PARENT_MODE[MODES[kw]] == "running"


@pytest.mark.parametrize("kw", ALL)
def test_parent_help_has_cr_and_global_stays_incomplete(root, capsys, kw):
    s = _session(root)
    tokens, cr = _help(s, f"{kw} ")
    assert cr is True and "<name>" not in tokens and len(tokens) >= 2
    g = cfgmod.CliSession(root)
    g.enter_configure()
    ok, out = _run(g, kw, capsys)
    assert not ok and "Incomplete" in out and g.mode == "global"
    tokens, cr = _help(g, f"{kw} ")
    assert cr is False and "<name>" in tokens


@pytest.mark.parametrize("kw", ALL)
def test_common_help_keywords_and_no_without_cr(root, capsys, kw):
    s = _session(root, kw)
    tokens, cr = _help(s, "")
    assert cr is False and tokens[:3] == ["show", "no", "clear"]
    assert {"commit", "root", "end", "exit", "help"} <= set(tokens)
    tokens, cr = _help(s, "no ")
    assert cr is False and tokens
    ok, out = _run(s, "no", capsys)
    assert not ok and "Incomplete command" in out


def test_no_help_tracks_candidate_not_committed(root, capsys):
    s = _session(root, "topology")
    assert _help(s, "no ") == (["sample_lab"], False)
    _run(s, "topo_b", capsys)
    assert _help(s, "no ") == (["topo_b"], False) and _tab(s, "no t") == ["topo_b"]
    _run(s, "clear", capsys)
    assert _help(s, "no ") == (["sample_lab"], False)
    s = _session(root, "reference")
    assert _help(s, "no ") == (["iosxr_basics", "sample"], False)
    _run(s, "no sample", capsys)
    _run(s, "ref_b", capsys)
    assert sorted(_help(s, "no ")[0]) == ["iosxr_basics", "ref_b"] and _help(s, "no ")[1] is False
    assert _help(s, "no ref_b") == (["ref_b"], True)
    assert _help(s, "no REF") == ([], False)


# ------------------------------------------------------ selection (single)


@pytest.mark.parametrize("kind", SINGLE)
def test_selection_matches_parent_path_and_stays_and_replaces(root, capsys, kind):
    kw, key, mode, a, b = KINDS[kind]
    old = _session(root, f"{kw} {b}")
    new = _session(root, kw)
    assert _run(new, b, capsys)[0] and new.mode == mode
    assert new.settings_candidate == old.settings_candidate and new.settings_dirty() and old.settings_dirty()
    assert climain.render_configuration_candidate(new) == b  # kind-scoped Common form
    assert climain.render_configuration_candidate(old) == f"!\n {kw}\n  {b}\n!"  # running-scope form
    _run(new, a, capsys)  # A -> B -> A: net zero, same as the named path
    assert not new.settings_dirty()
    ok, out = _run(new, "nope", capsys)
    assert not ok and "does not exist" in out and new.mode == mode and new.settings_candidate[key] == a
    ok, out = _run(new, f"{a} extra", capsys)
    assert not ok and new.mode == mode


@pytest.mark.parametrize("kind", SINGLE)
def test_no_exact_name_unselects_stays_and_keeps_files(root, capsys, kind):
    kw, key, mode, a, b = KINDS[kind]
    s = _session(root, kw)
    assert _run(s, f"no {a}", capsys) == (True, "")
    assert key not in s.settings_candidate and s.mode == mode and s.settings_dirty()
    assert (root / {"topology": "topologies", "scenario": "scenarios"}[kind] / f"{a}.yaml").is_file()
    assert _help(s, "no ") == ([], False)
    label = {"topology": "Topology", "scenario": "Scenario"}[kind]
    for bad in (a, b, "unknown", a.upper()):
        before = dict(s.settings_candidate)
        ok, out = _run(s, f"no {bad}", capsys)
        assert not ok and f"{label} '{bad}' is not currently selected." in out
        assert s.settings_candidate == before and s.mode == mode


@pytest.mark.parametrize("kind", SINGLE)
def test_wrong_name_while_selected_is_rejected(root, capsys, kind):
    kw, key, mode, a, b = KINDS[kind]
    s = _session(root, kw)
    before = dict(s.settings_candidate)
    for bad in (b, a[:-1], "unknown"):
        ok, out = _run(s, f"no {bad}", capsys)
        assert not ok and "not currently selected" in out
    assert s.settings_candidate == before and not s.settings_dirty()


# ------------------------------------------------- required-selection commit


def test_topology_unselected_candidate_commit_fails_keeps_candidate_then_reselect_commits(root, capsys):
    s = _session(root, "topology")
    committed = (root / "settings.yaml").read_bytes()
    _run(s, "no sample_lab", capsys)
    assert climain.render_configuration_candidate(s) == "no sample_lab"  # kind-scoped Common form
    ok, out = _run(s, "commit", capsys)
    assert not ok and "missing a valid active topology" in out
    assert (root / "settings.yaml").read_bytes() == committed and lab.topology_exists("sample_lab", root)
    assert s.mode == "running_topology_common" and "active_topology" not in s.settings_candidate and s.settings_dirty()
    # unselect + reselect (a different one) in the SAME commit
    _run(s, "topo_b", capsys)
    ok, out = _run(s, "commit", capsys)
    assert ok and "Commit complete." in out
    assert lab.read_settings(root)["active_topology"] == "topo_b" and s.mode == "running_topology_common"


def test_topology_unselected_clear_restores_candidate_and_help(root, capsys):
    s = _session(root, "topology")
    _run(s, "no sample_lab", capsys)
    assert _help(s, "no ") == ([], False)
    _run(s, "clear", capsys)
    assert s.settings_candidate["active_topology"] == "sample_lab" and not s.settings_dirty()
    assert _help(s, "no ") == (["sample_lab"], False)


def test_topology_unselect_then_same_name_is_net_zero(root, capsys):
    s = _session(root, "topology")
    _run(s, "no sample_lab", capsys)
    _run(s, "sample_lab", capsys)
    assert not s.settings_dirty() and climain.render_configuration_candidate(s) == ""
    assert _run(s, "commit", capsys)[1].strip() == "No changes to commit."


def test_scenario_required_by_existing_validation_and_commit_failure_keeps_state(root, capsys):
    s = _session(root, "scenario")
    committed = (root / "settings.yaml").read_bytes()
    _run(s, "no sample", capsys)
    ok, out = _run(s, "commit", capsys)
    assert not ok and "missing a valid active scenario" in out
    assert (root / "settings.yaml").read_bytes() == committed
    assert s.mode == "running_scenario_common" and "active_scenario" not in s.settings_candidate
    _run(s, "failover_test", capsys)
    assert _run(s, "commit", capsys)[0] and lab.read_settings(root)["active_scenario"] == "failover_test"


def test_commit_failure_by_io_keeps_common_mode_for_topology(root, capsys, monkeypatch):
    s = _session(root, "topology", "topo_b")
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    ok, out = _run(s, "commit", capsys)
    assert "Commit failed" in out and s.mode == "running_topology_common" and s.settings_candidate["active_topology"] == "topo_b"
    monkeypatch.setattr(os, "replace", real_replace)
    assert lab.read_settings(root)["active_topology"] == "sample_lab"
    assert _run(s, "commit", capsys)[0] and lab.read_settings(root)["active_topology"] == "topo_b"


# --------------------------------------------------------------- reference


def test_reference_multi_select_idempotent_no_individual(root, capsys):
    s = _session(root, "reference")
    for n in ("ref_b", "ref_c", "ref_b"):  # duplicate select is a no-op
        assert _run(s, n, capsys)[0]
    assert s.settings_candidate["active_references"] == ["sample", "iosxr_basics", "ref_b", "ref_c"]
    assert _run(s, "no ref_b", capsys) == (True, "") and s.mode == "running_reference_common"
    assert s.settings_candidate["active_references"] == ["sample", "iosxr_basics", "ref_c"]
    ok, out = _run(s, "no ref_b", capsys)
    assert not ok and "Reference 'ref_b' is not currently selected." in out
    ok, out = _run(s, "nope", capsys)
    assert not ok and "does not exist" in out and s.mode == "running_reference_common"


def test_reference_selection_equals_parent_path(root, capsys):
    old = _session(root, "reference ref_b")
    new = _session(root, "reference", "ref_b")
    assert new.settings_candidate == old.settings_candidate
    assert climain.render_configuration_candidate(new) == "ref_b"  # kind-scoped Common form
    assert climain.render_configuration_candidate(old) == "!\n reference\n  ref_b\n!"


def test_d3_help_tab_follow_candidate_in_common_mode(root, capsys):
    s = _session(root, "reference")
    assert _help(s, "no ")[0] == _tab(s, "no ") == ["iosxr_basics", "sample"]
    _run(s, "no sample", capsys)
    _run(s, "no iosxr_basics", capsys)
    assert _help(s, "no ") == ([], False) and _tab(s, "no ") == []


def test_d4_d5_global_rendering_and_active_deletion_guidance_still_hold(root, capsys):
    s = _session(root, "reference", "no sample")
    _run(s, "exit", capsys)
    _run(s, "exit", capsys)
    assert s.mode == "global"
    _run(s, "no reference sample", capsys)
    assert climain.render_configuration_candidate(s) == "running-config\n no reference sample\n!\nno reference sample"
    assert _run(s, "commit", capsys)[0] and not lab.reference_exists("sample", root)
    s2 = _session(root)
    _run(s2, "root", capsys)
    _run(s2, "no reference iosxr_basics", capsys)
    ok, out = _run(s2, "commit", capsys)
    assert not ok and "Deselect it with 'no reference iosxr_basics' under 'running-config'" in out


def test_d6_pending_delete_rejected_in_common_mode_until_clear(root, capsys):
    g = cfgmod.CliSession(root)
    g.enter_configure()
    _run(g, "no reference ref_b", capsys)
    _run(g, "running-config", capsys)
    _run(g, "reference", capsys)
    ok, out = _run(g, "ref_b", capsys)
    assert not ok and "pending deletion" in out and g.mode == "running_reference_common"
    assert "ref_b" not in g.settings_candidate["active_references"]
    g.clear()
    assert _run(g, "ref_b", capsys)[0] and "ref_b" in g.settings_candidate["active_references"]


def test_d7_net_zero_and_order_restore_via_common_mode(root, capsys):
    before = (root / "settings.yaml").read_bytes()
    s = _session(root, "reference")
    _run(s, "no sample", capsys)
    _run(s, "sample", capsys)
    assert s.settings_candidate["active_references"] == ["sample", "iosxr_basics"]
    assert not s.settings_dirty() and climain.render_configuration_candidate(s) == ""
    assert "No changes to commit." in _run(s, "commit", capsys)[1]
    assert (root / "settings.yaml").read_bytes() == before


def _deselect_and_delete_via_common(root, capsys):
    s = _session(root, "reference", "no sample")
    _run(s, "root", capsys)
    _run(s, "no reference sample", capsys)
    return s


def test_case12_fault_injection_with_common_mode_candidates(root, capsys, monkeypatch):
    s = _deselect_and_delete_via_common(root, capsys)
    monkeypatch.setattr(os, "replace", lambda src, dst: (_ for _ in ()).throw(OSError("x")) if str(dst).endswith("settings.yaml") else real_replace(src, dst))
    ok, out = _run(s, "commit", capsys)
    assert "Commit failed" in out and lab.reference_exists("sample", root)
    assert lab.read_settings(root)["active_references"] == ["sample", "iosxr_basics"] and _dangling(root) == []
    monkeypatch.setattr(os, "replace", real_replace)
    assert _run(s, "commit", capsys)[0]
    assert not lab.reference_exists("sample", root) and _dangling(root) == []


def test_case12_delete_failure_after_settings_write_has_no_dangling(root, capsys, monkeypatch):
    s = _deselect_and_delete_via_common(root, capsys)
    monkeypatch.setitem(cfgmod._DEFINITION_DELETERS, "reference", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert "Commit failed" in _run(s, "commit", capsys)[1]
    assert lab.read_settings(root)["active_references"] == ["iosxr_basics"] and _dangling(root) == []


def test_reference_zero_selection_is_allowed_to_commit(root, capsys):
    s = _session(root, "reference", "no sample", "no iosxr_basics")
    assert _run(s, "commit", capsys)[0]
    assert lab.read_settings(root).get("active_references") in ([], None)
    assert lab.reference_exists("sample", root)


# ------------------------------------------------------ description / names


@pytest.mark.parametrize("kw", ALL)
def test_description_text_rejected_with_hint(root, capsys, kw):
    s = _session(root, kw)
    before = dict(s.settings_candidate)
    ok, out = _run(s, "description foo", capsys)
    assert not ok and "Use the named mode" in out and s.settings_candidate == before and s.mode == MODES[kw]
    assert not [a for a in climain.HANDLERS if a.startswith(MODES[kw]) and "description" in a]
    with pytest.raises(cfgmod.ConfigError):
        s.set_current_running_description("x")


@pytest.mark.parametrize("kw", ALL)
def test_no_description_is_selection_removal_only_and_never_description_handler(root, capsys, kw, monkeypatch):
    calls = []
    for k in ("running_topology", "running_scenario", "running_reference", "running_access_info"):
        monkeypatch.setitem(climain.HANDLERS, f"{k}.clear_description", lambda *a, c=calls: c.append(a))
    s = _session(root, kw)
    s.settings_candidate["running_descriptions"] = {"topology": {"sample_lab": "keep"}}
    snapshot = dict(s.settings_candidate["running_descriptions"])
    ok, out = _run(s, "no description", capsys)  # mismatch (nothing is named `description` yet)
    assert not ok and "not currently selected" in out and "Use the named mode" not in out
    assert _run(s, "description", capsys)[0]  # selects the definition literally named `description`
    ok, out = _run(s, "no description", capsys)
    assert ok and out == "" and s.mode == MODES[kw]
    assert calls == [] and s.settings_candidate["running_descriptions"] == snapshot
    assert grammar.parse(MODES[kw], "no description").action == f"{MODES[kw]}.no_select"


@pytest.mark.parametrize("kw", ALL)
def test_named_submodes_and_description_unchanged(root, capsys, kw):
    sel = {"topology": "sample_lab", "scenario": "sample", "reference": "sample"}[kw]
    s = _session(root, f"{kw} {sel}")
    assert s.mode == f"running_{kw}" and climain.prompt_text(s) == f"network-lab(config-running-{kw}-{sel})# "
    _run(s, "description Example", capsys)
    kind = kw
    assert s.settings_candidate["running_descriptions"][kind][sel] == "Example"
    assert _run(s, "no description", capsys)[0] and s.mode == f"running_{kw}"
    assert not s.settings_candidate["running_descriptions"].get(kind)


@pytest.mark.parametrize("kw", ALL)
def test_keyword_named_definition_via_parent_and_keyword_priority_in_child(root, capsys, kw):
    s = _session(root, kw)
    before = dict(s.settings_candidate)
    for word in ("show", "SHOW", "sh"):
        _run(s, word, capsys)
        assert s.settings_candidate == before and s.mode == MODES[kw]
    for word in ("c", "e"):
        ok, out = _run(s, word, capsys)
        assert not ok and "Ambiguous" in out
    _run(s, "exit", capsys)
    assert _run(s, f"{kw} show", capsys)[0]
    assert s.mode == f"running_{kw}" and s.current_running_entry_name == "show"


# ---------------------------------------------------------- mode transitions


@pytest.mark.parametrize("kw", ALL)
def test_dirty_running_can_enter_exit_and_move_between_siblings_via_parent(root, capsys, kw):
    s = _session(root, "no access-info")
    for k in ALL:
        assert _run(s, k, capsys)[0] and s.mode == MODES[k]
        assert _run(s, "exit", capsys)[0] and s.mode == "running" and s.settings_dirty()


@pytest.mark.parametrize("kw", ALL)
def test_definition_candidate_dirty_still_enters_running_submode(root, capsys, kw):
    g = cfgmod.CliSession(root)
    g.enter_configure()
    _run(g, "no access-info ai_b", capsys)  # pending definition deletion
    assert _run(g, "running-config", capsys)[0]
    assert _run(g, kw, capsys)[0] and g.mode == MODES[kw]


def test_root_end_commit_clear_modes(root, capsys):
    s = _session(root, "topology", "topo_b")
    ok, out = _run(s, "end", capsys)
    assert "Uncommitted" in out and s.mode == "running_topology_common"
    _run(s, "clear", capsys)
    assert s.mode == "running_topology_common" and s.settings_candidate["active_topology"] == "sample_lab"
    _run(s, "topo_b", capsys)
    assert _run(s, "commit", capsys)[0] and s.mode == "running_topology_common"
    assert _run(s, "root", capsys)[0] and s.mode == "global"


def test_show_views_in_common_modes(root, capsys):
    s = _session(root, "reference", "no sample")
    assert _run(s, "show configuration", capsys)[1].strip() == "no sample"
    assert " reference\n  sample\n" in _run(s, "show running-config", capsys)[1]  # committed view


# ------------------------------------------------------------------- paste


def test_paste_new_common_forms_replay(root, capsys):
    s = _session(root)
    out = _paste(s, "topology\n topo_b\n!\nscenario\n failover_test\n!\nreference\n no sample\n ref_b\n!", capsys)
    assert out.strip() == "" and s.mode == "running"
    assert s.settings_candidate["active_topology"] == "topo_b" and s.settings_candidate["active_scenario"] == "failover_test"
    assert s.settings_candidate["active_references"] == ["iosxr_basics", "ref_b"]


def test_paste_topology_unselect_then_reselect_in_one_block(root, capsys):
    s = _session(root)
    out = _paste(s, "topology\n no sample_lab\n sample_lab\n!", capsys)
    assert out.strip() == "" and not s.settings_dirty()


def test_paste_show_configuration_output_replays(root, capsys):
    src = _session(root, "topology", "topo_b")
    assert climain.render_configuration_candidate(src) == "topo_b"  # Common output replays in Common
    c = _session(root, "topology")
    assert _paste(c, "topo_b", capsys).strip() == "" and c.settings_candidate["active_topology"] == "topo_b"
    src = _session(root, "topology topo_b")
    _run(src, "exit", capsys)
    text = climain.render_configuration_candidate(src)
    for start in ("running", "global"):
        s = cfgmod.CliSession(root)
        s.enter_configure()
        if start == "running":
            climain.execute_command_line(s, "running-config")
        out = _paste(s, text if start == "running" else "running-config\n" + text, capsys)
        assert s.settings_candidate["active_topology"] == "topo_b", (start, out)


def test_paste_show_running_config_output_is_net_zero_replay(root, capsys):
    s = _session(root)
    text = "access-info\n  sample_lab\n!\n topology\n  sample_lab\n!\n scenario\n  sample\n!\n reference\n  sample\n  iosxr_basics\n!"
    out = _paste(s, text, capsys)
    assert out.strip() == "" and not s.settings_dirty() and s.mode == "running"


def test_paste_old_named_script_unchanged_and_error_stops(root, capsys):
    s = _session(root)
    out = _paste(s, "topology topo_b\n description D\n exit\n!", capsys)
    assert out.strip() == "" and s.mode == "running"
    assert s.settings_candidate["running_descriptions"] == {"topology": {"topo_b": "D"}}
    s = _session(root)
    out = _paste(s, "topology\n no topo_b\n sample_lab\n", capsys)
    assert "not currently selected" in out and s.settings_candidate["active_topology"] == "sample_lab"
    assert s.mode == "running_topology_common"


def test_bare_unselected_topology_rendering_is_not_replayable_but_fails_safely(root, capsys):
    """`no topology` (the renderer's line for an unselected candidate) has no
    grammar in config-running: replay stops with an error instead of
    guessing; nothing is changed."""
    s = _session(root)
    out = _paste(s, "no topology\n topology\n topo_b", capsys)
    assert "Invalid" in out and s.settings_candidate["active_topology"] == "sample_lab"
