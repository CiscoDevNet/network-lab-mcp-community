# SPDX-License-Identifier: Apache-2.0
"""Tab/`?` completion regression tests for the `tab` key binding's
multiple-candidate branch (`_make_key_bindings()` in cli/main.py).

Root cause investigated: `reference pa?` / `scenario m?` do NOT actually
duplicate-render on this HEAD (verified independently via a headless
prompt_toolkit session and a real PTY run through pyte) -- the real,
reproduced bug is that Tab with more than one candidate never extended the
buffer to the longest common prefix at all (e.g. "reference pa<TAB>" left
the buffer completely unchanged at "reference pa" instead of advancing to
"reference pagent_"), even though the ambiguous candidate list was always
printed correctly. These tests cover the two pure functions factored out
of the `tab` key binding to fix that (`_longest_common_prefix()`,
`_tab_multi_candidate_extension()`), plus the full grammar.complete() ->
extension-decision pipeline for the two commands named in the report, and
a handful of other dynamic-argument/literal commands to confirm the fix is
generic rather than reference/scenario-specific.

A follow-up fix (same task, second pass): the `tab` key binding applied the
LCP extension but then *always* fell through to also rendering the full
candidate list, even on the very first Tab press that still had unambiguous
ground to cover (Cisco IOS/IOS XE/IOS XR never does this -- the candidate
list only appears once Tab can no longer extend the buffer any further).
Fixed with a plain early `return` right after the buffer extension, so the
candidate list is only ever reached when `_tab_multi_candidate_extension()`
returns None. Deliberately stateless -- no "already listed once" tracking
was added, matching the project's preference for deriving behavior from the
current buffer and a fresh `grammar.complete()` call rather than session
state; a Tab pressed again with the buffer unchanged (or after a Backspace
and a different prefix) just re-runs the same decision and gets whatever
that buffer now implies. The `_TabFlowHarness`-based tests near the bottom
of this file exercise this multi-press flow for real, through the actual
prompt_toolkit Buffer/KeyBindings/Application machinery (the same
`create_pipe_input()` + `DummyOutput()` + `create_app_session()` seam
test_monitor_terminal.py uses for its own headless Application testing),
driven via `anyio.run()` exactly like test_mcp_concurrency.py's own async
tests (no pytest-asyncio/pytest-anyio plugin is installed or configured
here) -- everything above that stays a pure-function/grammar-level test
with no prompt_toolkit involved."""

from __future__ import annotations

import asyncio
import contextlib
import io

import anyio
import pytest
import yaml
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain


def make_ctx(**kwargs) -> grammar.CliContext:
    return grammar.CliContext(**kwargs)


# ==========================================================================
# `_longest_common_prefix` -- pure
# ==========================================================================


def test_lcp_partial_divergence():
    assert climain._longest_common_prefix(["pagent_pkts", "pagent_tgn"]) == "pagent_"


def test_lcp_one_candidate_is_prefix_of_another():
    assert (
        climain._longest_common_prefix(
            ["multi_flow_path_validation", "multi_flow_path_validation_failure_test"]
        )
        == "multi_flow_path_validation"
    )


def test_lcp_no_common_prefix():
    assert climain._longest_common_prefix(["R1", "SW1"]) == ""


def test_lcp_single_candidate_is_itself():
    assert climain._longest_common_prefix(["pagent_pkts"]) == "pagent_pkts"


def test_lcp_empty_list():
    assert climain._longest_common_prefix([]) == ""


def test_lcp_three_candidates_narrows_to_shared_prefix():
    assert climain._longest_common_prefix(["R1", "R2", "R9_DISCOVERY_ONLY"]) == "R"


# ==========================================================================
# `_tab_multi_candidate_extension` -- pure, combines LCP with replace_prefix
# ==========================================================================


def test_extension_advances_past_typed_prefix():
    result = grammar.CompletionResult(candidates=["pagent_pkts", "pagent_tgn"], replace_prefix="pa")
    assert climain._tab_multi_candidate_extension(result) == "pagent_"


