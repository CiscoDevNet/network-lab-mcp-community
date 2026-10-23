# SPDX-License-Identifier: Apache-2.0
"""`no topology <name>`: candidate-based deletion of a STORED
topology definition, from global configuration mode.

This is deliberately distinct from `config-running# no topology`, which
(if it existed) would unset the *active topology selection* instead --
that command does not actually exist
in the current codebase (`config-running` mode only has `no access-info`
and `no reference <name>`; `active_topology` is a mandatory
running-config field with no "unset" path), so there is no real
collision to guard against beyond the one this module documents.

Deletion participates in the same "at most one dirty definition
candidate at a time" rule as topology/access-info/scenario/reference
editing (`CliSession.can_switch_definition()`), extended with a
prospective-absence representation: `definition_candidate is None` while
`definition_kind`/`definition_name`/`definition_original` remain set to
the real committed definition being removed. This reuses
`_enter_definition()`'s existing reload guard and `clear()`'s existing
restore branch unchanged -- re-entering the *same* topology name cancels
the pending deletion for free.

All tests use the isolated `lab_root` fixture; nothing here touches the
real repository's lab/ directory."""

from __future__ import annotations

import pytest

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain


def _add_topology(lab_root, name, **fields):
    data = {"name": name, "description": "", "devices": {}, "links": []}
    data.update(fields)
    lab.write_topology(name, data, lab_root)
    return data


# ==========================================================================
# Grammar: `no` (global-config only), `no topology <name>`, candidate-aware
# provider, inline `?`, Tab, abbreviation
# ==========================================================================


def test_no_topology_global_config_only():
    result = grammar.parse("global", "no topology test_lab")
    assert result.ok
    assert result.action == "global.no_topology"
    assert result.args == {"name": "test_lab"}
    for mode in ("exec", "running", "topology", "device", "access_info", "access_device", "scenario", "reference"):
        result = grammar.parse(mode, "no topology test_lab")
        assert not result.ok, mode


def test_no_bare_incomplete():
    result = grammar.parse("global", "no")
    assert not result.ok
    assert result.error.kind == "incomplete"


def test_no_topology_bare_incomplete():
    result = grammar.parse("global", "no topology")
    assert not result.ok
    assert result.error.kind == "incomplete"


def test_no_help_lists_all_four_kinds():
    """`no ?` lists all four definition kinds (a generalization of the
    original topology-only listing)."""
    ctx = grammar.CliContext()
    result = grammar.help("global", "no ", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("access-info", "Remove an access-info definition"),
        ("topology", "Remove a topology definition"),
        ("scenario", "Remove a scenario definition"),
        ("reference", "Remove a reference definition"),
    ]
    assert result.show_cr is False


def test_no_topology_help_lists_candidate_names():
    ctx = grammar.CliContext(no_topology_candidate_names=("sample", "test_lab"))
    result = grammar.help("global", "no topology ", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("sample", "Existing topology definition"),
        ("test_lab", "Existing topology definition"),
    ]
    assert result.show_cr is False


def test_no_topology_exact_name_inline_help_shows_cr():
    ctx = grammar.CliContext(no_topology_candidate_names=("test_lab",))
    result = grammar.help("global", "no topology test_lab", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("test_lab", "Existing topology definition")
    ]
    assert result.show_cr is True


def test_no_topology_spaced_help_is_cr_only():
    ctx = grammar.CliContext(no_topology_candidate_names=("test_lab",))
    result = grammar.help("global", "no topology test_lab ", ctx)
    assert result.lines == []
    assert result.show_cr is True


def test_no_topology_tab_completion():
    ctx = grammar.CliContext(no_topology_candidate_names=("sample", "test_lab"))
    assert set(grammar.complete("global", "no topology ", ctx).candidates) == {"sample", "test_lab"}
    assert grammar.complete("global", "no topology s", ctx).candidates == ["sample"]


def test_no_topology_abbreviation():
    result = grammar.parse("global", "n topology test_lab")
    assert result.ok
    assert result.action == "global.no_topology"
    result = grammar.parse("global", "no top test_lab")
    assert result.ok
    assert result.action == "global.no_topology"


# ==========================================================================
# Candidate-aware help under dirty state
# ==========================================================================


