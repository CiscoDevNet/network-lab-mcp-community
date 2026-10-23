# SPDX-License-Identifier: Apache-2.0
"""Symmetric candidate-based deletion for all four stored
definition kinds (`no access-info <name>`, `no topology <name>`, `no
scenario <name>`, `no reference <name>`), from global configuration mode.

This generalizes the topology-only `no topology <name>` (see
test_topology_deletion.py, which remains the authoritative regression
suite for topology-specific behavior and is not duplicated here) via
`CliSession.remove_definition(kind, name)` -- the exact same mechanism,
parametrized by kind. This file focuses on:

- cross-kind conflicts (the single shared definition_candidate slot means
  a dirty topology blocks deleting a scenario, etc. -- a stronger
  invariant than "one topology candidate", proven here across kinds);
- access-info/scenario/reference's own full delete/restore/net-zero
  lifecycle and active-reference (running-config) protection;
- access-info's private-data safety (no credentials in diffs, errors, or
  restored candidates leaking anywhere unexpected);
- running-config selector wording/dynamic-completion/candidate-aware
  filtering for all four kinds (the "config-running# topology ?" UX gap);
- filesystem safety (path traversal, symlinks) for the three newly
  deletable kinds.

All tests use the isolated `lab_root` fixture; nothing here touches the
real repository's lab/ directory, and no access-info fixture password is
ever a real credential."""

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


def _add_access_info(lab_root, name, **fields):
    data = {"name": name, "devices": {}}
    data.update(fields)
    lab.write_access_info(name, data, lab_root)
    return data


def _add_scenario(lab_root, name, **fields):
    data = {"name": name, "description": "", "objectives": []}
    data.update(fields)
    lab.write_scenario(name, data, lab_root)
    return data


def _add_reference(lab_root, name, **fields):
    data = {"name": name, "description": "", "guidance": []}
    data.update(fields)
    lab.write_reference(name, data, lab_root)
    return data


# ==========================================================================
# Grammar: `no ?` lists all four kinds; each is a fixed, dynamically
# completed child
# ==========================================================================


def test_no_access_info_parses():
    result = grammar.parse("global", "no access-info test_lab")
    assert result.ok
    assert result.action == "global.no_access_info"
    assert result.args == {"name": "test_lab"}


def test_no_scenario_parses():
    result = grammar.parse("global", "no scenario maintenance")
    assert result.ok
    assert result.action == "global.no_scenario"


def test_no_reference_parses():
    result = grammar.parse("global", "no reference iosxr_basics")
    assert result.ok
    assert result.action == "global.no_reference"


def test_no_kind_bare_incomplete_for_all_kinds():
    for keyword in ("access-info", "topology", "scenario", "reference"):
        result = grammar.parse("global", f"no {keyword}")
        assert not result.ok, keyword
        assert result.error.kind == "incomplete"


def test_no_access_info_help_lists_candidate_names():
    ctx = grammar.CliContext(no_access_info_candidate_names=("lab_a", "lab_b"))
    result = grammar.help("global", "no access-info ", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("lab_a", "Existing access-info definition"),
        ("lab_b", "Existing access-info definition"),
    ]


def test_no_scenario_help_lists_candidate_names():
    ctx = grammar.CliContext(no_scenario_candidate_names=("sample", "maintenance"))
    result = grammar.help("global", "no scenario ", ctx)
    assert {(line.token, line.description) for line in result.lines} == {
        ("sample", "Existing scenario definition"),
        ("maintenance", "Existing scenario definition"),
    }


def test_no_reference_help_lists_candidate_names():
    ctx = grammar.CliContext(no_reference_candidate_names=("sample", "iosxr_basics"))
    result = grammar.help("global", "no reference ", ctx)
    assert {(line.token, line.description) for line in result.lines} == {
        ("sample", "Existing reference definition"),
        ("iosxr_basics", "Existing reference definition"),
    }


def test_no_kind_exact_name_shows_cr_for_all_kinds():
    ctx = grammar.CliContext(
        no_access_info_candidate_names=("a",),
        no_topology_candidate_names=("t",),
        no_scenario_candidate_names=("s",),
        no_reference_candidate_names=("r",),
    )
    assert grammar.help("global", "no access-info a", ctx).show_cr is True
    assert grammar.help("global", "no topology t", ctx).show_cr is True
    assert grammar.help("global", "no scenario s", ctx).show_cr is True
    assert grammar.help("global", "no reference r", ctx).show_cr is True


