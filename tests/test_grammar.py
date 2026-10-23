# SPDX-License-Identifier: Apache-2.0
"""Tests for the command grammar single source of truth: parsing, unique
abbreviation, ambiguity/incomplete/invalid-input detection, fixed-keyword
case-insensitivity, object-identifier case-sensitivity, "select or create"
identifier help, and context-sensitive completion/help across every CLI
mode (EXEC, global, running-config, topology/device, access-info/
access-device, scenario, reference)."""

from __future__ import annotations

import pytest

from network_lab_mcp.cli import grammar


def make_ctx(**kwargs) -> grammar.CliContext:
    return grammar.CliContext(**kwargs)


# ---- parsing / abbreviation ----


def test_unique_abbreviation_resolves():
    result = grammar.parse("exec", "conf")
    assert result.ok
    assert result.action == "exec.configure"


def test_abbreviation_is_context_sensitive():
    result = grammar.parse("global", "top srv6_lab")
    assert result.ok
    assert result.action == "global.topology"
    assert result.args == {"name": "srv6_lab"}

    result = grammar.parse("access_device", "tra ssh")
    assert result.ok
    assert result.action == "access_device.set_transport"
    assert result.args == {"value": "ssh"}


def test_ambiguous_abbreviation_rejected():
    # global root has both "running-config" and "reference" -> "r" is ambiguous.
    result = grammar.parse("global", "r")
    assert not result.ok
    assert result.error.kind == "ambiguous"
    assert result.error.token == "r"


def test_ambiguous_end_exit():
    # global root has both "end" and "exit" -> "e" is ambiguous.
    result = grammar.parse("global", "e")
    assert not result.ok
    assert result.error.kind == "ambiguous"


def test_incomplete_command():
    result = grammar.parse("access_device", "transport")
    assert not result.ok
    assert result.error.kind == "incomplete"
    assert result.error.span is None


def test_unknown_command_at_first_token():
    result = grammar.parse("exec", "foo")
    assert not result.ok
    assert result.error.kind == "unknown"
    assert result.error.token == "foo"


def test_invalid_transport_value_has_caret_span():
    result = grammar.parse("access_device", "transport invalid")
    assert not result.ok
    assert result.error.kind == "invalid"
    assert result.error.token == "invalid"
    assert result.error.span == (10, 17)
    assert "ssh" in result.error.detail


def test_invalid_port_value():
    result = grammar.parse("access_device", "port 99999")
    assert not result.ok
    assert result.error.kind == "invalid"
    assert "65535" in result.error.detail

    ok_result = grammar.parse("access_device", "port 22")
    assert ok_result.ok
    assert ok_result.args == {"value": "22"}


def test_configure_terminal_alias():
    result = grammar.parse("exec", "configure terminal")
    assert result.ok
    assert result.action == "exec.configure"


def test_no_reference_command():
    result = grammar.parse("running", "no reference srv6")
    assert result.ok
    assert result.action == "running.reference_remove"
    assert result.args == {"name": "srv6"}


def test_running_topology_scenario_reference_are_selectors():
    assert grammar.parse("running", "topology srv6_lab").action == "running.topology"
    assert grammar.parse("running", "scenario troubleshoot").action == "running.scenario"
    assert grammar.parse("running", "reference srv6").action == "running.reference_add"


def test_description_is_rest_of_line():
    result = grammar.parse("topology", "description SRv6 lab with two PEs")
    assert result.ok
    assert result.args == {"text": "SRv6 lab with two PEs"}


def test_topology_device_mode_has_no_access_fields():
    # Safe topology device mode only knows about "type" -- address/transport/
    # port/username/password moved to access-info's device submode.
    for keyword in ("address", "transport", "port", "username", "password"):
        result = grammar.parse("device", f"{keyword} something")
        assert not result.ok, keyword