def test_extension_none_when_already_at_common_prefix():
    # Already typed exactly the full common prefix -- nothing further to
    # extend to (must not be reported as an extension, or Tab would loop
    # inserting the same text it already has).
    result = grammar.CompletionResult(candidates=["pagent_pkts", "pagent_tgn"], replace_prefix="pagent_")
    assert climain._tab_multi_candidate_extension(result) is None


def test_extension_to_a_candidate_that_is_itself_a_prefix_of_another():
    result = grammar.CompletionResult(
        candidates=["multi_flow_path_validation", "multi_flow_path_validation_failure_test"],
        replace_prefix="m",
    )
    extension = climain._tab_multi_candidate_extension(result)
    assert extension == "multi_flow_path_validation"
    # Never a trailing space -- an ambiguous match (one candidate is still
    # a strict prefix of another) is never marked syntactically complete.
    assert not extension.endswith(" ")


def test_extension_none_when_candidates_diverge_immediately():
    result = grammar.CompletionResult(candidates=["R1", "SW1"], replace_prefix="")
    assert climain._tab_multi_candidate_extension(result) is None


# ==========================================================================
# Full pipeline: grammar.complete() -> extension decision, for the exact
# commands named in the bug report plus other dynamic-argument/literal
# commands, to confirm the fix is generic (never reference/scenario-only).
# ==========================================================================


def test_reference_pa_tab_extends_to_pagent_underscore():
    ctx = make_ctx(reference_names=("pagent_pkts", "pagent_tgn", "sample"))
    result = grammar.complete("global", "reference pa", ctx)
    assert set(result.candidates) == {"pagent_pkts", "pagent_tgn"}
    assert climain._tab_multi_candidate_extension(result) == "pagent_"


def test_reference_pagent_p_tab_is_unique_full_completion():
    ctx = make_ctx(reference_names=("pagent_pkts", "pagent_tgn", "sample"))
    result = grammar.complete("global", "reference pagent_p", ctx)
    assert result.candidates == ["pagent_pkts"]
    # Unique-candidate completion is the *other* branch of the tab key
    # binding (len(candidates) == 1), not this extension helper -- assert
    # the contract it relies on instead: the one candidate, complete with
    # no trailing space of its own.
    assert result.candidates[0] == "pagent_pkts"


def test_scenario_m_tab_extends_without_premature_trailing_space():
    ctx = make_ctx(scenario_names=("multi_flow_path_validation", "multi_flow_path_validation_failure_test"))
    result = grammar.complete("global", "scenario m", ctx)
    assert set(result.candidates) == {
        "multi_flow_path_validation",
        "multi_flow_path_validation_failure_test",
    }
    extension = climain._tab_multi_candidate_extension(result)
    assert extension == "multi_flow_path_validation"
    assert not extension.endswith(" ")


def test_topology_tab_extension_is_generic_not_reference_scenario_only():
    ctx = make_ctx(topology_names=("lab_abc", "lab_abd"))
    result = grammar.complete("global", "topology lab_a", ctx)
    assert set(result.candidates) == {"lab_abc", "lab_abd"}
    assert climain._tab_multi_candidate_extension(result) == "lab_ab"


def test_access_info_tab_extension_is_generic():
    ctx = make_ctx(access_info_names=("site_1", "site_2"))
    result = grammar.complete("global", "access-info site_", ctx)
    assert set(result.candidates) == {"site_1", "site_2"}
    assert climain._tab_multi_candidate_extension(result) is None  # already at common prefix


def test_terminal_monitor_tab_extension_is_generic():
    ctx = make_ctx(monitor_terminal_device_ids=("R1", "R2", "R9_DISCOVERY_ONLY"))
    result = grammar.complete("exec", "terminal monitor R", ctx)
    assert set(result.candidates) == {"R1", "R2", "R9_DISCOVERY_ONLY"}
    assert climain._tab_multi_candidate_extension(result) is None  # "R" already fully typed


def test_literal_single_candidate_unaffected_by_extension_helper():
    # A single literal-keyword match (e.g. "d" -> "delete" at EXEC root)
    # goes through the len(candidates) == 1 branch of the tab key binding,
    # never this helper -- confirm grammar.complete() itself still returns
    # exactly one candidate, unaffected by this change.
    ctx = make_ctx()
    result = grammar.complete("exec", "d", ctx)
    assert result.candidates == ["delete"]