def test_no_kind_tab_completion():
    ctx = grammar.CliContext(
        no_access_info_candidate_names=("acc_a",),
        no_scenario_candidate_names=("scen_a",),
        no_reference_candidate_names=("ref_a",),
    )
    assert grammar.complete("global", "no access-info ", ctx).candidates == ["acc_a"]
    assert grammar.complete("global", "no scenario ", ctx).candidates == ["scen_a"]
    assert grammar.complete("global", "no reference ", ctx).candidates == ["ref_a"]


def test_global_parent_help_wording():
    ctx = grammar.CliContext()
    result = grammar.help("global", "", ctx)
    described = {line.token: line.description for line in result.lines}
    assert described["access-info"] == "Create or edit an access-info definition"
    assert described["topology"] == "Create or edit a topology definition"
    assert described["scenario"] == "Create or edit a scenario definition"
    assert described["reference"] == "Create or edit a reference definition"
    assert described["no"] == "Remove a stored definition"


# ==========================================================================
# Cross-kind conflicts: the single shared definition_candidate slot means
# a dirty definition of ANY kind blocks a different definition of ANY
# kind
# ==========================================================================


def test_dirty_topology_blocks_scenario_deletion(lab_root):
    _add_scenario(lab_root, "scenario_b")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.set_topology_description("edited")

    with pytest.raises(cfgmod.ConfigError):
        session.remove_definition("scenario", "scenario_b")

    assert session.definition_kind == "topology"
    assert lab.scenario_exists("scenario_b", lab_root)


def test_dirty_access_info_blocks_reference_edit(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition("access_info", "sample_lab")  # dirty: pending deletion

    ok, message = session.can_switch_definition("reference", "iosxr_basics")
    assert ok is False
    assert "commit" in message and "clear" in message
    assert session.definition_kind == "access_info"
    assert session.definition_candidate is None


def test_dirty_scenario_blocks_reference_deletion(lab_root):
    _add_reference(lab_root, "ref_b")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_scenario_definition("brand_new_scenario")

    with pytest.raises(cfgmod.ConfigError):
        session.remove_definition("reference", "ref_b")

    assert session.definition_kind == "scenario"
    assert lab.reference_exists("ref_b", lab_root)


def test_pending_reference_deletion_blocks_topology_edit(lab_root):
    _add_reference(lab_root, "ref_a")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition("reference", "ref_a")

    ok, message = session.can_switch_definition("topology", "sample_lab")
    assert ok is False
    assert session.definition_kind == "reference"
    assert session.definition_name == "ref_a"


def test_pending_deletion_of_a_blocks_pending_deletion_of_different_kind(lab_root):
    _add_scenario(lab_root, "scenario_c")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition("topology", "sample_lab")

    with pytest.raises(cfgmod.ConfigError):
        session.remove_definition("scenario", "scenario_c")

    assert session.definition_kind == "topology"
    assert climain.render_configuration_candidate(session) == "no topology sample_lab"


def test_same_string_name_different_kind_is_a_different_candidate_identity(lab_root):
    """`topology test_lab` and `access-info test_lab` are
    different stored definitions even though the name string matches."""
    _add_topology(lab_root, "test_lab")
    _add_access_info(lab_root, "test_lab")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("test_lab"))
    session.set_topology_description("edited")

    with pytest.raises(cfgmod.ConfigError):
        session.remove_definition("access_info", "test_lab")

    assert session.definition_kind == "topology"
    assert session.definition_candidate["description"] == "edited"
    assert lab.access_info_exists("test_lab", lab_root)


# ==========================================================================
# Access-info deletion lifecycle + private-data safety
# ==========================================================================


def test_delete_inactive_access_info(lab_root):
    _add_access_info(lab_root, "lab_backup", devices={"R9": {"address": "192.0.2.9", "password": "p"}})
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition("access_info", "lab_backup")
    assert lab.access_info_exists("lab_backup", lab_root)  # not yet
    assert climain.render_configuration_candidate(session) == "no access-info lab_backup"
    session.commit()
    assert not lab.access_info_exists("lab_backup", lab_root)


