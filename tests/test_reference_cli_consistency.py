"""Reference CLI consistency and commit safety:

D3 running `no reference ?` enumerates the candidate-active references
(Help == Tab == prefix); D4 global `show configuration` separates a running
selection removal from a stored-definition deletion; D5 the active-reference
deletion commit error says how to proceed; D6 a pending-deletion reference
cannot be newly selected; D7 unselect-then-reselect is an exact net-zero;
Case 12 deletion + deselection commits write settings.yaml before deleting
the file, so a mid-commit failure never persists a dangling selection.

Every test uses the isolated `lab_root` fixture (tmp_path); nothing here
touches the real lab/ directory, the network or a device."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest
import yaml

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain

real_replace = os.replace


def _two_active(lab_root):
    """sample + iosxr_basics active, extra_ref stored but inactive."""
    lab.write_reference("extra_ref", {"name": "extra_ref", "description": "", "guidance": []}, lab_root)
    settings = lab.read_settings(lab_root)
    settings["active_references"] = ["sample", "iosxr_basics"]
    lab.write_settings(settings, lab_root)


def _session(lab_root):
    s = cfgmod.CliSession(lab_root)
    s.enter_configure()
    return s


def _run(session, line, capsys):
    capsys.readouterr()
    ok = climain.execute_command_line(session, line)
    out = capsys.readouterr().out
    if line == "commit":  # _do_commit reports failure by output, not by return value
        ok = "Commit complete." in out or "No changes to commit." in out
    return ok, out


def _help(session, text):
    result = grammar.help(session.mode, text, climain.build_context(session))
    return [line.token for line in result.lines], result.show_cr


def _tab(session, text):
    return grammar.complete(session.mode, text, climain.build_context(session)).candidates


def _dangling(lab_root):
    settings = lab.read_settings(lab_root)
    return [n for n in settings.get("active_references") or [] if not lab.reference_exists(n, lab_root)]


# ---------------------------------------------------------------- D3 Help


def test_running_no_reference_help_lists_candidate_active_and_matches_tab(lab_root, capsys):
    _two_active(lab_root)
    s = _session(lab_root)
    _run(s, "running-config", capsys)
    assert _help(s, "no reference ") == (["iosxr_basics", "sample"], False)
    assert _tab(s, "no reference ") == ["iosxr_basics", "sample"]

    _run(s, "no reference sample", capsys)
    assert _help(s, "no reference ") == (["iosxr_basics"], False)
    assert _tab(s, "no reference ") == ["iosxr_basics"]

    _run(s, "no reference iosxr_basics", capsys)
    assert _help(s, "no reference ") == ([], False)  # zero candidates, no stray <cr>/<name>
    assert _tab(s, "no reference ") == []


def test_running_no_reference_help_follows_candidate_not_committed(lab_root, capsys):
    _two_active(lab_root)
    s = _session(lab_root)
    _run(s, "running-config", capsys)
    _run(s, "reference extra_ref", capsys)
    _run(s, "exit", capsys)
    assert "extra_ref" in _help(s, "no reference ")[0]
    assert _tab(s, "no reference ") == sorted(_help(s, "no reference ")[0])


def test_running_no_reference_prefix_exact_and_case(lab_root, capsys):
    _two_active(lab_root)
    s = _session(lab_root)
    _run(s, "running-config", capsys)
    assert _help(s, "no reference s") == (["sample"], False)
    assert _help(s, "no reference sample") == (["sample"], True)  # exact match shows <cr>
    assert _help(s, "no reference SAMPLE") == ([], False)  # case-sensitive
    assert _tab(s, "no reference SA") == []
    assert _tab(s, "no reference sa") == ["sample"]


def test_running_reference_help_unchanged(lab_root, capsys):
    _two_active(lab_root)
    s = _session(lab_root)
    _run(s, "running-config", capsys)
    assert _help(s, "reference ")[0] == sorted(lab.list_reference_names(lab_root))


# ---------------------------------------------------------- D4 rendering


def test_global_show_configuration_separates_selection_removal_from_definition_deletion(lab_root, capsys):
    _two_active(lab_root)
    s = _session(lab_root)
    _run(s, "running-config", capsys)
    _run(s, "no reference sample", capsys)
    # running-mode output is the plain selection removal (unchanged)
    assert climain.render_configuration_candidate(s) == "no reference sample"
    _run(s, "exit", capsys)
    assert climain.render_configuration_candidate(s) == "running-config\n no reference sample\n!"
    _run(s, "no reference sample", capsys)  # now also delete the definition
    assert climain.render_configuration_candidate(s) == "running-config\n no reference sample\n!\nno reference sample"


def test_definition_deletion_alone_renders_as_plain_global_command(lab_root, capsys):
    _two_active(lab_root)
    s = _session(lab_root)
    _run(s, "no reference extra_ref", capsys)
    assert climain.render_configuration_candidate(s) == "no reference extra_ref"


def test_pasted_global_rendering_roundtrips_to_same_candidate(lab_root, capsys):
    _two_active(lab_root)
    s = _session(lab_root)
    _run(s, "running-config", capsys)
    _run(s, "no reference sample", capsys)
    _run(s, "exit", capsys)
    _run(s, "no reference sample", capsys)
    text = climain.render_configuration_candidate(s)
    s.clear()
    capsys.readouterr()
    climain.execute_input_block(s, text)
    assert climain.render_configuration_candidate(s) == text
    assert s.settings_candidate["active_references"] == ["iosxr_basics"]
    assert s.definition_candidate is None and s.definition_name == "sample"


# ------------------------------------------------------ D5 error message


def test_active_reference_deletion_commit_error_guides_user_and_keeps_state(lab_root, capsys):
    _two_active(lab_root)
    s = _session(lab_root)
    _run(s, "no reference sample", capsys)
    ok, out = _run(s, "commit", capsys)
    assert not ok
    assert "% Cannot remove reference 'sample' because it is active in running-config." in out
    assert "% Deselect it with 'no reference sample' under 'running-config', then commit again." in out
    assert lab.reference_exists("sample", lab_root)
    assert lab.read_settings(lab_root)["active_references"] == ["sample", "iosxr_basics"]
    assert s.definition_dirty()  # candidate kept
    s.clear()
    assert not s.overall_dirty()


def test_guided_workflow_succeeds(lab_root, capsys):
    _two_active(lab_root)
    s = _session(lab_root)
    _run(s, "no reference sample", capsys)
    assert not _run(s, "commit", capsys)[0]
    for line in ("running-config", "no reference sample", "root"):
        assert _run(s, line, capsys)[0]
    assert s.mode == "global"
    ok, out = _run(s, "commit", capsys)
    assert ok and "Commit complete." in out
    assert not lab.reference_exists("sample", lab_root)
    assert lab.read_settings(lab_root)["active_references"] == ["iosxr_basics"]


def test_other_kinds_error_message_unchanged(lab_root, capsys):
    s = _session(lab_root)
    _run(s, "no access-info sample_lab", capsys)
    ok, out = _run(s, "commit", capsys)
    assert not ok
    assert out.strip() == "% Cannot remove access information 'sample_lab' because it is active in running-config."


# ------------------------------------------------- D6 pending deletion


def test_pending_delete_reference_cannot_be_newly_selected_until_clear(lab_root, capsys):
    _two_active(lab_root)
    s = _session(lab_root)
    _run(s, "no reference extra_ref", capsys)
    _run(s, "running-config", capsys)
    ok, out = _run(s, "reference extra_ref", capsys)
    assert not ok and "pending deletion" in out and "'clear'" in out
    assert "extra_ref" not in s.settings_candidate["active_references"]
    assert s.mode == "running"
    s.clear()
    _run(s, "running-config", capsys) if s.mode != "running" else None
    assert _run(s, "reference extra_ref", capsys)[0]
    assert "extra_ref" in s.settings_candidate["active_references"]


def test_already_active_pending_delete_reference_still_reenters_and_deselects(lab_root, capsys):
    _two_active(lab_root)
    s = _session(lab_root)
    _run(s, "no reference sample", capsys)
    _run(s, "running-config", capsys)
    assert _run(s, "reference sample", capsys)[0]  # already active: submode only
    assert s.mode == "running_reference"
    _run(s, "exit", capsys)
    assert _run(s, "no reference sample", capsys)[0]
    _run(s, "root", capsys)
    assert _run(s, "commit", capsys)[0]
    assert not lab.reference_exists("sample", lab_root)


def test_delete_then_deselect_and_deselect_then_delete_both_commit(lab_root, capsys):
    for order in ("delete_first", "deselect_first"):
        _two_active(lab_root) if not lab.reference_exists("extra_ref", lab_root) else None
        settings = lab.read_settings(lab_root)
        settings["active_references"] = ["sample", "iosxr_basics"]
        lab.write_settings(settings, lab_root)
        lab.write_reference("sample", {"name": "sample", "description": "", "guidance": []}, lab_root)
        s = _session(lab_root)
        if order == "delete_first":
            _run(s, "no reference sample", capsys)
            _run(s, "running-config", capsys)
            _run(s, "no reference sample", capsys)
        else:
            _run(s, "running-config", capsys)
            _run(s, "no reference sample", capsys)
            _run(s, "root", capsys)
            _run(s, "no reference sample", capsys)
        assert _run(s, "commit", capsys)[0], order
        assert not lab.reference_exists("sample", lab_root)
        assert _dangling(lab_root) == []


def test_single_dirty_definition_constraint_kept(lab_root, capsys):
    _two_active(lab_root)
    s = _session(lab_root)
    assert _run(s, "no reference sample", capsys)[0]
    ok, out = _run(s, "no reference iosxr_basics", capsys)
    assert not ok
    assert "Uncommitted changes exist. Use 'commit' or 'clear' before switching." in out


def test_commit_validation_still_fails_closed_for_select_plus_delete_of_inactive(lab_root):
    _two_active(lab_root)
    s = _session(lab_root)
    s.mode = "running"
    s.add_reference("extra_ref")
    s.go_to_global()
    s.remove_definition("reference", "extra_ref")
    with pytest.raises(cfgmod.CommitValidationError, match="active in running-config"):
        s.commit()
    assert lab.reference_exists("extra_ref", lab_root)


# ------------------------------------------------------------ D7 net-zero


def test_unselect_then_reselect_is_exact_net_zero(lab_root, capsys):
    _two_active(lab_root)
    before = (lab_root / "settings.yaml").read_bytes()
    s = _session(lab_root)
    _run(s, "running-config", capsys)
    _run(s, "no reference sample", capsys)
    _run(s, "reference sample", capsys)
    assert s.settings_candidate["active_references"] == ["sample", "iosxr_basics"]
    assert not s.settings_dirty() and not s.overall_dirty()
    assert climain.render_configuration_candidate(s) == ""
    ok, out = _run(s, "commit", capsys)
    assert "No changes to commit." in out
    assert (lab_root / "settings.yaml").read_bytes() == before  # no rewrite


def test_select_then_unselect_is_net_zero(lab_root, capsys):
    _two_active(lab_root)
    s = _session(lab_root)
    s.mode = "running"
    s.add_reference("extra_ref")
    s.remove_reference("extra_ref")
    assert not s.settings_dirty()


def test_new_activation_appends_after_committed_order(lab_root, capsys):
    _two_active(lab_root)
    s = _session(lab_root)
    s.mode = "running"
    s.remove_reference("sample")
    s.add_reference("extra_ref")
    s.add_reference("sample")
    assert s.settings_candidate["active_references"] == ["sample", "iosxr_basics", "extra_ref"]
    assert climain.render_configuration_candidate(s) != ""
    s.commit()
    assert lab.read_settings(lab_root)["active_references"] == ["sample", "iosxr_basics", "extra_ref"]


def test_committed_order_preserved_when_one_removed(lab_root):
    _two_active(lab_root)
    s = _session(lab_root)
    s.mode = "running"
    s.remove_reference("sample")
    s.commit()
    assert lab.read_settings(lab_root)["active_references"] == ["iosxr_basics"]


# ------------------------------------------------- Case 12 fault injection


def _delete_and_deselect(lab_root, capsys):
    _two_active(lab_root)
    s = _session(lab_root)
    s.mode = "running"
    s.remove_reference("sample")
    s.go_to_global()
    s.remove_definition("reference", "sample")
    return s


def _fail_replace(monkeypatch, when):
    calls = []

    def fake(src, dst):
        calls.append(os.path.basename(str(dst)))
        if when(os.path.basename(str(dst))):
            raise OSError("injected replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fake)
    return calls


def test_settings_write_failure_leaves_definition_and_selection_both_intact(lab_root, monkeypatch, capsys):
    s = _delete_and_deselect(lab_root, capsys)
    _fail_replace(monkeypatch, lambda name: name == "settings.yaml")
    ok, out = _run(s, "commit", capsys)
    assert not ok and "% Commit failed" in out and "Commit complete." not in out
    assert lab.reference_exists("sample", lab_root)  # NOT deleted first
    assert lab.read_settings(lab_root)["active_references"] == ["sample", "iosxr_basics"]
    assert _dangling(lab_root) == []
    assert s.definition_dirty() and s.settings_dirty()  # candidate retained
    monkeypatch.setattr(os, "replace", real_replace)
    ok, out = _run(s, "commit", capsys)
    assert ok and "Commit complete." in out
    assert not lab.reference_exists("sample", lab_root)
    assert lab.read_settings(lab_root)["active_references"] == ["iosxr_basics"]
    assert _dangling(lab_root) == []


def test_delete_failure_after_settings_write_has_no_dangling_and_retry_converges(lab_root, monkeypatch, capsys):
    s = _delete_and_deselect(lab_root, capsys)

    def boom(*a, **k):
        raise OSError("injected delete failure")

    monkeypatch.setitem(cfgmod._DEFINITION_DELETERS, "reference", boom)
    ok, out = _run(s, "commit", capsys)
    assert not ok and "% Commit failed" in out
    assert lab.read_settings(lab_root)["active_references"] == ["iosxr_basics"]  # selection already gone
    assert lab.reference_exists("sample", lab_root)  # unreferenced orphan, not a dangling selection
    assert _dangling(lab_root) == []
    assert s.definition_dirty() and not s.settings_dirty()

    # Restart (fault removed): a fresh session sees a consistent state and can finish the delete.
    monkeypatch.undo()
    s2 = _session(lab_root)
    assert _dangling(lab_root) == []
    assert not s2.overall_dirty()
    assert s2.committed_settings["active_references"] == ["iosxr_basics"]
    assert _run(s2, "no reference sample", capsys)[0]
    assert _run(s2, "commit", capsys)[0]
    assert not lab.reference_exists("sample", lab_root)
    assert _dangling(lab_root) == []


def test_delete_failure_retry_same_session_completes(lab_root, monkeypatch, capsys):
    s = _delete_and_deselect(lab_root, capsys)
    real = cfgmod._DEFINITION_DELETERS["reference"]
    monkeypatch.setitem(cfgmod._DEFINITION_DELETERS, "reference", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert not _run(s, "commit", capsys)[0]
    monkeypatch.setitem(cfgmod._DEFINITION_DELETERS, "reference", real)
    ok, out = _run(s, "commit", capsys)
    assert ok and "Commit complete." in out
    assert not lab.reference_exists("sample", lab_root)
    assert lab.read_settings(lab_root)["active_references"] == ["iosxr_basics"]
    assert not s.overall_dirty()


def test_restart_reload_in_new_process_after_failed_commit(lab_root, monkeypatch, capsys):
    s = _delete_and_deselect(lab_root, capsys)
    _fail_replace(monkeypatch, lambda name: name == "settings.yaml")
    assert not _run(s, "commit", capsys)[0]
    monkeypatch.setattr(os, "replace", real_replace)
    code = textwrap.dedent(
        """
        import sys, yaml
        from pathlib import Path
        from network_lab_mcp import lab
        root = Path(sys.argv[1])
        st = lab.read_settings(root)
        missing = [n for n in st.get("active_references") or [] if not lab.reference_exists(n, root)]
        print(missing, st.get("active_references"))
        """
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code, str(lab_root)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=True,
    ).stdout.strip()
    assert out == "[] ['sample', 'iosxr_basics']"


def test_topology_switch_plus_delete_writes_settings_first(lab_root, monkeypatch):
    lab.write_topology("t2", {"name": "t2", "description": "", "devices": {}, "links": []}, lab_root)
    s = cfgmod.CliSession(lab_root)
    s.enter_configure()
    s.mode = "running"
    s.select_topology("t2")
    s.go_to_global()
    s.remove_definition("topology", "sample_lab")
    monkeypatch.setitem(cfgmod._DEFINITION_DELETERS, "topology", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(OSError):
        s.commit()
    settings = lab.read_settings(lab_root)
    assert settings["active_topology"] == "t2"
    assert lab.topology_exists("t2", lab_root)  # selection points at an existing topology


def test_undeletable_definition_is_rejected_before_any_write(lab_root, monkeypatch):
    s = _delete_and_deselect(lab_root, None)
    before = (lab_root / "settings.yaml").read_bytes()
    monkeypatch.setitem(cfgmod._DEFINITION_DELETABILITY_CHECKS, "reference", lambda *a, **k: False)
    with pytest.raises(cfgmod.CommitValidationError, match="cannot be deleted"):
        s.commit()
    assert (lab_root / "settings.yaml").read_bytes() == before
    assert lab.reference_exists("sample", lab_root)


def test_create_or_edit_still_writes_definition_before_settings(lab_root):
    order = []
    s = cfgmod.CliSession(lab_root)
    s.enter_configure()
    s.enter_reference_definition("brand_new")
    s.mode = "running"
    s.add_reference("brand_new")
    s.go_to_global()
    orig_w, orig_s = cfgmod._DEFINITION_WRITERS["reference"], lab.write_settings
    cfgmod._DEFINITION_WRITERS["reference"] = lambda *a, **k: (order.append("definition"), orig_w(*a, **k))
    lab.write_settings = lambda *a, **k: (order.append("settings"), orig_s(*a, **k))
    try:
        s.commit()
    finally:
        cfgmod._DEFINITION_WRITERS["reference"] = orig_w
        lab.write_settings = orig_s
    assert order == ["definition", "settings"]
    assert "brand_new" in lab.read_settings(lab_root)["active_references"]