# ==========================================================================
# Inline `?` regression: candidates render exactly once, descriptions kept.
# No duplicate-rendering bug was reproduced on this HEAD (see module
# docstring), but these pin the exact expected (non-duplicated) shape so
# a future regression here would be caught.
# ==========================================================================


def test_reference_prefix_help_has_no_duplicate_rendering():
    ctx = make_ctx(reference_names=("pagent_pkts", "pagent_tgn", "sample"))
    result = grammar.help("global", "reference pa", ctx)
    tokens = [line.token for line in result.lines]
    assert tokens == sorted(tokens)
    assert tokens.count("pagent_pkts") == 1
    assert tokens.count("pagent_tgn") == 1
    assert all(line.description == "Existing reference definition" for line in result.lines)


def test_scenario_prefix_help_has_no_duplicate_rendering():
    ctx = make_ctx(scenario_names=("multi_flow_path_validation", "multi_flow_path_validation_failure_test"))
    result = grammar.help("global", "scenario m", ctx)
    tokens = [line.token for line in result.lines]
    assert tokens == sorted(tokens)
    assert tokens.count("multi_flow_path_validation") == 1
    assert tokens.count("multi_flow_path_validation_failure_test") == 1
    assert all(line.description == "Existing scenario definition" for line in result.lines)


# ==========================================================================
# Candidate ordering is unchanged by this fix -- grammar.complete() already
# sorts dynamic-argument candidates; this fix never re-sorts or reorders.
# ==========================================================================


def test_candidate_ordering_unchanged_for_reference():
    ctx = make_ctx(reference_names=("pagent_tgn", "pagent_pkts", "sample"))
    result = grammar.complete("global", "reference pa", ctx)
    assert result.candidates == sorted(result.candidates) == ["pagent_pkts", "pagent_tgn"]


# ==========================================================================
# Real multi-press Tab flow, through the actual prompt_toolkit
# Buffer/KeyBindings/Application machinery (headless, via create_pipe_input()
# + DummyOutput() + create_app_session() -- same seam test_monitor_terminal.py
# uses for its own Application testing). Confirms the early-`return` fix:
# the candidate list is suppressed exactly as long as LCP extension still
# has ground to cover, and reappears (every time, statelessly) once it
# doesn't -- never gated by "already shown once" session state.
# ==========================================================================


def _write_yaml(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)


@pytest.fixture()
def tab_flow_lab_root(tmp_path):
    """A lab_root using the exact overlapping-prefix names from the bug
    report (pagent_pkts/pagent_tgn, multi_flow_path_validation/
    multi_flow_path_validation_failure_test) -- the shared `lab_root`
    fixture's own sample/failover_test names don't share a prefix, so they
    can't exercise the LCP-extension-vs-candidate-list distinction this
    file is about."""
    root = tmp_path / "lab"
    _write_yaml(
        root / "settings.yaml",
        {
            "active_access_info": "sample_lab",
            "active_topology": "sample_lab",
            "active_scenario": "multi_flow_path_validation",
            "active_references": ["sample"],
        },
    )
    _write_yaml(root / "principles.yaml", {"general_operating_principles": ["Inspect before changing."]})
    _write_yaml(
        root / "topologies" / "sample_lab.yaml",
        {"name": "sample_lab", "description": "x", "devices": {"R1": {"type": "iosxr"}}, "links": []},
    )
    _write_yaml(
        root / "access-info" / "sample_lab.yaml",
        {
            "name": "sample_lab",
            "jump_hosts": {},
            "devices": {
                "R1": {
                    "type": "iosxr",
                    "address": "192.0.2.11",
                    "transport": "ssh",
                    "port": 22,
                    "username": "u",
                    "password": "p",
                },
            },
        },
    )
    _write_yaml(root / "scenarios" / "multi_flow_path_validation.yaml", {"name": "multi_flow_path_validation"})
    _write_yaml(
        root / "scenarios" / "multi_flow_path_validation_failure_test.yaml",
        {"name": "multi_flow_path_validation_failure_test"},
    )
    _write_yaml(root / "references" / "sample.yaml", {"name": "sample"})
    _write_yaml(root / "references" / "pagent_pkts.yaml", {"name": "pagent_pkts"})
    _write_yaml(root / "references" / "pagent_tgn.yaml", {"name": "pagent_tgn"})
    return root


