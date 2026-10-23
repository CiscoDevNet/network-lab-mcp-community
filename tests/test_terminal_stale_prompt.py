# SPDX-License-Identifier: Apache-2.0
"""Discovery's command-completion wait must not be satisfied by a prompt
already sitting in the pane from a *previous* command (a stale-prompt
race) -- terminal._wait_for_pattern()'s `baseline_text` parameter, and
discovery._run_command()'s use of it.

Uses real tmux validation sessions (never a real router) with a script
that deliberately prints an "old prompt" immediately, then only after a
delay prints new output followed by a genuinely new prompt -- exactly the
shape of the race this guards against."""

from __future__ import annotations

import re
import time

import pytest

from network_lab_mcp import terminal

_PROMPT_RE = re.compile(r"RP/\S+/CPU\d+:(?P<hostname>[^#\s]+)#\s*$", re.MULTILINE)


@pytest.fixture(autouse=True)
def _cleanup_validation_sessions():
    yield
    for session_name in list(terminal.list_validation_sessions()):
        validation_id = session_name[len(terminal.VALIDATION_PREFIX) :]
        terminal.close_validation_session(validation_id)


def _open(validation_id: str, script: str) -> str:
    return terminal.open_validation_session(validation_id, ["bash", "-c", script])["session_name"]


def test_stale_prompt_alone_does_not_complete_the_wait():
    # Old prompt appears immediately; nothing new ever arrives.
    session_name = _open("stale-a", "echo 'RP/0/RP0/CPU0:R1#'; sleep 5")
    time.sleep(0.3)
    baseline = terminal._capture_pane(session_name, terminal.HISTORY_LIMIT)
    assert _PROMPT_RE.search(baseline)  # confirm the stale prompt really is already there

    with pytest.raises(terminal.TerminalError):
        terminal._wait_for_pattern(session_name, _PROMPT_RE, timeout=1.0, baseline_text=baseline)


def test_new_output_and_new_prompt_after_baseline_completes_the_wait():
    session_name = _open(
        "stale-b",
        "echo 'RP/0/RP0/CPU0:R1#'; sleep 0.6; echo 'command output line'; echo 'RP/0/RP0/CPU0:R1#'; sleep 5",
    )
    time.sleep(0.2)
    baseline = terminal._capture_pane(session_name, terminal.HISTORY_LIMIT)
    assert _PROMPT_RE.search(baseline)

    started = time.monotonic()
    result = terminal._wait_for_pattern(session_name, _PROMPT_RE, timeout=5.0, baseline_text=baseline)
    elapsed = time.monotonic() - started

    assert "command output line" in result
    assert elapsed >= 0.3  # did not complete instantly on the stale prompt alone


def test_without_baseline_a_prompt_already_present_matches_immediately():
    # Documents the default (no staleness gate) behavior for a genuinely
    # fresh session with nothing prior to be stale relative to.
    session_name = _open("stale-c", "echo 'RP/0/RP0/CPU0:R1#'; sleep 5")
    time.sleep(0.3)
    result = terminal._wait_for_pattern(session_name, _PROMPT_RE, timeout=2.0)
    assert _PROMPT_RE.search(result)


def test_normal_immediate_completion_when_output_already_differs_from_baseline():
    session_name = _open("stale-d", "echo 'RP/0/RP0/CPU0:R1#'; sleep 5")
    baseline = ""  # session just created, nothing captured yet
    time.sleep(0.3)
    result = terminal._wait_for_pattern(session_name, _PROMPT_RE, timeout=2.0, baseline_text=baseline)
    assert _PROMPT_RE.search(result)


def test_command_timeout_still_raises_terminal_error():
    session_name = _open("stale-e", "sleep 5")
    with pytest.raises(terminal.TerminalError):
        terminal._wait_for_pattern(session_name, _PROMPT_RE, timeout=0.5)


def test_multiple_sequential_commands_each_require_fresh_output():
    session_name = _open(
        "stale-f",
        "echo 'RP/0/RP0/CPU0:R1#'; "
        "sleep 0.4; echo 'first output'; echo 'RP/0/RP0/CPU0:R1#'; "
        "sleep 0.4; echo 'second output'; echo 'RP/0/RP0/CPU0:R1#'; "
        "sleep 5",
    )
    time.sleep(0.1)
    baseline1 = terminal._capture_pane(session_name, terminal.HISTORY_LIMIT)
    result1 = terminal._wait_for_pattern(session_name, _PROMPT_RE, timeout=3.0, baseline_text=baseline1)
    assert "first output" in result1
    assert "second output" not in result1

    baseline2 = result1
    result2 = terminal._wait_for_pattern(session_name, _PROMPT_RE, timeout=3.0, baseline_text=baseline2)
    assert "second output" in result2


def test_prompt_like_text_inside_command_output_does_not_falsely_complete():
    # A line that merely *contains* the prompt pattern earlier in the
    # output, followed by real trailing non-prompt text, must not match --
    # only the actual tail of the pane is checked.
    session_name = _open(
        "stale-g",
        "echo 'RP/0/RP0/CPU0:R1#'; sleep 0.4; "
        "echo 'RP/0/RP0/CPU0:R1# is a device prompt example'; echo 'still not done'; "
        "sleep 0.4; echo 'RP/0/RP0/CPU0:R1#'; sleep 5",
    )
    time.sleep(0.1)
    baseline = terminal._capture_pane(session_name, terminal.HISTORY_LIMIT)
    result = terminal._wait_for_pattern(session_name, _PROMPT_RE, timeout=3.0, baseline_text=baseline)
    assert "still not done" in result