def test_access_device_mode_has_full_field_set():
    assert grammar.parse("access_device", "address 192.0.2.11").ok
    assert grammar.parse("access_device", "username example-user").ok
    assert grammar.parse("access_device", "no username").ok
    assert grammar.parse("access_device", "no password").ok
    assert grammar.parse("access_device", "no port").ok


def test_topology_and_scenario_and_reference_have_edit_command():
    assert grammar.parse("topology", "edit").action == "topology.edit"
    assert grammar.parse("scenario", "edit").action == "scenario.edit"
    assert grammar.parse("reference", "edit").action == "reference.edit"


def test_abort_is_no_longer_recognized():
    for mode in ("global", "running", "topology", "device", "access_info", "access_device", "scenario", "reference"):
        result = grammar.parse(mode, "abort")
        assert not result.ok, mode
        assert result.error.kind == "unknown"


def test_clear_replaces_abort_everywhere():
    for mode in ("global", "running", "topology", "device", "access_info", "access_device", "scenario", "reference"):
        result = grammar.parse(mode, "clear")
        assert result.ok, mode
        assert result.action == f"{mode}.clear"


# ---- fixed-keyword case-insensitivity ----


def test_fixed_keywords_case_insensitive():
    for line in ("configure", "CONFIGURE", "Configure", "CoNfIgUrE"):
        result = grammar.parse("exec", line)
        assert result.ok
        assert result.action == "exec.configure"


def test_nested_fixed_keyword_case_insensitive():
    result = grammar.parse("access_device", "TRANSPORT SSH")
    assert result.ok
    assert result.args == {"value": "ssh"}


# ---- object-identifier case sensitivity (grammar does not fold case) ----


def test_object_identifier_case_preserved_by_parser():
    result = grammar.parse("global", "topology SRv6_Lab")
    assert result.ok
    # The grammar itself never case-folds or validates existence; it hands
    # the exact entered case to the caller (cli/config.py) unchanged.
    assert result.args == {"name": "SRv6_Lab"}


# ---- device.type enum (shared by topology device mode and access-info device mode) ----


def test_device_type_accepts_exact_and_case_insensitive_values():
    for mode in ("device", "access_device"):
        for line, expected in (
            ("type iosxr", "iosxr"),
            ("type IOSXE", "iosxe"),
            ("type NxOs", "nxos"),
            ("type HOST", "host"),
            ("type Host", "host"),
        ):
            result = grammar.parse(mode, line)
            assert result.ok, (mode, line)
            assert result.args == {"value": expected}


def test_device_type_accepts_unambiguous_abbreviation():
    result = grammar.parse("device", "type nx")
    assert result.ok
    assert result.args == {"value": "nxos"}

    result_host = grammar.parse("access_device", "type h")
    assert result_host.ok
    assert result_host.args == {"value": "host"}


def test_device_type_exact_ios_is_not_ambiguous_with_iosxr_iosxe():
    """`ios` is a first-class exact type -- exact match must win before
    abbreviation resolution, so it is never rejected merely because it is
    also a prefix of `iosxr`/`iosxe`."""
    result = grammar.parse("device", "type ios")
    assert result.ok
    assert result.action == "device.set_type"
    assert result.args["value"] == "ios"


@pytest.mark.parametrize("prefix", ["i", "io"])
def test_device_type_short_prefix_is_ambiguous_across_all_ios_variants(prefix):
    result = grammar.parse("device", f"type {prefix}")
    assert not result.ok
    assert result.error.kind == "invalid"
    assert "Ambiguous" in result.error.detail
    assert "iosxr" in result.error.detail and "iosxe" in result.error.detail and "ios" in result.error.detail


def test_device_type_iosx_is_ambiguous_between_iosxr_and_iosxe_only():
    """`iosx` does not prefix-match the exact `ios` token itself, so the
    ambiguity is only between `iosxr`/`iosxe`."""
    result = grammar.parse("device", "type iosx")
    assert not result.ok
    assert result.error.kind == "invalid"
    assert "Matches: iosxe, iosxr." in result.error.detail