class _TabFlowHarness:
    """Drives a real PromptSession + the production `tab`/`?` key bindings
    headlessly, one keypress at a time, reading back the live buffer text
    and whatever got printed via run_in_terminal() since the last read."""

    def __init__(self, lab_root):
        self.session = cfgmod.CliSession(lab_root)
        self.session.enter_configure()
        self._buf = io.StringIO()
        self._redirect = contextlib.redirect_stdout(self._buf)
        self._pipe_input_cm = create_pipe_input()
        self.pipe_input = self._pipe_input_cm.__enter__()
        self._app_session_cm = create_app_session(input=self.pipe_input, output=DummyOutput())
        self._app_session_cm.__enter__()
        self._redirect.__enter__()
        # PromptSession must be built (it constructs its own Application
        # eagerly in __init__) only *after* the pipe-input/app-session
        # context above is active -- built any earlier, it binds to the
        # ambient real stdin/stdout instead of the fake ones, and every
        # key sent below would silently go nowhere.
        kb = climain._make_key_bindings(self.session)
        self.prompt_session: PromptSession = PromptSession(key_bindings=kb, complete_while_typing=False)
        self.task = asyncio.ensure_future(self.prompt_session.prompt_async())

    async def settle(self) -> None:
        await asyncio.sleep(0.05)

    async def type_text(self, text: str) -> None:
        self.pipe_input.send_text(text)
        await self.settle()

    async def press_tab(self) -> str:
        """Sends one Tab keypress, lets it fully process, and returns
        whatever was printed to stdout as a result (empty string if
        nothing was -- the unique/extension-only cases print nothing)."""
        self.pipe_input.send_text("\t")
        await self.settle()
        printed = self._buf.getvalue()
        self._buf.seek(0)
        self._buf.truncate(0)
        return printed

    def buffer_text(self) -> str:
        return self.prompt_session.default_buffer.text

    async def aclose(self) -> None:
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt, EOFError):
            await self.task
        self._redirect.__exit__(None, None, None)
        self._app_session_cm.__exit__(None, None, None)
        self._pipe_input_cm.__exit__(None, None, None)


def _run_tab_flow(lab_root, scenario) -> None:
    """Runs `scenario(harness)` (an async callable containing the actual
    steps/assertions) against a fresh `_TabFlowHarness`, via `anyio.run()`
    -- the same sync-test-function/`anyio.run()`-body convention
    test_mcp_concurrency.py already uses elsewhere in this suite, so this
    file adds no new async-testing plugin dependency (no pytest-asyncio/
    pytest-anyio is installed or configured here)."""

    async def body() -> None:
        harness = _TabFlowHarness(lab_root)
        try:
            await scenario(harness)
        finally:
            await harness.aclose()

    anyio.run(body)


def test_reference_pa_tab_extends_only_no_list_shown(tab_flow_lab_root):
    async def scenario(h):
        await h.type_text("reference pa")
        printed = await h.press_tab()
        assert h.buffer_text() == "reference pagent_"
        assert printed == ""

    _run_tab_flow(tab_flow_lab_root, scenario)


def test_reference_pagent__tab_shows_list_once_lcp_exhausted(tab_flow_lab_root):
    async def scenario(h):
        await h.type_text("reference pagent_")
        printed = await h.press_tab()
        assert h.buffer_text() == "reference pagent_"
        assert "pagent_pkts" in printed
        assert "pagent_tgn" in printed

    _run_tab_flow(tab_flow_lab_root, scenario)


def test_reference_pagent__tab_pressed_again_shows_list_again(tab_flow_lab_root):
    async def scenario(h):
        await h.type_text("reference pagent_")
        first = await h.press_tab()
        second = await h.press_tab()
        assert h.buffer_text() == "reference pagent_"
        assert "pagent_pkts" in first and "pagent_tgn" in first
        assert "pagent_pkts" in second and "pagent_tgn" in second

    _run_tab_flow(tab_flow_lab_root, scenario)


