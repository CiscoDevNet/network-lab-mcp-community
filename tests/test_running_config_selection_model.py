# SPDX-License-Identifier: Apache-2.0
"""Running-config selection model clarifications.

Two independent, additive changes to `config-running`'s already-existing
selection semantics (access-info: optional/single/unsettable; topology:
mandatory/single, switched not unset; scenario: mandatory/single, switched
not unset; reference: optional/multiple, individually removable) --
neither change alters those semantics, only how they are *displayed* or
*explained*:

1. `show running-config` always renders the access-info and reference
   sections, even when empty/absent, using a display-only "<none>"
   marker instead of silently omitting the section. Never applies to
   topology/scenario (mandatory, never absent in valid state). "<none>"
   is rendering only: never written to settings.yaml, never a valid
   selector token, never a grammar candidate, never returned through
   `show configuration` candidate-diff syntax (which keeps using
   "no access-info" etc., unchanged).

2. `config-running# no ?` (the spaced form only) gets an additional
   explanatory footer describing all four kinds' selection model,
   without adding "no topology"/"no scenario" as real grammar candidates
   -- those two names appear only in the footer's prose, never as
   parseable/completable tokens. `no?` (attached, no space) and `no ?`
   in every other mode are unaffected.

Uses the isolated `lab_root` fixture only; never the real repository's
lab/ directory."""

from __future__ import annotations

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain


def _settings(lab_root, **overrides):
    settings = lab.read_settings(lab_root)
    settings.update(overrides)
    lab.write_settings(settings, lab_root)


# ==========================================================================
# "<none>" rendering
# ==========================================================================


def test_access_info_absent_shows_none_others_unaffected(lab_root):
    _settings(lab_root, active_access_info=None)
    session = cfgmod.CliSession(lab_root)
    out = climain.render_committed_running_config(session)
    assert " access-info" in out
    assert "  <none>" in out
    assert " topology" in out
    assert "  sample_lab" in out
    assert " scenario" in out
    assert " reference" in out
    assert "  sample" in out


def test_references_empty_shows_none_others_unaffected(lab_root):
    _settings(lab_root, active_references=[])
    session = cfgmod.CliSession(lab_root)
    out = climain.render_committed_running_config(session)
    assert " reference" in out
    assert "  <none>" in out
    assert " access-info" in out
    assert "  sample_lab" in out


def test_both_access_info_and_references_absent(lab_root):
    _settings(lab_root, active_access_info=None, active_references=[])
    session = cfgmod.CliSession(lab_root)
    out = climain.render_committed_running_config(session)
    lines = out.splitlines()
    # Both sections present, both showing <none>, never omitted.
    assert " access-info" in lines
    assert " reference" in lines
    assert lines.count("  <none>") == 2


def test_normal_populated_state_has_no_none_marker(lab_root):
    session = cfgmod.CliSession(lab_root)
    out = climain.render_committed_running_config(session)
    assert "<none>" not in out


def test_none_never_mixed_with_real_values_in_reference_section(lab_root):
    # references non-empty -> real values only, never <none> alongside them.
    session = cfgmod.CliSession(lab_root)
    out = climain.render_committed_running_config(session)
    ref_idx = out.splitlines().index(" reference")
    following = out.splitlines()[ref_idx + 1]
    assert following != "  <none>"
    assert "<none>" not in following


def test_topology_and_scenario_never_show_none():
    # Mandatory fields keep their pre-existing "omit if absent" behavior;
    # This never introduces <none> for them. (access-info/reference
    # legitimately show <none> here too, since this minimal settings dict
    # omits them as well -- that is not what this test is checking.)
    settings = {"active_topology": None, "active_scenario": None}
    lines = climain._running_config_lines(settings).splitlines()
    assert " topology" not in lines
    assert " scenario" not in lines


# ==========================================================================
# Serialization safety
# ==========================================================================


def test_none_marker_never_persisted_to_settings_yaml(lab_root, tmp_path):
    _settings(lab_root, active_access_info=None, active_references=[])
    session = cfgmod.CliSession(lab_root)
    climain.render_committed_running_config(session)  # renders <none>, must not persist it
    settings_path = lab_root / "settings.yaml"
    text = settings_path.read_text()
    assert "<none>" not in text


# ==========================================================================
# Committed vs. candidate separation (lifecycle)
# ==========================================================================