def test_device_type_rejects_unknown_value():
    result = grammar.parse("access_device", "type junos")
    assert not result.ok
    assert result.error.kind == "invalid"
    assert "Invalid device type" in result.error.detail


# ---- completion ----


def test_unique_literal_completion():
    ctx = make_ctx()
    result = grammar.complete("exec", "conf", ctx)
    assert result.candidates == ["configure"]


def test_ambiguous_literal_completion_lists_candidates():
    ctx = make_ctx()
    result = grammar.complete("global", "r", ctx)
    assert set(result.candidates) == {"running-config", "reference"}


def test_zero_candidate_completion():
    ctx = make_ctx()
    result = grammar.complete("exec", "zzz", ctx)
    assert result.candidates == []


def test_dynamic_completion_is_case_sensitive_and_preserves_case():
    ctx = make_ctx(topology_candidate_device_names=("R1", "R2", "PE1"))
    result = grammar.complete("topology", "device R", ctx)
    assert set(result.candidates) == {"R1", "R2"}

    result_lower = grammar.complete("topology", "device r", ctx)
    assert result_lower.candidates == []  # must not case-fold to match R1/R2


def test_access_info_device_completion_uses_its_own_candidate_names():
    ctx = make_ctx(access_info_candidate_device_names=("R1", "PC1"))
    result = grammar.complete("access_info", "device P", ctx)
    assert result.candidates == ["PC1"]


def test_topology_name_completion_case_sensitive():
    ctx = make_ctx(topology_names=("srv6_lab",))
    assert grammar.complete("global", "topology s", ctx).candidates == ["srv6_lab"]
    assert grammar.complete("global", "topology S", ctx).candidates == []


def test_running_topology_completion_uses_existing_topology_names():
    ctx = make_ctx(running_topology_names=("srv6_lab", "sample_lab"))
    result = grammar.complete("running", "topology s", ctx)
    assert set(result.candidates) == {"srv6_lab", "sample_lab"}


def test_access_info_name_completion():
    ctx = make_ctx(access_info_names=("lab_devices",))
    assert grammar.complete("global", "access-info l", ctx).candidates == ["lab_devices"]


def test_password_never_completes():
    ctx = make_ctx()
    assert grammar.complete("access_device", "password ", ctx).candidates == []
    assert grammar.complete("access_device", "password some-text", ctx).candidates == []


def test_no_reference_completion_uses_candidate_references():
    ctx = make_ctx(candidate_reference_names=("srv6", "iosxr_basics"))
    result = grammar.complete("running", "no reference ", ctx)
    assert set(result.candidates) == {"srv6", "iosxr_basics"}


def test_device_type_tab_completion_exposes_only_fixed_enum():
    ctx = make_ctx()
    result = grammar.complete("device", "type ", ctx)
    assert set(result.candidates) == {"iosxr", "iosxe", "ios", "nxos", "host"}

    result_prefix = grammar.complete("access_device", "type n", ctx)
    assert result_prefix.candidates == ["nxos"]

    result_host = grammar.complete("device", "type h", ctx)
    assert result_host.candidates == ["host"]


# ---- context-sensitive help ----


def test_bare_help_lists_all_global_root_commands():
    ctx = make_ctx()
    result = grammar.help("global", "", ctx)
    tokens = [line.token for line in result.lines]
    assert tokens == [
        "running-config",
        "discover",
        "access-info",
        "topology",
        "scenario",
        "reference",
        "no",
        "show",
        "clear",
        "commit",
        "end",
        "exit",
        "help",
    ]
    assert result.show_cr is False


def test_bare_help_lists_all_running_root_commands():
    ctx = make_ctx()
    result = grammar.help("running", "", ctx)
    tokens = [line.token for line in result.lines]
    assert tokens == [
        "access-info",
        "topology",
        "scenario",
        "reference",
        "no",
        "show",
        "clear",
        "commit",
        "root",
        "end",
        "exit",
        "help",
    ]


