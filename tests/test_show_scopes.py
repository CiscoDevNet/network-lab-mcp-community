# SPDX-License-Identifier: Apache-2.0
"""Tests for context-aware `show running-config` / `show configuration`
scoping, and the EXEC-only restriction on `show version`.

General rule: in EXEC/global/running mode, `show running-config` is the
committed MCP running-config selection (unchanged). In every other mode
(a topology/access-info/scenario/reference definition, or its nested
device/jump-host submode), `show running-config` means "what is currently
*fully* committed for *this* object" (re-read fresh from disk, empty if
never committed), while `show configuration` means **uncommitted changes
only** -- a bounded, pragmatic delta, not a full candidate dump (see
docs/cli_reference.md "show configuration"). Explicit local access-info
rendering (both committed and candidate views) shows passwords in clear
text; MCP/log/error/help/completion/history privacy is covered separately
in test_lab_loading_and_privacy.py and test_grammar.py."""

from __future__ import annotations

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain


# ---- EXEC / global / running: unchanged MCP-selection semantics ----


def test_exec_show_running_config_is_mcp_selection(lab_root):
    session = cfgmod.CliSession(lab_root)
    text = climain.render_committed_running_config(session)
    assert "topology" in text and "sample_lab" in text
    assert "scenario" in text and "sample" in text


def test_global_mode_show_running_config_unaffected_by_open_definition(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("brand_new"))
    session.mode = "global"  # as if the operator `exit`-ed back up
    climain.h_show_running_config(session, {})
    # Still the MCP selection, not the open (unrelated) topology definition.
    text = climain.render_committed_running_config(session)
    assert "sample_lab" in text