def test_pending_no_access_info_does_not_leak_before_commit(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    session.clear_access_info_selection()
    out_before = climain.render_committed_running_config(session)
    assert "<none>" not in out_before
    assert "sample_lab" in out_before  # still shows the committed value

    session.commit()
    out_after = climain.render_committed_running_config(session)
    assert "  <none>" in out_after.splitlines()


def test_failed_commit_keeps_previous_committed_value_displayed(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    session.clear_access_info_selection()
    session.go_to_global()
    session.remove_definition("scenario", "sample")  # active scenario -> commit fails closed
    try:
        session.commit()
    except cfgmod.CommitValidationError:
        pass
    out = climain.render_committed_running_config(session)
    assert "sample_lab" in out
    assert "<none>" not in out


def test_reselecting_access_info_after_none_removes_it_cleanly(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    session.clear_access_info_selection()
    session.commit()
    assert "  <none>" in climain.render_committed_running_config(session).splitlines()

    session2 = cfgmod.CliSession(lab_root)
    session2.enter_configure()
    session2.mode = "running"
    session2.select_access_info("sample_lab")
    session2.commit()
    out = climain.render_committed_running_config(session2)
    assert "<none>" not in out
    assert "  sample_lab" in out.splitlines()


def test_pending_reference_removal_does_not_leak_before_commit(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    session.remove_reference("sample")
    out_before = climain.render_committed_running_config(session)
    assert "<none>" not in out_before
    assert "  sample" in out_before.splitlines()

    session.commit()
    out_after = climain.render_committed_running_config(session)
    assert "  <none>" in out_after.splitlines()


def test_readding_reference_after_none_removes_it_cleanly(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    session.remove_reference("sample")
    session.commit()
    assert "  <none>" in climain.render_committed_running_config(session).splitlines()

    session2 = cfgmod.CliSession(lab_root)
    session2.enter_configure()
    session2.mode = "running"
    session2.add_reference("sample")
    session2.commit()
    out = climain.render_committed_running_config(session2)
    assert "<none>" not in out
    assert "  sample" in out.splitlines()


# ==========================================================================
# show configuration (candidate diff) is unaffected -- never uses <none>
# ==========================================================================


def test_show_configuration_diff_still_uses_no_access_info_syntax(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    session.clear_access_info_selection()
    text = climain.render_configuration_candidate(session)
    assert text == "no access-info"
    assert "<none>" not in text


# ==========================================================================
# `config-running# no ?` explanatory footer (Change 1)
# ==========================================================================


def test_footer_content_exact():
    footer = climain._render_running_config_selection_model()
    assert footer.startswith("Running-config selection model:")
    assert "access-info" in footer
    assert "topology" in footer
    assert "scenario" in footer
    assert "reference" in footer
    assert 'use "topology <name>" to switch' in footer
    assert 'use "scenario <name>" to switch' in footer
    assert 'no reference <name>' in footer


def test_footer_shown_for_spaced_no_in_running_mode():
    assert climain._should_show_running_no_footer("running", "no ") is True


def test_footer_not_shown_for_attached_no_question_mark():
    # "no?" (no trailing space) is a different help context ("token?" vs
    # "token ?") -- investigation confirms the codebase already treats
    # them differently (grammar.help() returns a single-line summary for
    # "no?", not the full candidate list); the footer must not appear.
    assert climain._should_show_running_no_footer("running", "no") is False


def test_footer_not_shown_in_other_modes():
    for mode in ("exec", "global", "access_info", "access_device", "topology", "scenario", "reference"):
        assert climain._should_show_running_no_footer(mode, "no ") is False, mode


def test_footer_not_shown_for_deeper_no_paths():
    # "no access-info ?" / "no reference ?" are help for a *different*
    # (spaced) context one level deeper -- not the bare "no ?" the footer
    # is scoped to.
    assert climain._should_show_running_no_footer("running", "no access-info ") is False
    assert climain._should_show_running_no_footer("running", "no reference ") is False


def test_footer_not_shown_for_unrelated_running_tokens():
    assert climain._should_show_running_no_footer("running", "topology ") is False
    assert climain._should_show_running_no_footer("running", "reference ") is False
    assert climain._should_show_running_no_footer("running", "") is False


# ==========================================================================
# Grammar-level: footer never becomes a real candidate / never affects
# parsing or Tab completion
# ==========================================================================


def test_no_topology_and_no_scenario_still_rejected_in_running_mode():
    assert not grammar.parse("running", "no topology sample_lab").ok
    assert not grammar.parse("running", "no scenario sample").ok


def test_running_no_help_only_lists_access_info_and_reference():
    ctx = grammar.CliContext()
    result = grammar.help("running", "no ", ctx)
    tokens = [line.token for line in result.lines]
    assert tokens == ["access-info", "reference"]


def test_running_no_tab_completion_only_lists_access_info_and_reference():
    ctx = grammar.CliContext()
    result = grammar.complete("running", "no ", ctx)
    assert set(result.candidates) == {"access-info", "reference"}


# ==========================================================================
# Semantic-drift guard: the footer's claims must always
# match the real grammar shape.
# ==========================================================================


def test_semantic_drift_access_info_settable_and_unsettable():
    assert grammar.parse("running", "access-info sample_lab").ok
    assert grammar.parse("running", "no access-info").ok


def test_semantic_drift_topology_settable_never_unsettable():
    assert grammar.parse("running", "topology sample_lab").ok
    assert not grammar.parse("running", "no topology").ok
    assert not grammar.parse("running", "no topology sample_lab").ok


def test_semantic_drift_scenario_settable_never_unsettable():
    assert grammar.parse("running", "scenario sample").ok
    assert not grammar.parse("running", "no scenario").ok
    assert not grammar.parse("running", "no scenario sample").ok


def test_semantic_drift_reference_addable_and_individually_removable():
    assert grammar.parse("running", "reference sample").ok
    assert grammar.parse("running", "no reference sample").ok