def test_partial_token_help():
    ctx = make_ctx()
    result = grammar.help("exec", "con", ctx)
    assert [line.token for line in result.lines] == ["configure"]
    assert result.show_cr is False


def test_next_token_help_for_enum_argument():
    ctx = make_ctx()
    result = grammar.help("access_device", "transport ", ctx)
    values = {line.token: line.description for line in result.lines}
    assert values == {"ssh": "Use SSH transport", "telnet": "Use Telnet transport"}


def test_next_token_help_for_plain_selector_shows_dynamic_names():
    # running-config's selectors are plain (non-creatable)
    # identifiers, but bare `?` now dynamically lists the actual
    # selectable names (enumerate_when_empty) instead of a generic
    # <name> placeholder -- this test previously asserted the old,
    # now-fixed UX gap (a bare selector name never showed anything
    # concrete to select from).
    ctx = make_ctx(running_topology_names=("srv6_lab",))
    result = grammar.help("running", "topology ", ctx)
    assert [line.token for line in result.lines] == ["srv6_lab"]


def test_creatable_identifier_help_lists_existing_and_create_hint():
    ctx = make_ctx(topology_names=("sample_lab", "test"))
    result = grammar.help("global", "topology ", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("sample_lab", "Existing topology definition"),
        ("test", "Existing topology definition"),
        ("<name>", "Create or edit topology definition"),
    ]


def test_creatable_identifier_help_for_access_info_scenario_reference():
    ctx = make_ctx(access_info_names=("lab_a",), scenario_names=("failover_test",), reference_names=("sr_mpls",))
    access_info_result = grammar.help("global", "access-info ", ctx)
    assert [line.token for line in access_info_result.lines] == ["lab_a", "<name>"]

    scenario_result = grammar.help("global", "scenario ", ctx)
    assert [line.token for line in scenario_result.lines] == ["failover_test", "<name>"]

    reference_result = grammar.help("global", "reference ", ctx)
    assert [line.token for line in reference_result.lines] == ["sr_mpls", "<name>"]


def test_creatable_identifier_partial_help_only_shows_matches():
    ctx = make_ctx(topology_names=("sample_lab", "test"))
    result = grammar.help("global", "topology s", ctx)
    assert [line.token for line in result.lines] == ["sample_lab"]


def test_device_creatable_help_under_topology_and_access_info():
    ctx = make_ctx(topology_candidate_device_names=("R1",), access_info_candidate_device_names=("R1",))
    topology_device_help = grammar.help("topology", "device ", ctx)
    assert [(line.token, line.description) for line in topology_device_help.lines] == [
        ("R1", "Existing device"),
        ("<name>", "Create or edit device"),
    ]
    access_info_device_help = grammar.help("access_info", "device ", ctx)
    assert [(line.token, line.description) for line in access_info_device_help.lines] == [
        ("R1", "Existing device"),
        ("<name>", "Create or edit device"),
    ]


def test_password_help_shows_hint_never_value():
    ctx = make_ctx()
    result = grammar.help("access_device", "password ", ctx)
    assert [line.token for line in result.lines] == ["<password>"]
    result_partial = grammar.help("access_device", "password secret", ctx)
    assert [line.token for line in result_partial.lines] == ["<password>"]


def test_device_type_help_lists_fixed_enum_not_a_placeholder():
    ctx = make_ctx()
    for mode in ("device", "access_device"):
        result = grammar.help(mode, "type ", ctx)
        values = {line.token: line.description for line in result.lines}
        assert values == {
            "iosxr": "Cisco IOS XR",
            "iosxe": "Cisco IOS XE",
            "ios": "Cisco IOS",
            "nxos": "Cisco NX-OS",
            "host": "Generic host / endpoint",
        }
        assert result.show_cr is False
        assert "<value>" not in [line.token for line in result.lines]


def test_device_type_cr_marker_when_value_already_supplied():
    ctx = make_ctx()
    result = grammar.help("device", "type iosxr ", ctx)
    assert result.lines == []
    assert result.show_cr is True