def test_no_topology_completion_reflects_dirty_topology_identity(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.set_topology_description("changed")
    ctx = climain.build_context(session)
    assert ctx.no_topology_candidate_names == ("sample_lab",)


def test_no_topology_completion_empty_when_different_kind_dirty(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_scenario_definition("brand_new_scenario")
    ctx = climain.build_context(session)
    assert ctx.no_topology_candidate_names == ()


def test_no_topology_completion_empty_when_already_pending_deletion(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_topology_definition("sample_lab")
    ctx = climain.build_context(session)
    assert ctx.no_topology_candidate_names == ()


def test_no_topology_completion_full_when_clean(lab_root):
    _add_topology(lab_root, "lab_a")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    ctx = climain.build_context(session)
    assert set(ctx.no_topology_candidate_names) == {"sample_lab", "lab_a"}


# ==========================================================================
# Candidate deletion: basic semantics
# ==========================================================================


def test_no_topology_does_not_immediately_delete_file(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_topology_definition("sample_lab")
    assert lab.topology_exists("sample_lab", lab_root)  # still on disk
    assert session.definition_kind == "topology"
    assert session.definition_name == "sample_lab"
    assert session.definition_candidate is None
    assert session.definition_original is not None


def test_show_configuration_renders_no_topology(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_topology_definition("sample_lab")
    assert climain.render_configuration_candidate(session) == "no topology sample_lab"


def test_show_running_config_remains_committed_only_before_commit(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_topology_definition("sample_lab")
    text = climain.render_committed_running_config(session)
    assert "sample_lab" in text  # still the committed selection
    # And the definition's own committed content is unaffected.
    assert lab.load_topology("sample_lab", lab_root)["name"] == "sample_lab"


def test_nonexistent_topology_deletion_is_a_clean_error(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    with pytest.raises(cfgmod.ConfigError, match="does not exist"):
        session.remove_topology_definition("does_not_exist_at_all")
    assert session.definition_kind is None
    assert session.overall_dirty() is False


def test_nonexistent_topology_deletion_end_to_end(lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    climain.execute_command_line(session, "no topology does_not_exist_at_all")
    assert capsys.readouterr().out.strip() == "% Topology 'does_not_exist_at_all' does not exist."
    assert session.definition_kind is None


# ==========================================================================
# One-topology-candidate restriction
# ==========================================================================


def test_dirty_edit_of_a_blocks_deletion_of_b(lab_root):
    _add_topology(lab_root, "lab_a", description="original-a")
    _add_topology(lab_root, "lab_b", description="original-b")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("lab_a"))
    session.set_topology_description("edited-a")

    with pytest.raises(cfgmod.ConfigError, match="commit.*clear|clear.*commit"):
        session.remove_topology_definition("lab_b")

    # lab_a's edit is completely unaffected.
    assert session.definition_kind == "topology"
    assert session.definition_name == "lab_a"
    assert session.definition_candidate["description"] == "edited-a"
    assert lab.load_topology("lab_b", lab_root)["description"] == "original-b"


def test_pending_deletion_of_b_blocks_edit_of_a(lab_root):
    _add_topology(lab_root, "lab_a", description="original-a")
    _add_topology(lab_root, "lab_b", description="original-b")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_topology_definition("lab_b")

    ok, message = session.can_switch_definition("topology", "lab_a")
    assert ok is False
    assert "commit" in message and "clear" in message
    assert session.definition_kind == "topology"
    assert session.definition_name == "lab_b"
    assert session.definition_candidate is None  # pending deletion untouched
    assert lab.load_topology("lab_a", lab_root)["description"] == "original-a"


def test_pending_deletion_of_a_blocks_deletion_of_b(lab_root):
    _add_topology(lab_root, "lab_a")
    _add_topology(lab_root, "lab_b")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_topology_definition("lab_a")

    with pytest.raises(cfgmod.ConfigError):
        session.remove_topology_definition("lab_b")

    assert session.definition_name == "lab_a"
    assert climain.render_configuration_candidate(session) == "no topology lab_a"
    assert lab.topology_exists("lab_b", lab_root)


def test_rejected_cross_topology_edit_end_to_end_via_paste(lab_root, capsys):
    """Edit lab_a, then attempt to delete lab_b in
    the same paste -- lab_a's edit is retained, the delete is rejected,
    no second topology candidate is created."""
    _add_topology(lab_root, "lab_a")
    _add_topology(lab_root, "lab_b")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    block = "topology lab_a\n description updated\nexit\nno topology lab_b\n"
    climain.execute_input_block(session, block)
    out = capsys.readouterr().out
    assert "Uncommitted changes exist" in out
    assert session.definition_kind == "topology"
    assert session.definition_name == "lab_a"
    assert session.definition_candidate["description"] == "updated"
    assert lab.load_topology("lab_b", lab_root)["description"] == ""


# ==========================================================================
# Same-topology state transitions ARE allowed
# ==========================================================================


def test_edit_then_delete_same_topology(lab_root):
    _add_topology(lab_root, "lab_a", description="original")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("lab_a"))
    session.set_topology_description("edited")
    session.remove_topology_definition("lab_a")

    assert session.definition_kind == "topology"
    assert session.definition_name == "lab_a"
    assert session.definition_candidate is None
    assert climain.render_configuration_candidate(session) == "no topology lab_a"


def test_delete_then_restore_same_topology_produces_no_net_diff(lab_root):
    _add_topology(lab_root, "lab_a", description="original")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_topology_definition("lab_a")
    session.apply_topology_definition_plan(session.plan_topology_definition("lab_a"))

    assert session.definition_candidate == session.definition_original
    assert session.definition_candidate["description"] == "original"
    assert climain.render_configuration_candidate(session) == ""
    assert session.definition_dirty() is False


def test_delete_then_restore_then_edit_preserves_unrelated_content(lab_root):
    _add_topology(
        lab_root,
        "lab_a",
        description="original",
        devices={"R1": {"type": "iosxr"}},
        links=[],
    )
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_topology_definition("lab_a")
    session.apply_topology_definition_plan(session.plan_topology_definition("lab_a"))
    session.set_topology_description("new description")

    assert session.definition_candidate["devices"] == {"R1": {"type": "iosxr"}}
    text = climain.render_configuration_candidate(session)
    assert "new description" in text
    assert "R1" not in text  # unrelated/unchanged device not rendered as a change


def test_create_new_topology_then_delete_is_net_zero(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("brand_new"))
    session.enter_topology_device("R1")
    session.set_device_field("type", "iosxr")
    climain.h_root(session, {})
    session.remove_topology_definition("brand_new")

    assert session.definition_kind is None
    assert session.definition_name is None
    assert session.overall_dirty() is False
    assert climain.render_configuration_candidate(session) == ""
    session.commit()
    assert not lab.topology_exists("brand_new", lab_root)


# ==========================================================================
# `clear` cancels a pending deletion
# ==========================================================================


def test_clear_cancels_pending_deletion(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_topology_definition("sample_lab")
    assert climain.render_configuration_candidate(session) == "no topology sample_lab"

    session.clear()

    assert climain.render_configuration_candidate(session) == ""
    assert lab.topology_exists("sample_lab", lab_root)
    assert session.definition_candidate == session.definition_original
    ctx = climain.build_context(session)
    assert "sample_lab" in ctx.no_topology_candidate_names


# ==========================================================================
# Active-topology reference safety
# ==========================================================================


def test_commit_fails_closed_when_deleting_the_active_topology(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    assert session.settings_candidate["active_topology"] == "sample_lab"
    session.remove_topology_definition("sample_lab")

    with pytest.raises(cfgmod.CommitValidationError, match="active in running-config"):
        session.commit()

    assert lab.topology_exists("sample_lab", lab_root)
    assert session.definition_kind == "topology"
    assert session.definition_candidate is None  # candidate preserved after failed commit


def test_commit_succeeds_when_active_topology_is_switched_in_the_same_commit(lab_root):
    """A combined commit is safely supported here because
    the guard checks the *effective* settings_candidate, not the
    already-committed selection."""
    _add_topology(lab_root, "lab_c")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    session.select_topology("lab_c")
    session.go_to_global()
    session.remove_topology_definition("sample_lab")

    session.commit()

    assert not lab.topology_exists("sample_lab", lab_root)
    assert lab.read_settings(lab_root)["active_topology"] == "lab_c"


def test_deletion_never_cascades_to_running_config_selection(lab_root):
    _add_topology(lab_root, "lab_a")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_topology_definition("lab_a")
    # Deleting an *inactive* topology must never touch the running-config
    # candidate at all.
    assert session.settings_candidate["active_topology"] == "sample_lab"
    assert session.settings_dirty() is False


# ==========================================================================
# Persistence
# ==========================================================================


def test_successful_commit_removes_exactly_the_intended_topology(lab_root):
    _add_topology(lab_root, "lab_a")
    _add_topology(lab_root, "lab_b")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_topology_definition("lab_a")
    session.commit()

    assert not lab.topology_exists("lab_a", lab_root)
    assert lab.topology_exists("lab_b", lab_root)
    assert lab.topology_exists("sample_lab", lab_root)


def test_commit_clears_topology_candidate_state(lab_root):
    _add_topology(lab_root, "lab_a")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_topology_definition("lab_a")
    session.commit()

    assert session.definition_kind is None
    assert session.definition_name is None
    assert session.definition_original is None
    assert session.definition_candidate is None
    assert climain.render_configuration_candidate(session) == ""


def test_commit_clears_state_so_another_topology_may_be_deleted_next(lab_root):
    _add_topology(lab_root, "lab_a")
    _add_topology(lab_root, "lab_b")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_topology_definition("lab_a")
    session.commit()

    session.remove_topology_definition("lab_b")  # now allowed: prior candidate released
    session.commit()

    assert not lab.topology_exists("lab_a", lab_root)
    assert not lab.topology_exists("lab_b", lab_root)


def test_clear_after_deletion_preserves_file_and_releases_candidate(lab_root):
    _add_topology(lab_root, "lab_a")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_topology_definition("lab_a")
    session.clear()

    assert lab.topology_exists("lab_a", lab_root)
    assert session.overall_dirty() is False


def test_deletion_not_recreatable_by_fresh_process(lab_root):
    """Simulates a process restart: a brand-new CliSession/CliContext
    must no longer list the deleted topology anywhere."""
    _add_topology(lab_root, "lab_a")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_topology_definition("lab_a")
    session.commit()

    fresh_session = cfgmod.CliSession(lab_root)
    fresh_session.enter_configure()
    ctx = climain.build_context(fresh_session)
    assert "lab_a" not in ctx.topology_names
    assert "lab_a" not in ctx.no_topology_candidate_names


# ==========================================================================
# Filesystem safety
# ==========================================================================


@pytest.mark.parametrize("traversal", ["../escape", "../../escape", "/tmp/escape"])
def test_path_traversal_deletion_is_rejected(lab_root, tmp_path, traversal):
    outside = tmp_path / "escape.yaml"
    outside.write_text("name: escape\ndevices: {}\nlinks: []\n")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    with pytest.raises(cfgmod.ConfigError, match="does not exist"):
        session.remove_topology_definition(traversal)
    assert outside.exists()
    assert session.definition_kind is None


def test_path_traversal_rejected_at_persistence_layer_directly(lab_root, tmp_path):
    """Defense in depth: even if a caller bypassed the CLI's exact-match
    guard, lab.delete_topology() itself refuses anything outside
    lab/topologies/."""
    outside = tmp_path / "escape.yaml"
    outside.write_text("name: escape\ndevices: {}\nlinks: []\n")
    with pytest.raises(lab.LabConfigError):
        lab.delete_topology("../escape", lab_root)
    assert outside.exists()


def test_symlink_topology_is_excluded_from_deletion(lab_root, tmp_path):
    outside = tmp_path / "real_topology.yaml"
    outside.write_text("name: symlinked\ndevices: {}\nlinks: []\n")
    (lab_root / "topologies" / "symlinked.yaml").symlink_to(outside)

    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    with pytest.raises(cfgmod.ConfigError, match="does not exist"):
        session.remove_topology_definition("symlinked")
    assert outside.exists()
    assert (lab_root / "topologies" / "symlinked.yaml").is_symlink()


def test_symlink_topology_rejected_at_persistence_layer_directly(lab_root, tmp_path):
    outside = tmp_path / "real_topology2.yaml"
    outside.write_text("name: symlinked2\ndevices: {}\nlinks: []\n")
    (lab_root / "topologies" / "symlinked2.yaml").symlink_to(outside)
    with pytest.raises(lab.LabConfigError):
        lab.delete_topology("symlinked2", lab_root)
    assert outside.exists()
    assert (lab_root / "topologies" / "symlinked2.yaml").is_symlink()


# ==========================================================================
# Multi-line paste
# ==========================================================================


def test_paste_delete_then_clear(lab_root):
    _add_topology(lab_root, "lab_a")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    climain.execute_input_block(session, "no topology lab_a\nclear\n")
    assert lab.topology_exists("lab_a", lab_root)
    assert session.overall_dirty() is False


def test_paste_delete_then_commit(lab_root):
    _add_topology(lab_root, "inactive_lab")
    _add_topology(lab_root, "sibling_lab")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    climain.execute_input_block(session, "no topology inactive_lab\ncommit\n")
    assert not lab.topology_exists("inactive_lab", lab_root)
    assert lab.topology_exists("sibling_lab", lab_root)
    assert lab.topology_exists("sample_lab", lab_root)


def test_paste_show_after_delete(lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    climain.execute_input_block(session, "no topology sample_lab\nshow\n")
    out = capsys.readouterr().out
    assert "no topology sample_lab" in out