def test_running_mode_show_running_config_is_committed_mcp_selection(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    session.select_topology("sample_lab")  # no-op, already committed value
    text = climain.render_committed_running_config(session)
    assert "sample_lab" in text


def test_running_mode_show_configuration_is_selection_delta(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    # Selecting the already-committed value is not a change -- no delta.
    session.select_topology("sample_lab")
    assert climain.render_configuration_candidate(session) == ""

    # A real change to the selection shows up as a delta.
    session.apply_topology_definition_plan(session.plan_topology_definition("new_topo"))
    session.mode = "running"
    session.select_topology("new_topo")
    text = climain.render_configuration_candidate(session)
    assert "new_topo" in text
    assert "sample_lab" not in text  # unrelated unchanged selections are not repeated


# ---- show version: EXEC only ----


def test_show_version_grammar_rejected_outside_exec():
    from network_lab_mcp.cli import grammar

    for mode in ("global", "running", "topology", "device", "access_info", "access_device", "scenario", "reference"):
        result = grammar.parse(mode, "show version")
        assert not result.ok, mode


# ---- new (never-committed) access-info definition ----


def test_new_access_info_running_config_is_empty(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("test")
    assert climain.render_committed_definition(session) == ""


def test_new_access_info_with_nothing_configured_yet_shows_no_delta(lab_root):
    # Entering a brand-new mode alone must not create fake output.
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("test")
    assert climain.render_configuration_candidate(session) == ""


def test_new_access_info_configuration_shows_only_its_own_delta(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("test")
    session.enter_access_info_device("R1")
    session.set_device_field("type", "iosxr")
    text = climain.render_configuration_candidate(session)
    assert "sample_lab" not in text  # never another definition's data
    assert "test" in text
    assert "R1" in text


def test_new_access_info_device_running_config_empty_until_commit(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("test")
    session.enter_access_info_device("R1")
    session.set_device_field("type", "iosxr")
    session.set_device_field("address", "192.0.2.50")
    session.set_device_field("password", "s3cret")

    assert climain.render_committed_definition(session) == ""
    candidate_text = climain.render_configuration_candidate(session)
    assert "R1" in candidate_text
    assert "192.0.2.50" in candidate_text
    # Explicit local access-info rendering shows password in clear text
    # (this is a lab tool -- see README.md "Password display policy");
    # MCP/log/error/help/completion/history privacy is unaffected and
    # tested separately.
    assert "s3cret" in candidate_text
    assert "********" not in candidate_text

    session.commit()
    committed_text = climain.render_committed_definition(session)
    assert "R1" in committed_text
    assert "192.0.2.50" in committed_text
    assert "s3cret" in committed_text


# ---- existing access-info definition ----


def test_existing_access_info_running_config_vs_configuration(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.enter_access_info_device("R1")
    session.set_device_field("address", "192.0.2.99")

    committed_text = climain.render_committed_definition(session)
    candidate_text = climain.render_configuration_candidate(session)
    # show running-config: the full committed block, clear-text password.
    assert "192.0.2.11" in committed_text  # original committed address
    assert "192.0.2.99" not in committed_text
    assert "example-password" in committed_text
    assert "********" not in committed_text
    # show configuration: only the changed field -- unchanged scalar fields
    # (type/transport/port/username/password) are not repeated.
    assert candidate_text == "access-info sample_lab\n device R1\n  address 192.0.2.99\n !\n!"


def test_clear_restores_committed_view_and_empties_the_delta(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.enter_access_info_device("R1")
    session.set_device_field("address", "192.0.2.99")
    assert "192.0.2.99" in climain.render_configuration_candidate(session)

    session.clear()
    assert not lab.access_info_exists("test", lab_root)  # unrelated: sanity
    # No uncommitted changes left -> no delta at all.
    assert climain.render_configuration_candidate(session) == ""
    # The full committed view is back to the original value.
    assert "192.0.2.11" in climain.render_committed_definition(session)
    assert "192.0.2.99" not in climain.render_committed_definition(session)


def test_clear_on_brand_new_definition_leaves_no_disk_write_and_falls_back(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("test")
    session.enter_access_info_device("R1")
    session.set_device_field("address", "192.0.2.50")
    session.clear()
    assert session.mode == "global"
    assert session.definition_kind is None
    assert not lab.access_info_exists("test", lab_root)


# ---- topology ----


def test_new_topology_running_config_is_empty(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("brand_new"))
    assert climain.render_committed_definition(session) == ""


def test_existing_topology_running_config_vs_configuration(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.set_topology_description("Edited description.")

    committed_text = climain.render_committed_definition(session)
    candidate_text = climain.render_configuration_candidate(session)
    assert "Edited description." not in committed_text
    assert "Edited description." in candidate_text
    # No access fields, links preserved on the committed side.
    assert "address" not in committed_text and "password" not in committed_text


def test_topology_device_mode_scopes_to_one_device(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.enter_topology_device("R1")
    committed_text = climain.render_committed_definition(session)
    assert "R1" in committed_text
    assert "R2" not in committed_text  # scoped away, even though R2 exists in the topology


def test_new_topology_device_running_config_empty(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.enter_topology_device("BRAND_NEW_DEVICE")
    session.set_device_field("type", "host")
    assert climain.render_committed_definition(session) == ""
    assert "BRAND_NEW_DEVICE" in climain.render_configuration_candidate(session)


# ---- scenario / reference ----


def test_new_scenario_running_config_is_empty(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_scenario_definition("brand_new_scenario")
    assert climain.render_committed_definition(session) == ""
    assert "brand_new_scenario" in climain.render_configuration_candidate(session)


def test_existing_scenario_running_config_vs_configuration(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_scenario_definition("failover_test")
    session.definition_candidate["description"] = "Edited."
    committed_text = climain.render_committed_definition(session)
    candidate_text = climain.render_configuration_candidate(session)
    assert "Edited." not in committed_text
    assert "Edited." in candidate_text


def test_new_reference_running_config_is_empty(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_reference_definition("brand_new_reference")
    assert climain.render_committed_definition(session) == ""


def test_scenario_edit_with_fake_editor_then_show_scopes(lab_root, monkeypatch, fake_editor):
    from network_lab_mcp.cli import main as climain2

    script = fake_editor(
        "import yaml\n"
        "data = yaml.safe_load(open(path))\n"
        "data['description'] = 'edited via fake editor'\n"
        "yaml.safe_dump(data, open(path, 'w'))\n"
    )
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_scenario_definition("failover_test")
    climain2.h_edit(session, {})
    committed_text = climain.render_committed_definition(session)
    candidate_text = climain.render_configuration_candidate(session)
    assert "edited via fake editor" not in committed_text
    assert "edited via fake editor" in candidate_text


# ---- after commit, running-config reflects the new committed state ----


def test_after_commit_running_config_shows_newly_committed_state(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("brand_new"))
    session.set_topology_description("Just committed.")
    session.commit()
    assert "Just committed." in climain.render_committed_definition(session)


# ==========================================================================
# Whole-object deletion was invisible in `show
# configuration`. `_named_objects_delta()` only ever walked the candidate
# map, so a device/jump-host removed via `no device <name>` / `no
# jump-host <name>` never appeared in the delta at all, even though
# `commit` correctly persisted the removal -- fixed to also walk the
# committed map for names now absent from the candidate, rendered as a
# single ` no <keyword> <name>` line (the same convention as
# _running_config_delta_lines()'s `no reference <name>`).
# ==========================================================================


def test_deleted_device_appears_in_configuration_delta_as_no_device(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.remove_device("R2")
    assert climain.render_configuration_candidate(session) == "access-info sample_lab\n no device R2\n!"
    # Committed state is untouched before commit.
    assert "R2" in lab.load_access_info("sample_lab", lab_root)["devices"]


def test_deleted_jump_host_appears_in_configuration_delta_as_no_jump_host(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.remove_jump_host("jump1")
    assert climain.render_configuration_candidate(session) == "access-info sample_lab\n no jump-host jump1\n!"


def test_deletion_diff_disappears_after_commit(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.remove_device("R2")
    session.commit()
    assert climain.render_configuration_candidate(session) == ""
    assert "R2" not in lab.load_access_info("sample_lab", lab_root)["devices"]


def test_clear_after_deletion_restores_object_and_diff_disappears(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.remove_device("R2")
    assert " no device R2" in climain.render_configuration_candidate(session)
    session.clear()
    assert "R2" in session.definition_candidate["devices"]
    assert climain.render_configuration_candidate(session) == ""


def test_deletion_diff_shown_alongside_addition(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.remove_device("R2")
    session.enter_access_info_device("R5")
    session.set_device_field("type", "iosxr")
    climain.h_exit(session, {})  # back to access_info mode: whole-definition scope
    text = climain.render_configuration_candidate(session)
    assert " no device R2" in text
    assert " device R5" in text
    assert "  type iosxr" in text


# ---- net-zero candidate changes never appear in the delta ----


def test_add_then_delete_new_device_produces_no_net_diff(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.enter_access_info_device("R5")
    session.set_device_field("type", "iosxr")
    session.remove_device("R5")
    assert climain.render_configuration_candidate(session) == ""


def test_delete_then_restore_identical_device_produces_no_net_diff(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    original_r1 = dict(session.definition_candidate["devices"]["R1"])
    session.remove_device("R1")
    session.enter_access_info_device("R1")
    for field_name, value in original_r1.items():
        session.set_device_field(field_name, value)
    assert session.definition_candidate["devices"]["R1"] == original_r1
    assert climain.render_configuration_candidate(session) == ""


def test_modify_then_restore_leaf_produces_no_net_diff(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.enter_access_info_device("R1")
    session.set_device_field("address", "192.0.2.99")
    session.set_device_field("address", "192.0.2.11")  # restore original
    assert climain.render_configuration_candidate(session) == ""


# ---- leaf deletion rendering ----


def test_cleared_leaf_of_existing_object_renders_as_no_field(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.enter_access_info_device("R1")
    session.clear_device_field("address")
    assert climain.render_configuration_candidate(session) == "access-info sample_lab\n device R1\n  no address\n !\n!"


def test_leaf_absent_from_committed_added_then_cleared_produces_no_diff(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.enter_access_info_device("R2")  # R2 has no committed username
    session.set_device_field("username", "temp")
    session.clear_device_field("username")
    assert climain.render_configuration_candidate(session) == ""


# ---- rendering scope is never commit scope ----


def test_device_scoped_show_never_implies_it_is_the_only_candidate_object(lab_root):
    """`show`/`show configuration` inside a device submode is scoped to
    just that device (existing convention), but this must never be
    confused with what `commit` actually persists -- see
    test_commit_from_nested_device_mode_preserves_sibling_devices in
    test_config.py for the commit-side proof."""
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.enter_access_info_device("R2")
    session.set_device_field("address", "192.0.2.77")
    scoped_text = climain.render_configuration_candidate(session)
    assert scoped_text == "access-info sample_lab\n device R2\n  address 192.0.2.77\n !\n!"
    assert "R1" not in scoped_text
    # The full candidate, unlike the scoped view, still has R1.
    assert "R1" in session.definition_candidate["devices"]