def test_cr_marker_when_command_complete():
    ctx = make_ctx()
    result = grammar.help("global", "commit ", ctx)
    assert result.lines == []
    assert result.show_cr is True


def test_running_config_show_cr():
    ctx = make_ctx()
    result = grammar.help("global", "show running-config ", ctx)
    assert result.lines == []
    assert result.show_cr is True


# ---- show version ----


def test_show_version_is_exec_only():
    result = grammar.parse("exec", "show version")
    assert result.ok
    assert result.action == "exec.show_version"

    for mode in ("global", "running", "topology", "device", "access_info", "access_device", "scenario", "reference"):
        result = grammar.parse(mode, "show version")
        assert not result.ok, mode
        assert result.error.kind == "invalid"


def test_show_help_lists_version_only_in_exec():
    ctx = make_ctx()
    assert [line.token for line in grammar.help("exec", "show ", ctx).lines] == ["running-config", "version", "logging"]

    for mode in ("global", "running", "topology", "device", "access_info", "access_device", "scenario", "reference"):
        tokens = [line.token for line in grammar.help(mode, "show ", ctx).lines]
        assert "version" not in tokens, mode
        assert "logging" not in tokens, mode


def test_show_version_tab_completion_exec_only():
    ctx = make_ctx()
    assert "version" in grammar.complete("exec", "show ver", ctx).candidates

    for mode in ("global", "running", "topology", "device", "access_info", "access_device", "scenario", "reference"):
        assert grammar.complete(mode, "show ver", ctx).candidates == [], mode


def test_show_version_cr_marker():
    ctx = make_ctx()
    result = grammar.help("exec", "show version ", ctx)
    assert result.lines == []
    assert result.show_cr is True


# ---- help vs. ? distinction, and help topics ----


def test_bare_question_mark_still_shows_command_syntax_not_quick_start():
    # `?` (grammar.help with empty text) must remain the IOS XR-style
    # command-syntax listing -- Quick Start is a separate concern rendered
    # by cli/main.py only for the executed `help` command, never by `?`.
    ctx = make_ctx()
    result = grammar.help("exec", "", ctx)
    tokens = [line.token for line in result.lines]
    assert tokens == ["configure", "show", "delete", "terminal", "help", "exit", "quit"]


def test_bare_help_parses_as_its_own_complete_command():
    result = grammar.parse("exec", "help")
    assert result.ok
    assert result.action == "exec.help"
    assert result.args == {}


def test_help_topic_help_lists_topics_and_cr():
    ctx = make_ctx()
    result = grammar.help("exec", "help ", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("claude", "Show Claude Code integration help"),
        ("workflow", "Show the recommended Network Lab workflow"),
        ("editor", "Show external YAML editor usage"),
        ("cli", "Show CLI usage information"),
    ]
    assert result.show_cr is True


def test_help_topic_parses_for_each_topic():
    for topic in ("claude", "workflow", "editor", "cli"):
        result = grammar.parse("exec", f"help {topic}")
        assert result.ok, topic
        assert result.action == "exec.help_topic"
        assert result.args == {"topic": topic}


def test_help_topic_cr_marker():
    ctx = make_ctx()
    result = grammar.help("exec", "help claude ", ctx)
    assert result.lines == []
    assert result.show_cr is True


def test_help_topic_case_insensitive_and_unambiguous_abbreviation():
    assert grammar.parse("exec", "help CLAUDE").args == {"topic": "claude"}
    assert grammar.parse("exec", "help workflow").args == {"topic": "workflow"}
    assert grammar.parse("exec", "help w").args == {"topic": "workflow"}  # "w" is unique
    assert grammar.parse("exec", "help e").args == {"topic": "editor"}  # "e" is unique


def test_help_topic_ambiguous_abbreviation_rejected():
    result = grammar.parse("exec", "help c")
    assert not result.ok
    assert result.error.kind == "invalid"
    assert "Ambiguous" in result.error.detail
    assert "claude" in result.error.detail and "cli" in result.error.detail


