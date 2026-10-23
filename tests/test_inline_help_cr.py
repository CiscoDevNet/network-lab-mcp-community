# SPDX-License-Identifier: Apache-2.0
"""`<cr>` inline-help regression tests for the `creatable`-argument gap
fixed in grammar.help() (cli/grammar.py).

Root cause: `help()`'s "token?" exact-match branch only ever set
`show_cr` when the typed token exactly matched one of the argument's own
*known* candidates, explicitly excluding every `creatable` argument
(topology/access-info/scenario/reference/device/jump-host names) -- see
the removed comment this replaces. But `creatable` already means (by its
own existing definition, used by this exact same help() function a few
lines above to also show a "Create or edit ..." hint on bare `?`) that any
syntactically valid value is a legitimate stand-alone command here, not
just a known existing one -- confirmed directly against grammar.parse():
e.g. `parse("global", "scenario mu")` already succeeds today (returns
ok=True, action="global.scenario") even though "mu" is not an existing
scenario, because every creatable argument uses the default
`validate_freeform` validator (accepts any token) and its own node is
already terminal (has a `.command`). So the exact-match-only `<cr>` gate
was strictly narrower than what the grammar already allows to execute.

Fixed by granting `<cr>` unconditionally once `argument.creatable` is
true (short-circuiting past the existence check entirely -- see the `or`
in grammar.py's `help()`), while leaving every *non*-creatable dynamic
argument (show logging/delete logging device-or-file, terminal monitor
device-id, `no <kind> <name>`, running-config selectors, ...) completely
unchanged: those still require the exact token to already be a known
candidate, exactly as before this fix -- confirmed below for `terminal
monitor` with both a known and an unknown device.

This is a property of the `creatable` flag itself, not of any one
command: the same fix applies uniformly to scenario/reference/topology/
access-info/device/jump-host, with zero reference/scenario-specific
code."""

from __future__ import annotations

from network_lab_mcp.cli import grammar


def make_ctx(**kwargs) -> grammar.CliContext:
    return grammar.CliContext(**kwargs)


# ==========================================================================
# A/B. Exact existing / exact longest scenario name
# ==========================================================================


def test_exact_shorter_scenario_name_shows_cr_with_longer_candidate_too():
    ctx = make_ctx(scenario_names=("multi_flow_path_validation", "multi_flow_path_validation_failure_test"))
    result = grammar.help("global", "scenario multi_flow_path_validation", ctx)
    assert [line.token for line in result.lines] == [
        "multi_flow_path_validation",
        "multi_flow_path_validation_failure_test",
    ]
    assert all(line.description == "Existing scenario definition" for line in result.lines)
    assert result.show_cr is True


def test_exact_longest_scenario_name_shows_cr_alone():
    ctx = make_ctx(scenario_names=("multi_flow_path_validation", "multi_flow_path_validation_failure_test"))
    result = grammar.help("global", "scenario multi_flow_path_validation_failure_test", ctx)
    assert [line.token for line in result.lines] == ["multi_flow_path_validation_failure_test"]
    assert result.show_cr is True


# ==========================================================================
# C. Prefix-only scenario input -- repository reality check: `scenario
# <name>` accepts an arbitrary not-yet-existing name (validate_freeform,
# no custom validator; confirmed directly against grammar.parse() below),
# so a bare, non-matching prefix is *also* a complete, Enter-able command
# right now -- `<cr>` must appear, matching that actual grammar semantics
# rather than an "existing name only" assumption.
# ==========================================================================


def test_scenario_arbitrary_name_is_already_a_complete_command():
    # Confirms the grammar-SSOT premise the `<cr>` fix is built on, not
    # just its help() symptom: parse() itself accepts an unregistered name.
    result = grammar.parse("global", "scenario mu")
    assert result.ok
    assert result.action == "global.scenario"
    assert result.args == {"name": "mu"}


def test_scenario_prefix_only_input_shows_candidates_and_cr():
    ctx = make_ctx(scenario_names=("multi_flow_path_validation", "multi_flow_path_validation_failure_test"))
    result = grammar.help("global", "scenario mu", ctx)
    assert [line.token for line in result.lines] == [
        "multi_flow_path_validation",
        "multi_flow_path_validation_failure_test",
    ]
    assert result.show_cr is True


def test_scenario_prefix_matching_nothing_still_shows_cr():
    # Even a prefix that matches *no* existing scenario at all is still a
    # valid new-scenario-creation command -- `<cr>`, no candidates.
    ctx = make_ctx(scenario_names=("multi_flow_path_validation",))
    result = grammar.help("global", "scenario brand_new_name", ctx)
    assert result.lines == []
    assert result.show_cr is True


# ==========================================================================
# D. reference -- same generic `creatable` rule, independent command
# ==========================================================================


def test_reference_exact_name_shows_cr():
    ctx = make_ctx(reference_names=("pagent_pkts", "pagent_tgn", "sample"))
    result = grammar.help("global", "reference pagent_pkts", ctx)
    assert [line.token for line in result.lines] == ["pagent_pkts"]
    assert result.show_cr is True


def test_reference_prefix_only_shows_candidates_and_cr():
    ctx = make_ctx(reference_names=("pagent_pkts", "pagent_tgn", "sample"))
    result = grammar.help("global", "reference pa", ctx)
    assert [line.token for line in result.lines] == ["pagent_pkts", "pagent_tgn"]
    assert result.show_cr is True


# ==========================================================================
# Topology / access-info / device / jump-host -- the same `creatable` flag,
# confirming the fix is generic rather than scenario/reference-only.
# ==========================================================================