def test_reference_pagent_p_tab_completes_unique_candidate(tab_flow_lab_root):
    async def scenario(h):
        await h.type_text("reference pagent_p")
        printed = await h.press_tab()
        assert h.buffer_text() == "reference pagent_pkts"
        assert printed == ""

    _run_tab_flow(tab_flow_lab_root, scenario)


def test_scenario_mu_tab_extends_only_no_list_no_trailing_space(tab_flow_lab_root):
    async def scenario(h):
        await h.type_text("scenario mu")
        printed = await h.press_tab()
        assert h.buffer_text() == "scenario multi_flow_path_validation"
        assert not h.buffer_text().endswith(" ")
        assert printed == ""

    _run_tab_flow(tab_flow_lab_root, scenario)


def test_scenario_full_lcp_tab_shows_list(tab_flow_lab_root):
    async def scenario(h):
        await h.type_text("scenario multi_flow_path_validation")
        printed = await h.press_tab()
        assert h.buffer_text() == "scenario multi_flow_path_validation"
        assert "multi_flow_path_validation" in printed
        assert "multi_flow_path_validation_failure_test" in printed

    _run_tab_flow(tab_flow_lab_root, scenario)


def test_scenario_full_lcp_tab_pressed_again_shows_list_again(tab_flow_lab_root):
    async def scenario(h):
        await h.type_text("scenario multi_flow_path_validation")
        first = await h.press_tab()
        second = await h.press_tab()
        assert "multi_flow_path_validation_failure_test" in first
        assert "multi_flow_path_validation_failure_test" in second

    _run_tab_flow(tab_flow_lab_root, scenario)


def test_backspace_then_retab_uses_current_buffer_statelessly(tab_flow_lab_root):
    async def scenario(h):
        await h.type_text("scenario mu")
        await h.press_tab()
        assert h.buffer_text() == "scenario multi_flow_path_validation"
        # Backspace back to "scenario multi_flow_path_val" -- 9 chars of
        # "idation" removed -- then Tab again: must re-derive from the
        # buffer as it now stands, not from any memory of the previous Tab
        # press.
        for _ in range(len("idation")):
            await h.type_text("\x7f")
        assert h.buffer_text() == "scenario multi_flow_path_val"
        printed = await h.press_tab()
        assert h.buffer_text() == "scenario multi_flow_path_validation"
        assert printed == ""

    _run_tab_flow(tab_flow_lab_root, scenario)


def test_scenario_mu_question_mark_shows_help_only_no_candidate_list(tab_flow_lab_root):
    async def scenario(h):
        await h.type_text("scenario mu")
        h.pipe_input.send_text("?")
        await h.settle()
        printed = h._buf.getvalue()
        assert "Existing scenario definition" in printed
        lines = printed.splitlines()
        # Exactly one help line per candidate, each with its description --
        # "multi_flow_path_validation" is also a substring of the other
        # candidate's name, so match whole lines rather than counting the
        # substring across the whole block.
        assert sum(1 for line in lines if line.strip().startswith("multi_flow_path_validation ")) == 1
        assert sum(1 for line in lines if line.strip().startswith("multi_flow_path_validation_failure_test ")) == 1
        # The description-less two-line format the `tab` key binding's
        # list rendering uses ("  <candidate>\n" with no trailing
        # description) must never also appear here.
        assert "  multi_flow_path_validation\n" not in printed

    _run_tab_flow(tab_flow_lab_root, scenario)


def test_reference_pa_question_mark_shows_help_only(tab_flow_lab_root):
    async def scenario(h):
        await h.type_text("reference pa")
        h.pipe_input.send_text("?")
        await h.settle()
        printed = h._buf.getvalue()
        assert "Existing reference definition" in printed
        assert printed.count("pagent_pkts") == 1
        assert printed.count("pagent_tgn") == 1

    _run_tab_flow(tab_flow_lab_root, scenario)