def test_delete_active_access_info_fails_closed_without_leaking_credentials(lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    assert session.settings_candidate["active_access_info"] == "sample_lab"
    climain.execute_command_line(session, "no access-info sample_lab")
    climain.execute_command_line(session, "commit")
    out = capsys.readouterr().out
    assert "example-password" not in out
    assert "Cannot remove access information 'sample_lab' because it is active in running-config." in out
    assert lab.access_info_exists("sample_lab", lab_root)


def test_access_info_delete_then_restore_preserves_private_fields(lab_root):
    """Restoring after a pending deletion must not create
    a blank access-info and must not discard stored devices/jump-hosts."""
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition("access_info", "sample_lab")
    session.enter_access_info_definition("sample_lab")

    assert session.definition_candidate == session.definition_original
    assert session.definition_candidate["devices"]["R1"]["password"] == "example-password"
    assert "jump1" in session.definition_candidate["jump_hosts"]
    assert climain.render_configuration_candidate(session) == ""


def test_access_info_delete_then_restore_then_edit_preserves_siblings(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition("access_info", "sample_lab")
    session.enter_access_info_definition("sample_lab")
    session.enter_access_info_device("R2")
    session.set_device_field("username", "new-user")

    assert session.definition_candidate["devices"]["R1"]["password"] == "example-password"  # sibling untouched
    text = climain.render_configuration_candidate(session)
    assert "new-user" in text
    assert "R1" not in text


def test_create_new_access_info_then_delete_is_net_zero(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("brand_new_access")
    session.enter_access_info_device("R1")
    session.set_device_field("address", "192.0.2.1")
    climain.h_root(session, {})
    session.remove_definition("access_info", "brand_new_access")

    assert session.definition_kind is None
    assert climain.render_configuration_candidate(session) == ""
    session.commit()
    assert not lab.access_info_exists("brand_new_access", lab_root)


def test_nonexistent_access_info_deletion_is_clean(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    with pytest.raises(cfgmod.ConfigError, match="does not exist"):
        session.remove_definition("access_info", "does_not_exist")
    assert session.definition_kind is None


def test_show_configuration_never_renders_access_info_field_diff_for_deletion(lab_root):
    """Only the single whole-definition line, never a
    per-field/per-device deletion diff that could leak credentials."""
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition("access_info", "sample_lab")
    text = climain.render_configuration_candidate(session)
    assert text == "no access-info sample_lab"
    assert "example-password" not in text
    assert "R1" not in text
    assert "R2" not in text


# ==========================================================================
# Scenario deletion lifecycle + active-scenario protection (sections
# 9/10/47)
# ==========================================================================


def test_delete_inactive_scenario(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition("scenario", "failover_test")
    assert climain.render_configuration_candidate(session) == "no scenario failover_test"
    session.commit()
    assert not lab.scenario_exists("failover_test", lab_root)


def test_delete_active_scenario_fails_closed(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    assert session.settings_candidate["active_scenario"] == "sample"
    session.remove_definition("scenario", "sample")
    with pytest.raises(cfgmod.CommitValidationError, match="active in running-config"):
        session.commit()
    assert lab.scenario_exists("sample", lab_root)


def test_active_scenario_switch_plus_delete_old_succeeds_in_same_commit(lab_root):
    _add_scenario(lab_root, "scenario_new")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    session.set_scenario("scenario_new")
    session.go_to_global()
    session.remove_definition("scenario", "sample")
    session.commit()
    assert not lab.scenario_exists("sample", lab_root)
    assert lab.read_settings(lab_root)["active_scenario"] == "scenario_new"


def test_scenario_delete_then_restore_then_edit_net_diff(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition("scenario", "failover_test")
    session.enter_scenario_definition("failover_test")
    session.definition_candidate["objectives"] = ["new objective"]
    text = climain.render_configuration_candidate(session)
    assert "new objective" in text


def test_scenario_create_then_delete_net_zero(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_scenario_definition("temp_scenario")
    session.remove_definition("scenario", "temp_scenario")
    assert session.definition_kind is None
    assert climain.render_configuration_candidate(session) == ""


# ==========================================================================
# Reference deletion lifecycle + active-reference-set protection
# ==========================================================================


def test_delete_inactive_reference(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    assert session.settings_candidate["active_references"] == ["sample"]
    session.remove_definition("reference", "iosxr_basics")
    assert climain.render_configuration_candidate(session) == "no reference iosxr_basics"
    session.commit()
    assert not lab.reference_exists("iosxr_basics", lab_root)


def test_delete_active_reference_fails_closed(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition("reference", "sample")
    with pytest.raises(cfgmod.CommitValidationError, match="active in running-config"):
        session.commit()
    assert lab.reference_exists("sample", lab_root)


def test_active_reference_deactivate_plus_delete_succeeds_in_same_commit(lab_root):
    _add_reference(lab_root, "extra_ref")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    session.add_reference("extra_ref")
    session.remove_reference("sample")
    session.go_to_global()
    session.remove_definition("reference", "sample")
    session.commit()

    assert not lab.reference_exists("sample", lab_root)
    assert lab.reference_exists("extra_ref", lab_root)  # sibling untouched
    assert lab.read_settings(lab_root)["active_references"] == ["extra_ref"]


def test_unrelated_active_reference_unaffected_by_deletion(lab_root):
    _add_reference(lab_root, "kept_ref")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    session.add_reference("kept_ref")
    session.go_to_global()
    session.remove_definition("reference", "iosxr_basics")  # inactive, unrelated
    session.commit()

    assert not lab.reference_exists("iosxr_basics", lab_root)
    settings = lab.read_settings(lab_root)
    assert settings["active_references"] == ["sample", "kept_ref"]


def test_reference_create_then_delete_net_zero(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_reference_definition("temp_reference")
    session.remove_definition("reference", "temp_reference")
    assert session.definition_kind is None
    assert climain.render_configuration_candidate(session) == ""


# ==========================================================================
# Clear symmetry across all kinds
# ==========================================================================


@pytest.mark.parametrize(
    "kind,name",
    [("access_info", "sample_lab"), ("topology", "sample_lab"), ("scenario", "sample"), ("reference", "sample")],
)
def test_clear_cancels_pending_deletion_for_every_kind(lab_root, kind, name):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition(kind, name)
    session.clear()
    assert session.overall_dirty() is False
    assert climain.render_configuration_candidate(session) == ""


# ==========================================================================
# Fresh-process persistence
# ==========================================================================


def test_deletion_persists_across_fresh_sessions_for_every_kind(lab_root):
    _add_scenario(lab_root, "scenario_x")
    _add_reference(lab_root, "reference_x")
    _add_access_info(lab_root, "access_x")

    for kind, name in (("scenario", "scenario_x"), ("reference", "reference_x"), ("access_info", "access_x")):
        session = cfgmod.CliSession(lab_root)
        session.enter_configure()
        session.remove_definition(kind, name)
        session.commit()

    fresh = cfgmod.CliSession(lab_root)
    fresh.enter_configure()
    ctx = climain.build_context(fresh)
    assert "scenario_x" not in ctx.scenario_names
    assert "reference_x" not in ctx.reference_names
    assert "access_x" not in ctx.access_info_names


# ==========================================================================
# Running-config selector: dynamic completion + wording
# ==========================================================================


def test_running_topology_selector_help_is_dynamic(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    ctx = climain.build_context(session)
    result = grammar.help("running", "topology ", ctx)
    tokens = [line.token for line in result.lines]
    assert "sample_lab" in tokens
    assert result.lines[0].description == "Topology definition name"


def test_running_selector_wording():
    ctx = grammar.CliContext()
    result = grammar.help("running", "", ctx)
    described = {line.token: line.description for line in result.lines}
    assert described["access-info"] == "Select access-info for MCP"
    assert described["topology"] == "Select topology for MCP"
    assert described["scenario"] == "Select scenario for MCP"
    assert described["reference"] == "Activate reference for MCP"


def test_running_selector_exact_token_shows_cr(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    ctx = climain.build_context(session)
    result = grammar.help("running", "topology sample_lab", ctx)
    assert result.show_cr is True


def test_running_selector_execution_still_validates_nonexistent_name(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    with pytest.raises(cfgmod.ConfigError, match="does not exist"):
        session.select_topology("does_not_exist_at_all")


def test_pending_topology_deletion_excluded_from_running_selector(lab_root):
    """A topology pending deletion must not be
    offered as a valid effective running-config selection."""
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition("topology", "sample_lab")
    ctx = climain.build_context(session)
    assert "sample_lab" not in ctx.running_topology_names


def test_pending_reference_deletion_excluded_from_running_selector(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition("reference", "iosxr_basics")
    ctx = climain.build_context(session)
    assert "iosxr_basics" not in ctx.running_reference_names
    assert "sample" in ctx.running_reference_names


def test_pending_access_info_deletion_excluded_from_running_selector(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition("access_info", "sample_lab")
    ctx = climain.build_context(session)
    assert "sample_lab" not in ctx.running_access_info_names


def test_pending_scenario_deletion_excluded_from_running_selector(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition("scenario", "sample")
    ctx = climain.build_context(session)
    assert "sample" not in ctx.running_scenario_names
    assert "failover_test" in ctx.running_scenario_names


def test_running_selector_unaffected_by_dirty_edit_of_different_topology(lab_root):
    """The exclusion is scoped to the exact pending-deleted identity --
    an unrelated dirty *edit* (not deletion) leaves the running selector
    fully populated (execution's can_switch_definition() is what blocks
    the actual conflicting selection attempt, not this list)."""
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.set_topology_description("edited")
    ctx = climain.build_context(session)
    assert "sample_lab" in ctx.running_topology_names


# ==========================================================================
# Filesystem safety for access-info/scenario/reference
# ==========================================================================


@pytest.mark.parametrize(
    "kind,delete_fn,is_deletable_fn",
    [
        ("access_info", lab.delete_access_info, lab.access_info_is_deletable),
        ("scenario", lab.delete_scenario, lab.scenario_is_deletable),
        ("reference", lab.delete_reference, lab.reference_is_deletable),
    ],
)
def test_path_traversal_rejected_at_persistence_layer(lab_root, tmp_path, kind, delete_fn, is_deletable_fn):
    outside = tmp_path / "escape.yaml"
    outside.write_text("name: escape\n")
    assert is_deletable_fn("../escape", lab_root) is False
    with pytest.raises(lab.LabConfigError):
        delete_fn("../escape", lab_root)
    assert outside.exists()


@pytest.mark.parametrize(
    "kind,directory,delete_fn",
    [
        ("access_info", "access-info", lab.delete_access_info),
        ("scenario", "scenarios", lab.delete_scenario),
        ("reference", "references", lab.delete_reference),
    ],
)
def test_symlink_rejected_at_persistence_layer(lab_root, tmp_path, kind, directory, delete_fn):
    outside = tmp_path / f"real_{kind}.yaml"
    outside.write_text("name: symlinked\n")
    (lab_root / directory / "symlinked.yaml").symlink_to(outside)
    with pytest.raises(lab.LabConfigError):
        delete_fn("symlinked", lab_root)
    assert outside.exists()
    assert (lab_root / directory / "symlinked.yaml").is_symlink()


def test_symlinked_access_info_excluded_at_candidate_creation(lab_root, tmp_path):
    outside = tmp_path / "real_access_info.yaml"
    outside.write_text("name: symlinked\ndevices: {}\n")
    (lab_root / "access-info" / "symlinked.yaml").symlink_to(outside)

    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    with pytest.raises(cfgmod.ConfigError, match="does not exist"):
        session.remove_definition("access_info", "symlinked")
    assert outside.exists()


# ==========================================================================
# Paste regression: edit + cross-kind delete rejection
# ==========================================================================


def test_paste_edit_topology_then_delete_scenario_rejected(lab_root, capsys):
    _add_scenario(lab_root, "scenario_z")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    block = "topology sample_lab\n description updated\nexit\nno scenario scenario_z\n"
    climain.execute_input_block(session, block)
    out = capsys.readouterr().out
    assert "Uncommitted changes exist" in out
    assert session.definition_kind == "topology"
    assert session.definition_candidate["description"] == "updated"
    assert lab.scenario_exists("scenario_z", lab_root)