def test_topology_exact_name_shows_cr():
    ctx = make_ctx(topology_names=("sample_lab", "sample_lab_v2"))
    result = grammar.help("global", "topology sample_lab", ctx)
    assert [line.token for line in result.lines] == ["sample_lab", "sample_lab_v2"]
    assert result.show_cr is True


def test_access_info_exact_name_shows_cr():
    ctx = make_ctx(access_info_names=("site_1",))
    result = grammar.help("global", "access-info site_1", ctx)
    assert [line.token for line in result.lines] == ["site_1"]
    assert result.show_cr is True


def test_topology_device_creatable_name_shows_cr():
    ctx = make_ctx(topology_candidate_device_names=("R1",))
    result = grammar.help("topology", "device R1", ctx)
    assert [line.token for line in result.lines] == ["R1"]
    assert result.show_cr is True


def test_topology_device_new_name_also_shows_cr():
    ctx = make_ctx(topology_candidate_device_names=("R1",))
    result = grammar.help("topology", "device R99_NEW", ctx)
    assert result.lines == []
    assert result.show_cr is True


# ==========================================================================
# E. Non-creatable dynamic argument -- unchanged: existence still gates
# `<cr>`, both the "already worked" known-value case and the "correctly
# still absent" unknown-value case.
# ==========================================================================


def test_terminal_monitor_known_device_shows_cr_unchanged():
    ctx = make_ctx(monitor_terminal_device_ids=("R1", "R2"))
    result = grammar.help("exec", "terminal monitor R1", ctx)
    assert [line.token for line in result.lines] == ["R1"]
    assert result.show_cr is True


def test_terminal_monitor_unknown_device_still_has_no_cr():
    ctx = make_ctx(monitor_terminal_device_ids=("R1", "R2"))
    result = grammar.help("exec", "terminal monitor UNKNOWN_DEVICE", ctx)
    assert result.lines == []
    assert result.show_cr is False


# ==========================================================================
# F. Incomplete commands never show `<cr>`
# ==========================================================================


def test_bare_scenario_keyword_has_no_cr():
    ctx = make_ctx(scenario_names=("multi_flow_path_validation",))
    result = grammar.help("global", "scenario", ctx)
    assert result.show_cr is False


def test_bare_scenario_keyword_spaced_has_no_cr():
    ctx = make_ctx(scenario_names=("multi_flow_path_validation",))
    result = grammar.help("global", "scenario ", ctx)
    assert result.show_cr is False


def test_bare_terminal_keyword_has_no_cr():
    ctx = make_ctx()
    result = grammar.help("exec", "terminal", ctx)
    assert result.show_cr is False


# ==========================================================================
# G. No duplicate rendering -- `<cr>` is a single boolean flag, rendered by
# the real renderer (print_help_result in cli/main.py) exactly once.
# ==========================================================================


def test_cr_is_a_single_boolean_not_a_repeated_line():
    ctx = make_ctx(scenario_names=("multi_flow_path_validation", "multi_flow_path_validation_failure_test"))
    result = grammar.help("global", "scenario multi_flow_path_validation", ctx)
    assert result.show_cr is True
    assert not any(line.token == "<cr>" for line in result.lines)


def test_print_help_result_renders_cr_exactly_once(capsys):
    from network_lab_mcp.cli import main as climain

    ctx = make_ctx(scenario_names=("multi_flow_path_validation", "multi_flow_path_validation_failure_test"))
    result = grammar.help("global", "scenario multi_flow_path_validation", ctx)
    climain.print_help_result(result)
    out = capsys.readouterr().out
    assert out.count("<cr>") == 1
    assert "multi_flow_path_validation" in out
    assert "multi_flow_path_validation_failure_test" in out


# ==========================================================================
# H. Provider invocation -- the fix short-circuits past the second
# provider(ctx, "", committed) existence-check call for creatable
# arguments entirely (never needed once `creatable` alone decides
# `<cr>`), so the exact-match help path calls the provider at most once,
# not an added second time.
# ==========================================================================


def test_creatable_exact_match_help_calls_provider_exactly_once(monkeypatch):
    scenario_node = grammar.MODE_ROOTS["global"].literal_children["scenario"]
    argument = scenario_node.argument
    calls = []
    original = argument.provider

    def spy(ctx, prefix, committed=()):
        calls.append(prefix)
        return original(ctx, prefix, committed)

    monkeypatch.setattr(argument, "provider", spy)
    ctx = make_ctx(scenario_names=("multi_flow_path_validation", "multi_flow_path_validation_failure_test"))
    result = grammar.help("global", "scenario multi_flow_path_validation", ctx)
    assert result.show_cr is True
    assert calls == ["multi_flow_path_validation"]


def test_noncreatable_exact_match_help_provider_call_count_unchanged(monkeypatch):
    # Not part of this fix's scope -- pinned so a future change doesn't
    # accidentally add provider calls to the untouched non-creatable path.
    monitor_node = grammar.MODE_ROOTS["exec"].literal_children["terminal"].literal_children["monitor"]
    argument = monitor_node.argument
    calls = []
    original = argument.provider

    def spy(ctx, prefix, committed=()):
        calls.append(prefix)
        return original(ctx, prefix, committed)

    monkeypatch.setattr(argument, "provider", spy)
    ctx = make_ctx(monitor_terminal_device_ids=("R1", "R2"))
    result = grammar.help("exec", "terminal monitor R1", ctx)
    assert result.show_cr is True
    assert calls == ["R1", ""]


# ==========================================================================
# I. Tab-completion regression (unaffected by this inline-`?`-only change)
# is already covered by tests/test_tab_completion.py; nothing in this file
# duplicates it.
# ==========================================================================
