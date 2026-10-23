# SPDX-License-Identifier: Apache-2.0
"""Characterize `terminal._wait_for_pattern()`'s
current timeout semantics, with
deterministic tests (an injectable fake clock -- never a real 25-second
sleep) rather than inferring behavior from source inspection alone.

Investigation finding (see `_wait_for_pattern()`'s own body in
terminal.py): `deadline = time.monotonic() + timeout` is computed exactly
ONCE, and the poll loop condition is `while time.monotonic() < deadline`.
Nothing in the loop ever recomputes or extends `deadline` based on pane
content changing -- "activity" (the pane differing from `baseline_text`)
only ever *unlocks* whether a match is even considered (the stale-prompt
race guard), it never resets the timer. This is therefore a **FIXED TOTAL
DEADLINE**, not an inactivity timeout: continuously arriving, genuinely
changing terminal output does NOT postpone the timeout by even one
second -- only reaching the expected pattern before the deadline does.

Both Discovery's bootstrap login/command execution and managed
`terminal_open()`'s private authentication (`_authenticate_managed_
session()`) call this exact same shared primitive -- there is only one
command-wait implementation in the whole project, so this characterization
applies identically to every caller."""

from __future__ import annotations

import pytest

from network_lab_mcp import terminal


class _FakeClock:
    """A fully deterministic stand-in for the `time` module's monotonic
    clock and sleep, so `_wait_for_pattern()`'s real polling loop runs in
    zero wall-clock time while still exercising its exact real logic
    (only `time.monotonic()`/`time.sleep()` are faked; everything else in
    `_wait_for_pattern()` is untouched)."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(terminal.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(terminal.time, "sleep", clock.sleep)
    return clock


_NEVER_MATCHES_RE = terminal.re.compile(r"NEVER-APPEARS-PROMPT#\s*$")


# ---- Test A: silent terminal ----


def test_wait_for_pattern_silent_terminal_times_out(monkeypatch, fake_clock):
    """No new output ever appears -- times out at (approximately) the
    configured deadline, the uncontroversial baseline case."""
    monkeypatch.setattr(terminal, "_capture_pane", lambda session_name, lines: "same output, never changes")

    with pytest.raises(terminal.TerminalError, match="Timed out after 25s"):
        terminal._wait_for_pattern("sess", _NEVER_MATCHES_RE, timeout=25)

    # The loop stopped at (approximately) the deadline, not early and not
    # far beyond it (within one poll_interval of the configured timeout).
    assert 25.0 <= fake_clock.now < 25.0 + 0.3 + 1e-9


# ---- Test B: continuous output, prompt never (yet) matches ----


def test_wait_for_pattern_continuous_new_output_does_not_extend_the_deadline(monkeypatch, fake_clock):
    """FIXED TOTAL DEADLINE, proven directly: pane content changes on
    *every single poll* (this is exactly what "the terminal is actively
    producing new output" looks like) for the entire simulated 25-second
    window, and the expected pattern never matches -- if this were an
    inactivity timeout, continuous activity would keep postponing the
    timeout indefinitely (it would never fire while content keeps
    changing); instead it still times out at the configured deadline,
    proving activity never resets/extends the deadline at all."""
    calls = {"count": 0}

    def fake_capture_pane(session_name, lines):
        calls["count"] += 1
        # A different line every single call -- indistinguishable from a
        # command that is continuously, legitimately producing new
        # output, right up until (and past) the deadline.
        return f"line {calls['count']} of ongoing command output\n"

    monkeypatch.setattr(terminal, "_capture_pane", fake_capture_pane)

    with pytest.raises(terminal.TerminalError, match="Timed out after 25s"):
        terminal._wait_for_pattern("sess", _NEVER_MATCHES_RE, timeout=25)

    # Many distinct polls happened (proving output was indeed "active"
    # throughout, not silent) -- yet the deadline still fired on schedule.
    assert calls["count"] >= 25 / 0.3 - 2
    assert 25.0 <= fake_clock.now < 25.0 + 0.3 + 1e-9


def test_wait_for_pattern_a_match_available_only_past_the_deadline_is_never_seen(monkeypatch, fake_clock):
    """The strongest direct proof of "fixed total deadline": the expected
    prompt genuinely becomes available in the pane at simulated t=26s
    (one second *after* the 25s deadline) -- an inactivity-timeout design
    would still be polling at t=26s (nothing timed it out yet, since
    output changed continuously right up to t=26) and would succeed; the
    real implementation instead raises before ever reaching that content,
    because its deadline was fixed at t=25 from the very first call."""

    def fake_capture_pane(session_name, lines):
        if fake_clock.now >= 26.0:
            return "R1#\n"  # the prompt that WOULD satisfy the pattern
        return f"unmatched activity at t={fake_clock.now:.1f}\n"

    monkeypatch.setattr(terminal, "_capture_pane", fake_capture_pane)
    prompt_re = terminal.re.compile(r"R1#\s*$")

    with pytest.raises(terminal.TerminalError, match="Timed out after 25s"):
        terminal._wait_for_pattern("sess", prompt_re, timeout=25)

    # Confirms the loop never actually reached t=26 -- it stopped at the
    # fixed deadline instead of continuing to poll until a match appeared.
    assert fake_clock.now < 26.0


# ---- Test C: activity, then silence ----


def test_wait_for_pattern_activity_then_silence_times_out_at_the_same_fixed_deadline(monkeypatch, fake_clock):
    """Output changes for a while (activity), then goes completely still
    (silence) for the remainder of the window -- times out at the same
    configured deadline either way, confirming the deadline was never
    dependent on when activity happened to stop."""
    calls = {"count": 0}

    def fake_capture_pane(session_name, lines):
        calls["count"] += 1
        if fake_clock.now < 10.0:
            return f"active output line {calls['count']}\n"
        return "quiet, unchanged since t=10\n"  # silence for the rest of the window

    monkeypatch.setattr(terminal, "_capture_pane", fake_capture_pane)

    with pytest.raises(terminal.TerminalError, match="Timed out after 25s"):
        terminal._wait_for_pattern("sess", _NEVER_MATCHES_RE, timeout=25)

    assert 25.0 <= fake_clock.now < 25.0 + 0.3 + 1e-9


# ---- A match found comfortably before the deadline still succeeds normally ----


def test_wait_for_pattern_still_succeeds_well_before_the_deadline(monkeypatch, fake_clock):
    """Sanity check that the fake clock doesn't itself break the normal,
    already-well-tested success path (real prompt regex, real baseline-
    diff behavior) -- only the polling *timing* is faked."""

    def fake_capture_pane(session_name, lines):
        if fake_clock.now >= 3.0:
            return "R1#\n"
        return "booting...\n"

    monkeypatch.setattr(terminal, "_capture_pane", fake_capture_pane)
    prompt_re = terminal.re.compile(r"R1#\s*$")

    result = terminal._wait_for_pattern("sess", prompt_re, timeout=25)

    assert result == "R1#\n"
    assert fake_clock.now < 25.0