def test_help_topic_unknown_value_rejected():
    result = grammar.parse("exec", "help bogus")
    assert not result.ok
    assert result.error.kind == "invalid"
    assert "Invalid help topic" in result.error.detail


def test_help_topic_tab_completion():
    ctx = make_ctx()
    assert set(grammar.complete("exec", "help ", ctx).candidates) == {"claude", "workflow", "editor", "cli"}
    assert grammar.complete("exec", "help w", ctx).candidates == ["workflow"]


def test_help_available_in_every_mode():
    for mode in ("global", "running", "topology", "device", "access_info", "access_device", "scenario", "reference"):
        result = grammar.parse(mode, "help")
        assert result.ok, mode
        assert result.action == f"{mode}.help"
        topic_result = grammar.parse(mode, "help cli")
        assert topic_result.ok, mode
        assert topic_result.action == f"{mode}.help_topic"


# ==========================================================================
# `no` command symmetry for access-info (object deletion + leaf clearing)
# ==========================================================================


# ---- access-info: no device / no jump-host (whole-object candidate deletion) ----


def test_no_device_parses():
    result = grammar.parse("access_info", "no device R4")
    assert result.ok
    assert result.action == "access_info.remove_device"
    assert result.args == {"name": "R4"}


def test_no_jump_host_parses():
    result = grammar.parse("access_info", "no jump-host jump1")
    assert result.ok
    assert result.action == "access_info.remove_jump_host"
    assert result.args == {"name": "jump1"}


def test_no_device_not_reachable_from_device_submode():
    # Scope containment: object deletion only exists at access-info root,
    # never inside the device/jump-host submode grammar.
    result = grammar.parse("access_device", "no device R1")
    assert not result.ok
    result2 = grammar.parse("access_jump_host", "no jump-host jump1")
    assert not result2.ok


def test_no_device_completion_uses_candidate_device_names():
    ctx = make_ctx(access_info_candidate_device_names=("R1", "R5"))
    result = grammar.complete("access_info", "no device ", ctx)
    assert set(result.candidates) == {"R1", "R5"}


def test_no_jump_host_completion_uses_candidate_jump_host_names():
    ctx = make_ctx(access_info_candidate_jump_host_names=("jump1", "jump_temp"))
    result = grammar.complete("access_info", "no jump-host ", ctx)
    assert set(result.candidates) == {"jump1", "jump_temp"}


def test_no_node_help_lists_device_and_jump_host():
    ctx = make_ctx()
    result = grammar.help("access_info", "no ", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("device", "Remove a device"),
        ("jump-host", "Remove a jump host"),
    ]


def test_access_info_root_help_lists_no_among_top_level_keywords():
    ctx = make_ctx()
    tokens = [line.token for line in grammar.help("access_info", "", ctx).lines]
    for expected in ("device", "jump-host", "no", "show", "clear", "commit", "root", "exit", "end", "help"):
        assert expected in tokens, expected


def test_no_device_bare_help_enumerates_candidate_device_names():
    ctx = make_ctx(access_info_candidate_device_names=("R1", "R5"))
    result = grammar.help("access_info", "no device ", ctx)
    assert sorted(line.token for line in result.lines) == ["R1", "R5"]


def test_no_jump_host_bare_help_enumerates_candidate_jump_host_names():
    ctx = make_ctx(access_info_candidate_jump_host_names=("jump1",))
    result = grammar.help("access_info", "no jump-host ", ctx)
    assert [line.token for line in result.lines] == ["jump1"]


def test_no_device_exact_match_inline_help_shows_cr_and_spaced_shows_cr_only():
    ctx = make_ctx(access_info_candidate_device_names=("R4",))
    inline = grammar.help("access_info", "no device R4", ctx)
    assert [line.token for line in inline.lines] == ["R4"]
    assert inline.show_cr is True

    spaced = grammar.help("access_info", "no device R4 ", ctx)
    assert spaced.lines == []
    assert spaced.show_cr is True


def test_no_device_partial_inline_help_has_no_cr():
    ctx = make_ctx(access_info_candidate_device_names=("R40",))
    result = grammar.help("access_info", "no device R4", ctx)
    assert [line.token for line in result.lines] == ["R40"]
    assert result.show_cr is False


def test_no_device_help_for_nonexistent_name_produces_nothing():
    ctx = make_ctx(access_info_candidate_device_names=("R1",))
    result = grammar.help("access_info", "no device R9", ctx)
    assert result.lines == []
    assert result.show_cr is False


def test_no_jump_host_exact_match_inline_help_shows_cr():
    ctx = make_ctx(access_info_candidate_jump_host_names=("jump1",))
    inline = grammar.help("access_info", "no jump-host jump1", ctx)
    assert [line.token for line in inline.lines] == ["jump1"]
    assert inline.show_cr is True


# ---- device / jump-host submode: full leaf `no` symmetry ----


def test_access_device_no_leaf_parses():
    for keyword, action in (
        ("type", "access_device.clear_type"),
        ("address", "access_device.clear_address"),
        ("transport", "access_device.clear_transport"),
        ("username", "access_device.clear_username"),
        ("password", "access_device.clear_password"),
        ("port", "access_device.clear_port"),
        ("jump-host", "access_device.clear_jump_host"),
    ):
        result = grammar.parse("access_device", f"no {keyword}")
        assert result.ok, keyword
        assert result.action == action


def test_access_jump_host_no_leaf_parses():
    for keyword, action in (
        ("type", "access_jump_host.clear_type"),
        ("address", "access_jump_host.clear_address"),
        ("transport", "access_jump_host.clear_transport"),
        ("username", "access_jump_host.clear_username"),
        ("password", "access_jump_host.clear_password"),
        ("port", "access_jump_host.clear_port"),
    ):
        result = grammar.parse("access_jump_host", f"no {keyword}")
        assert result.ok, keyword
        assert result.action == action


def test_access_device_no_help_lists_all_seven_leaves():
    ctx = make_ctx()
    result = grammar.help("access_device", "no ", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("type", "Clear the device type"),
        ("address", "Clear the device address"),
        ("transport", "Clear the device transport"),
        ("username", "Clear the device username"),
        ("password", "Clear the device password"),
        ("port", "Clear the device port"),
        ("jump-host", "Clear the device jump-host reference"),
    ]


def test_access_jump_host_no_help_lists_all_six_leaves():
    ctx = make_ctx()
    result = grammar.help("access_jump_host", "no ", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("type", "Clear the jump-host type"),
        ("address", "Clear the jump-host address"),
        ("transport", "Clear the jump-host transport"),
        ("username", "Clear the jump-host username"),
        ("password", "Clear the jump-host password"),
        ("port", "Clear the jump-host port"),
    ]


# ---- drift guard: configurable-leaf set must always equal no-exposed-leaf set ----

_RESERVED_SUBMODE_KEYWORDS = {"no", "show", "clear", "commit", "root", "end", "exit", "help"}


def test_access_device_no_leaf_set_matches_configurable_leaf_set():
    root = grammar.MODE_ROOTS["access_device"]
    configurable = set(root.literal_children) - _RESERVED_SUBMODE_KEYWORDS
    removable = set(root.literal_children["no"].literal_children)
    assert configurable == removable == {
        "type", "address", "transport", "port", "username", "password", "jump-host",
    }


def test_access_jump_host_no_leaf_set_matches_configurable_leaf_set():
    root = grammar.MODE_ROOTS["access_jump_host"]
    configurable = set(root.literal_children) - _RESERVED_SUBMODE_KEYWORDS
    removable = set(root.literal_children["no"].literal_children)
    assert configurable == removable == {
        "type", "address", "transport", "port", "username", "password",
    }
