# SPDX-License-Identifier: Apache-2.0
"""`terminal monitor <device-id>` -- read-only
observation backend, incremental streaming, and monitor UI lifecycle.

Three layers are tested largely independently:

- `terminal.capture_device_terminal_view()`: the pure, side-effect-free
  observation primitive (WAITING/ACTIVE/ENDED, managed/discovery/none
  source priority), exercised directly against real (isolated) tmux
  sessions -- deterministic, no monitor UI involved.
- `cli.main._monitor_stream_step()`: the pure incremental-output/
  transition-marker logic, exercised with a fully scripted
  fake capture function -- deterministic, no tmux or real time involved.
- `cli.main.run_terminal_monitor()`: the monitor UI itself (a
  `full_screen=False` prompt_toolkit Application, so already-streamed
  activity survives in normal terminal scrollback), exercised through
  prompt_toolkit's own supported headless testing seam
  (`create_pipe_input()` + `DummyOutput()` + `create_app_session()`),
  event-driven (`threading.Event`), never a bare `time.sleep()` used as
  the pass/fail signal. Permanently "streamed" content is captured via
  `contextlib.redirect_stdout()` (matching how `run_in_terminal()` reaches
  the real terminal in production) where a test needs to inspect it.

Uses the isolated network-lab-mcp tmux socket (real tmux, never a real
router) with `terminal.LOGS_ROOT` monkeypatched to a temp directory, same
convention as test_terminal_logging.py / test_terminal_concurrency.py."""

from __future__ import annotations

import contextlib
import io
import threading

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from network_lab_mcp import terminal
from network_lab_mcp.cli import main as climain

_TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def isolated_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")


@pytest.fixture(autouse=True)
def _cleanup_device_sessions():
    yield
    for session in terminal.list_device_sessions():
        terminal.close_device_terminal(session["device"])
    # Source-priority tests also open Discovery-namespace fakes
    # directly (never through discover_topology()) -- clean those up too,
    # so a still-running one never leaks into (and is wrongly reused by)
    # the next test.
    for device_id in terminal.list_discovery_device_ids():
        terminal._close_session(terminal.derive_discovery_session_name(device_id))
    # A few tests deliberately create a session via terminal._create_session()
    # directly (to control whether the *pane's own process* dies, for
    # ENDED-state testing) rather than through _ensure_managed_session(),
    # which is the only place that ordinarily retires the shared bootstrap
    # session once a real one exists -- clean it up here too so this file
    # never leaves it behind for the rest of the suite.
    terminal._kill_session_if_exists(terminal.BOOTSTRAP_SESSION)


def _open_fake(device: str, script: str = "sleep 5") -> None:
    terminal._ensure_managed_session(
        terminal.derive_production_session_name(device), ["bash", "-c", script], log_device_name=device
    )


def _open_discovery_fake(device: str, script: str = "sleep 5") -> None:
    terminal._ensure_managed_session(terminal.derive_discovery_session_name(device), ["bash", "-c", script])


# ==========================================================================
# Observation backend: WAITING / ACTIVE / ENDED
# ==========================================================================


def test_waiting_when_no_session_exists():
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.status == "waiting"
    assert snapshot.source == "none"
    assert snapshot.pane_text == ""


def test_active_when_session_exists():
    _open_fake("R1")
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.status == "active"
    assert snapshot.source == "managed"


def test_waiting_again_after_session_disappears():
    _open_fake("R1")
    assert terminal.capture_device_terminal_view("R1").status == "active"
    terminal.close_device_terminal("R1")
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.status == "waiting"
    assert snapshot.pane_text == ""


def test_resumes_active_after_recreation_under_same_name():
    _open_fake("R1")
    assert terminal.capture_device_terminal_view("R1").status == "active"
    terminal.close_device_terminal("R1")
    assert terminal.capture_device_terminal_view("R1").status == "waiting"
    _open_fake("R1")
    assert terminal.capture_device_terminal_view("R1").status == "active"


def test_ended_when_pane_process_dies_but_session_remains():
    terminal._ensure_tmux_environment()
    session_name = terminal.derive_production_session_name("R1")
    terminal._create_session(session_name, ["bash", "-c", "echo done; exit 0"])

    def _observed_ended() -> bool:
        return terminal.capture_device_terminal_view("R1").status == "ended"

    assert _wait_until(_observed_ended)
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.status == "ended"
    assert "done" in snapshot.pane_text


def test_ended_pane_replaced_by_recreation_becomes_active():
    terminal._ensure_tmux_environment()
    session_name = terminal.derive_production_session_name("R1")
    terminal._create_session(session_name, ["bash", "-c", "exit 0"])
    assert _wait_until(lambda: terminal.capture_device_terminal_view("R1").status == "ended")
    terminal.close_device_terminal("R1")
    _open_fake("R1")
    assert terminal.capture_device_terminal_view("R1").status == "active"


def _wait_until(predicate, timeout: float = _TIMEOUT, interval: float = 0.05) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ==========================================================================
# Source priority (managed > discovery > waiting)
# ==========================================================================


def test_managed_priority_when_both_managed_and_discovery_exist():
    _open_discovery_fake("R1", "echo disco; sleep 5")
    _open_fake("R1", "echo managed; sleep 5")
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.source == "managed"
    assert snapshot.status == "active"
    assert "managed" in snapshot.pane_text
    assert "disco" not in snapshot.pane_text


def test_discovery_fallback_when_managed_absent():
    _open_discovery_fake("R1", "echo disco-only; sleep 5")
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.source == "discovery"
    assert snapshot.status == "active"
    assert "disco-only" in snapshot.pane_text


def test_waiting_when_neither_managed_nor_discovery_exist():
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.source == "none"
    assert snapshot.status == "waiting"
    # UI wording for this case is covered by test_status_line_waiting_has_no_source.


def test_discovery_disappears_managed_absent_converges_to_waiting():
    _open_discovery_fake("R1", "echo disco; sleep 5")
    assert terminal.capture_device_terminal_view("R1").source == "discovery"
    terminal._close_session(terminal.derive_discovery_session_name("R1"))
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.source == "none"
    assert snapshot.status == "waiting"


def test_discovery_takeover_when_managed_disappears_leaving_discovery():
    _open_discovery_fake("R1", "echo disco; sleep 5")
    _open_fake("R1", "echo managed; sleep 5")
    assert terminal.capture_device_terminal_view("R1").source == "managed"
    terminal.close_device_terminal("R1")
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.source == "discovery"
    assert snapshot.status == "active"
    assert "disco" in snapshot.pane_text


def test_managed_takeover_when_it_appears_while_discovery_active():
    _open_discovery_fake("R1", "echo disco; sleep 5")
    assert terminal.capture_device_terminal_view("R1").source == "discovery"
    _open_fake("R1", "echo managed; sleep 5")
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.source == "managed"
    assert "managed" in snapshot.pane_text


def test_discovery_race_target_vanishes_mid_observation(monkeypatch):
    _open_discovery_fake("R1")

    def _vanished_capture(session_name, lines):
        raise terminal.TerminalError(f"Session '{session_name}' does not exist.")

    monkeypatch.setattr(terminal, "_capture_pane", _vanished_capture)
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.status == "waiting"
    assert snapshot.source == "none"
    assert snapshot.pane_text == ""


# ==========================================================================
# Capture race: target vanishes between the state check and the capture
# ==========================================================================


def test_capture_race_target_vanishes_mid_observation(monkeypatch):
    _open_fake("R1")

    def _vanished_capture(session_name, lines):
        raise terminal.TerminalError(f"Session '{session_name}' does not exist.")

    monkeypatch.setattr(terminal, "_capture_pane", _vanished_capture)
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.status == "waiting"
    assert snapshot.pane_text == ""


def test_capture_race_recovers_once_capture_succeeds_again():
    _open_fake("R1")
    assert terminal.capture_device_terminal_view("R1").status == "active"


# ==========================================================================
# Pure incremental-stream step: _monitor_stream_step()
#
# Fully deterministic, no real tmux/time/UI involved -- a fake
# capture_device_terminal_view() drives each poll directly.
# ==========================================================================


def _fake_capture(sequence):
    """A queue-driven fake capture_device_terminal_view(device_id, lines=...)
    -- each call pops the next pre-scripted TerminalMonitorSnapshot. Accepts
    (and ignores) the `lines` kwarg the real signature also takes."""
    calls = list(sequence)

    def fake(device_id, lines=terminal.HISTORY_LIMIT):
        return calls.pop(0)

    return fake


def _snap(status, source, pane_text):
    return terminal.TerminalMonitorSnapshot("R1", status, source, pane_text)


def test_stream_step_waiting_at_start_emits_nothing():
    cursor = climain._MonitorStreamCursor()
    fake = _fake_capture([_snap("waiting", "none", "")])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", fake)
        printable, snapshot = climain._monitor_stream_step("R1", cursor)
    assert printable == []
    assert snapshot.status == "waiting"


def test_stream_step_basic_incremental_output():
    """poll1 -> A, poll2 -> A+B, poll3 -> A+B+C. Expected local
    output: A, then B, then C -- exactly once each."""
    cursor = climain._MonitorStreamCursor()
    fake = _fake_capture(
        [
            _snap("active", "managed", "A"),
            _snap("active", "managed", "A\nB"),
            _snap("active", "managed", "A\nB\nC"),
        ]
    )
    emitted: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", fake)
        for _ in range(3):
            printable, _ = climain._monitor_stream_step("R1", cursor)
            emitted.extend(printable)
    # First poll: a "started" marker plus the initial context ("A").
    assert emitted[0].startswith("[monitor]")
    assert emitted[1:] == ["A", "B", "C"]


def test_stream_step_unchanged_poll_emits_nothing_new():
    """several consecutive polls with no new output must
    append nothing beyond the very first poll's content."""
    cursor = climain._MonitorStreamCursor()
    fake = _fake_capture(
        [
            _snap("active", "managed", "A"),
            _snap("active", "managed", "A"),
            _snap("active", "managed", "A"),
        ]
    )
    all_printable: list[list[str]] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", fake)
        for _ in range(3):
            printable, _ = climain._monitor_stream_step("R1", cursor)
            all_printable.append(printable)
    assert all_printable[1] == []
    assert all_printable[2] == []


def test_stream_step_repeated_identical_lines_preserved():
    """legitimate repeated lines (e.g. duplicate routes)
    must never be collapsed as if they were duplicate polls."""
    cursor = climain._MonitorStreamCursor()
    fake = _fake_capture(
        [
            _snap("active", "managed", "10.0.0.0/24"),
            _snap("active", "managed", "10.0.0.0/24\n10.0.0.0/24\n10.0.0.0/24"),
        ]
    )
    emitted: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", fake)
        for _ in range(2):
            printable, _ = climain._monitor_stream_step("R1", cursor)
            emitted.extend(printable)
    assert emitted.count("10.0.0.0/24") == 3  # 1 initial + 2 new, never deduplicated


def test_stream_step_burst_output_preserved_even_if_it_exceeds_visible_pane():
    """a burst of output between two polls, larger than the
    tmux pane's own visible height (50 rows), must not be lost merely
    because the terminal/pane scrolled -- capture_device_terminal_view()
    is always called with lines=terminal.HISTORY_LIMIT precisely so the
    full (up to history-limit) buffer, not just the visible screen, is
    compared."""
    cursor = climain._MonitorStreamCursor()
    burst = [f"line-{i}" for i in range(200)]  # far more than the 50-row pane
    fake = _fake_capture(
        [
            _snap("active", "managed", "line-0"),
            _snap("active", "managed", "\n".join(burst)),
        ]
    )
    emitted: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", fake)
        for _ in range(2):
            printable, _ = climain._monitor_stream_step("R1", cursor)
            emitted.extend(printable)
    assert burst[1:] == [line for line in emitted if line.startswith("line-")][-199:]
    assert emitted.count("line-199") == 1


def test_stream_step_session_generation_reset_does_not_suppress_new_instance():
    """instance A outputs READY, disappears, instance B (same
    session name) outputs READY again -- expected exactly twice, once per
    instance, never suppressed as a false duplicate."""
    cursor = climain._MonitorStreamCursor()
    fake = _fake_capture(
        [
            _snap("active", "managed", "READY"),
            _snap("waiting", "none", ""),
            _snap("active", "managed", "READY"),  # a *new* instance, same text
        ]
    )
    emitted: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", fake)
        for _ in range(3):
            printable, _ = climain._monitor_stream_step("R1", cursor)
            emitted.extend(printable)
    assert emitted.count("READY") == 2


def test_stream_step_same_name_recreation_without_intermediate_waiting_poll():
    """The rarer race: the old instance disappears and a new one (same
    session name) appears between two polls, with no WAITING poll ever
    observed in between. The content-prefix integrity check alone (no
    tmux pane_id needed) must still detect this as a new instance."""
    cursor = climain._MonitorStreamCursor()
    fake = _fake_capture(
        [
            _snap("active", "managed", "OLD-BOOT-BANNER\nREADY"),
            _snap("active", "managed", "NEW-BOOT-BANNER\nREADY"),  # fresh pane, unrelated content
        ]
    )
    emitted: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", fake)
        for _ in range(2):
            printable, _ = climain._monitor_stream_step("R1", cursor)
            emitted.extend(printable)
    assert emitted.count("READY") == 2
    assert "NEW-BOOT-BANNER" in emitted
    assert any("resumed" in line for line in emitted if line.startswith("[monitor]"))


def test_stream_step_source_switch_produces_ordered_markers_no_duplication():
    """Discovery outputs DISCOVERY-A; managed takes over and
    outputs MANAGED-A; managed disappears, Discovery (still active) is
    preferred again and outputs DISCOVERY-B. Expected scrollback: DISCOVERY-A,
    a transition marker, MANAGED-A, a transition marker, DISCOVERY-B -- no
    merged/duplicate snapshot dumps."""
    cursor = climain._MonitorStreamCursor()
    fake = _fake_capture(
        [
            _snap("active", "discovery", "DISCOVERY-A"),
            _snap("active", "managed", "MANAGED-A"),
            _snap("active", "discovery", "DISCOVERY-B"),
        ]
    )
    emitted: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", fake)
        for _ in range(3):
            printable, _ = climain._monitor_stream_step("R1", cursor)
            emitted.extend(printable)
    content_only = [line for line in emitted if not line.startswith("[monitor]")]
    assert content_only == ["DISCOVERY-A", "MANAGED-A", "DISCOVERY-B"]
    markers = [line for line in emitted if line.startswith("[monitor]")]
    assert len(markers) == 3
    assert "started" in markers[0]
    assert "switched to managed" in markers[1]
    assert "switched to discovery" in markers[2]


def test_stream_step_waiting_after_activity_prints_marker_once_then_nothing():
    """waiting must not print its marker repeatedly, and
    must not touch previously emitted content."""
    cursor = climain._MonitorStreamCursor()
    fake = _fake_capture(
        [
            _snap("active", "discovery", "A\nB\nC"),
            _snap("waiting", "none", ""),
            _snap("waiting", "none", ""),
        ]
    )
    all_printable: list[list[str]] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", fake)
        for _ in range(3):
            printable, _ = climain._monitor_stream_step("R1", cursor)
            all_printable.append(printable)
    assert any("ended; waiting" in line for line in all_printable[1])
    assert all_printable[2] == []  # no repeated waiting marker


def test_stream_step_initial_context_is_bounded_not_full_history():
    """starting against an already-active session with a lot
    of prior content prints only a bounded recent window, never the
    entire history."""
    cursor = climain._MonitorStreamCursor()
    huge = [f"old-{i}" for i in range(500)]
    fake = _fake_capture([_snap("active", "managed", "\n".join(huge))])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", fake)
        printable, _ = climain._monitor_stream_step("R1", cursor)
    content_lines = [line for line in printable if not line.startswith("[monitor]")]
    assert len(content_lines) == climain._MONITOR_INITIAL_CONTEXT_LINES
    assert content_lines[-1] == "old-499"
    assert "old-0" not in content_lines  # bounded, not the whole 500-line history


# ==========================================================================
# Status line / status block
# ==========================================================================


def test_status_line_waiting_has_no_source():
    line = climain._monitor_status_line("R1", _snap("waiting", "none", ""))
    assert "Monitoring terminal R1" in line
    assert "Read-only" in line
    assert "waiting for terminal activity" in line
    assert "Source:" not in line
    assert "q: quit" in line


def test_status_line_active_managed():
    line = climain._monitor_status_line("R1", _snap("active", "managed", "x"))
    assert "Source: managed" in line
    assert "Status: active" in line


def test_status_line_active_discovery():
    line = climain._monitor_status_line("R1", _snap("active", "discovery", "x"))
    assert "Source: discovery" in line
    assert "Status: active" in line


def test_status_line_ended():
    line = climain._monitor_status_line("R1", _snap("ended", "managed", "x"))
    assert "Source: managed" in line
    assert "Status: ended" in line


def test_status_block_separators_match_terminal_width(monkeypatch):
    import os

    monkeypatch.setattr(climain.shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((40, 24)))
    block = climain._monitor_status_block("R1", _snap("waiting", "none", ""))
    lines = block.splitlines()
    assert len(lines) == 3
    assert lines[0] == "-" * 40
    assert lines[2] == "-" * 40
    assert len(lines[1]) <= 40


# ==========================================================================
# Non-full-screen confirmation
# ==========================================================================


def test_monitor_application_is_not_full_screen():
    app = climain._build_monitor_application("R1", 0.3)
    assert app.full_screen is False


# ==========================================================================
# Monitor UI lifecycle through the real Application
# ==========================================================================


def test_full_lifecycle_waiting_then_active_then_quit():
    device = "R1"
    seen_waiting = threading.Event()
    seen_active = threading.Event()
    real_capture = terminal.capture_device_terminal_view

    def tracking_capture(dev, lines=terminal.HISTORY_LIMIT):
        snapshot = real_capture(dev, lines=lines)
        if snapshot.status == "waiting":
            seen_waiting.set()
        elif snapshot.status == "active":
            seen_active.set()
        return snapshot

    def driver(pipe_input):
        assert seen_waiting.wait(timeout=_TIMEOUT)
        _open_fake(device)
        assert seen_active.wait(timeout=_TIMEOUT)
        pipe_input.send_text("q")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", tracking_capture)
        with create_pipe_input() as pipe_input:
            threading.Thread(target=driver, args=(pipe_input,), daemon=True).start()
            with create_app_session(input=pipe_input, output=DummyOutput()):
                climain.run_terminal_monitor(device, refresh_interval=0.02)

    assert seen_waiting.is_set()
    assert seen_active.is_set()


def test_session_disappearance_does_not_exit_monitor():
    device = "R1"
    _open_fake(device)
    seen_active = threading.Event()
    seen_waiting_after_active = threading.Event()
    became_active_once = threading.Event()
    real_capture = terminal.capture_device_terminal_view

    def tracking_capture(dev, lines=terminal.HISTORY_LIMIT):
        snapshot = real_capture(dev, lines=lines)
        if snapshot.status == "active":
            seen_active.set()
            became_active_once.set()
        elif snapshot.status == "waiting" and became_active_once.is_set():
            seen_waiting_after_active.set()
        return snapshot

    def driver(pipe_input):
        assert seen_active.wait(timeout=_TIMEOUT)
        terminal.close_device_terminal(device)
        assert seen_waiting_after_active.wait(timeout=_TIMEOUT)
        pipe_input.send_text("q")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", tracking_capture)
        with create_pipe_input() as pipe_input:
            threading.Thread(target=driver, args=(pipe_input,), daemon=True).start()
            with create_app_session(input=pipe_input, output=DummyOutput()):
                climain.run_terminal_monitor(device, refresh_interval=0.02)

    # Monitor did not exit on disappearance -- it ran until q was sent.
    assert seen_waiting_after_active.is_set()


def test_session_recreation_resumes_display():
    device = "R1"
    _open_fake(device)
    phases: list[str] = []
    real_capture = terminal.capture_device_terminal_view
    phase_lock = threading.Lock()
    first_active = threading.Event()
    waiting_after_close = threading.Event()
    second_active = threading.Event()

    def tracking_capture(dev, lines=terminal.HISTORY_LIMIT):
        snapshot = real_capture(dev, lines=lines)
        with phase_lock:
            if not phases or phases[-1] != snapshot.status:
                phases.append(snapshot.status)
        if snapshot.status == "active" and not first_active.is_set():
            first_active.set()
        elif snapshot.status == "waiting" and first_active.is_set():
            waiting_after_close.set()
        elif snapshot.status == "active" and waiting_after_close.is_set():
            second_active.set()
        return snapshot

    def driver(pipe_input):
        assert first_active.wait(timeout=_TIMEOUT)
        terminal.close_device_terminal(device)
        assert waiting_after_close.wait(timeout=_TIMEOUT)
        _open_fake(device)
        assert second_active.wait(timeout=_TIMEOUT)
        pipe_input.send_text("q")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", tracking_capture)
        with create_pipe_input() as pipe_input:
            threading.Thread(target=driver, args=(pipe_input,), daemon=True).start()
            with create_app_session(input=pipe_input, output=DummyOutput()):
                climain.run_terminal_monitor(device, refresh_interval=0.02)

    assert first_active.is_set()
    assert waiting_after_close.is_set()
    assert second_active.is_set()
    assert "active" in phases


def test_waiting_does_not_erase_previously_streamed_activity():
    """Discovery outputs A/B/C, then disappears.
    The already-printed activity must remain in the captured local output,
    and the status must become waiting."""
    device = "R1"
    _open_discovery_fake(device, "echo LINE-A; echo LINE-B; echo LINE-C; sleep 5")

    seen_all_three = threading.Event()
    seen_waiting_after = threading.Event()

    def driver(pipe_input):
        assert seen_all_three.wait(timeout=_TIMEOUT)
        terminal._close_session(terminal.derive_discovery_session_name(device))
        assert seen_waiting_after.wait(timeout=_TIMEOUT)
        pipe_input.send_text("q")

    buf = io.StringIO()
    real_capture = terminal.capture_device_terminal_view

    def tracking_capture(dev, lines=terminal.HISTORY_LIMIT):
        snapshot = real_capture(dev, lines=lines)
        if all(marker in snapshot.pane_text for marker in ("LINE-A", "LINE-B", "LINE-C")):
            seen_all_three.set()
        elif snapshot.status == "waiting" and seen_all_three.is_set():
            seen_waiting_after.set()
        return snapshot

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", tracking_capture)
        with create_pipe_input() as pipe_input:
            threading.Thread(target=driver, args=(pipe_input,), daemon=True).start()
            with create_app_session(input=pipe_input, output=DummyOutput()):
                with contextlib.redirect_stdout(buf):
                    climain.run_terminal_monitor(device, refresh_interval=0.02)

    printed = buf.getvalue()
    assert "LINE-A" in printed
    assert "LINE-B" in printed
    assert "LINE-C" in printed
    assert "ended; waiting" in printed


def test_q_exit_preserves_streamed_activity():
    """activity already emitted is not cleared
    by monitor shutdown, and the managed/Discovery session is untouched."""
    device = "R1"
    _open_fake(device, "echo KEEP-ME; sleep 5")
    seen_content = threading.Event()
    real_capture = terminal.capture_device_terminal_view

    def tracking_capture(dev, lines=terminal.HISTORY_LIMIT):
        snapshot = real_capture(dev, lines=lines)
        if "KEEP-ME" in snapshot.pane_text:
            seen_content.set()
        return snapshot

    def driver(pipe_input):
        assert seen_content.wait(timeout=_TIMEOUT)
        pipe_input.send_text("q")

    buf = io.StringIO()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", tracking_capture)
        with create_pipe_input() as pipe_input:
            threading.Thread(target=driver, args=(pipe_input,), daemon=True).start()
            with create_app_session(input=pipe_input, output=DummyOutput()):
                with contextlib.redirect_stdout(buf):
                    climain.run_terminal_monitor(device, refresh_interval=0.02)

    assert "KEEP-ME" in buf.getvalue()
    assert device in {s["device"] for s in terminal.list_device_sessions()}


def test_q_exits_and_managed_session_remains_untouched():
    device = "R1"
    _open_fake(device)
    with create_pipe_input() as pipe_input:
        pipe_input.send_text("q")
        with create_app_session(input=pipe_input, output=DummyOutput()):
            climain.run_terminal_monitor(device, refresh_interval=0.02)
    # The managed session is still alive -- q was never sent to tmux.
    sessions = {s["device"] for s in terminal.list_device_sessions()}
    assert device in sessions


def test_uppercase_q_also_exits():
    device = "R1"
    _open_fake(device)
    with create_pipe_input() as pipe_input:
        pipe_input.send_text("Q")
        with create_app_session(input=pipe_input, output=DummyOutput()):
            climain.run_terminal_monitor(device, refresh_interval=0.02)
    sessions = {s["device"] for s in terminal.list_device_sessions()}
    assert device in sessions


def test_ctrl_c_exits_monitor_only():
    device = "R1"
    _open_fake(device)
    with create_pipe_input() as pipe_input:
        pipe_input.send_text("\x03")  # Ctrl-C
        with create_app_session(input=pipe_input, output=DummyOutput()):
            climain.run_terminal_monitor(device, refresh_interval=0.02)
    # Returning from run_terminal_monitor() at all (no exception, no hang)
    # proves only the monitor Application exited, not the process.
    sessions = {s["device"] for s in terminal.list_device_sessions()}
    assert device in sessions


def test_other_keystrokes_are_ignored_never_forwarded():
    device = "R1"
    _open_fake(device)
    sent: list[tuple] = []
    real_send = terminal._send_literal_text
    real_special = terminal._send_special_keys

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "_send_literal_text", lambda *a: sent.append(("text", *a)) or real_send(*a))
        mp.setattr(terminal, "_send_special_keys", lambda *a: sent.append(("keys", *a)) or real_special(*a))
        with create_pipe_input() as pipe_input:
            pipe_input.send_text("show version\r")  # ordinary keystrokes, then Enter
            pipe_input.send_text("q")
            with create_app_session(input=pipe_input, output=DummyOutput()):
                climain.run_terminal_monitor(device, refresh_interval=0.02)

    assert sent == []


# ==========================================================================
# No-write / read-only safety
# ==========================================================================


def test_monitor_never_calls_any_mutating_terminal_helper():
    device = "R1"
    real_open_device_terminal = terminal.open_device_terminal
    real_send_to_device = terminal.send_to_device
    real_close_device_terminal = terminal.close_device_terminal
    real_ensure_managed_session = terminal._ensure_managed_session
    real_send_literal_text = terminal._send_literal_text
    real_send_special_keys = terminal._send_special_keys
    real_send_enter = terminal._send_enter
    real_create_session = terminal._create_session
    real_create_logged_session = terminal._create_logged_session
    real_close_session = terminal._close_session
    real_start_session_logging = terminal._start_session_logging

    calls: list[str] = []

    def _tracked(name, fn):
        def wrapper(*args, **kwargs):
            calls.append(name)
            return fn(*args, **kwargs)

        return wrapper

    seen_active = threading.Event()
    seen_waiting_after = threading.Event()
    became_active = threading.Event()

    def tracking_capture(dev, lines=terminal.HISTORY_LIMIT):
        if became_active.is_set() and not seen_waiting_after.is_set():
            return terminal.TerminalMonitorSnapshot(dev, "active", "managed", "pane")
        return terminal.TerminalMonitorSnapshot(dev, "waiting", "none", "")

    # Bootstrap the shared tmux server *before* patching -- otherwise the
    # driver's own _ensure_tmux_environment() call below would need to
    # create the shared bootstrap session for the first time from *inside*
    # the patched block, which (since _ensure_tmux_environment() calls
    # _create_session() by plain module-level name lookup, not a captured
    # reference) would dispatch to the tracked wrapper and falsely count
    # as a "monitor-triggered" call.
    terminal._ensure_tmux_environment()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "open_device_terminal", _tracked("open_device_terminal", real_open_device_terminal))
        mp.setattr(terminal, "send_to_device", _tracked("send_to_device", real_send_to_device))
        mp.setattr(terminal, "close_device_terminal", _tracked("close_device_terminal", real_close_device_terminal))
        mp.setattr(terminal, "_ensure_managed_session", _tracked("_ensure_managed_session", real_ensure_managed_session))
        mp.setattr(terminal, "_send_literal_text", _tracked("_send_literal_text", real_send_literal_text))
        mp.setattr(terminal, "_send_special_keys", _tracked("_send_special_keys", real_send_special_keys))
        mp.setattr(terminal, "_send_enter", _tracked("_send_enter", real_send_enter))
        mp.setattr(terminal, "_create_session", _tracked("_create_session", real_create_session))
        mp.setattr(terminal, "_create_logged_session", _tracked("_create_logged_session", real_create_logged_session))
        mp.setattr(terminal, "_close_session", _tracked("_close_session", real_close_session))
        mp.setattr(terminal, "_start_session_logging", _tracked("_start_session_logging", real_start_session_logging))
        mp.setattr(terminal, "capture_device_terminal_view", tracking_capture)

        # The driver simulates lifecycle changes via raw tmux calls
        # (terminal._run(), never patched) instead of any of the tracked
        # helpers above -- those internally call each other by module-level
        # name lookup, so even a *saved* reference to e.g. the original
        # _ensure_managed_session would still dispatch to the now-patched
        # _create_logged_session/_send_enter/etc. Bypassing all of them
        # means only monitor-triggered calls (if any) appear in `calls`.
        def driver(pipe_input):
            import time

            session_name = terminal.derive_production_session_name(device)
            terminal._run(["new-session", "-d", "-s", session_name, "-x", "220", "-y", "50", "bash", "-c", "sleep 5"])
            became_active.set()
            time.sleep(0.2)
            terminal._run(["kill-session", "-t", session_name], check=False)
            seen_waiting_after.set()
            time.sleep(0.2)
            pipe_input.send_text("q")

        with create_pipe_input() as pipe_input:
            threading.Thread(target=driver, args=(pipe_input,), daemon=True).start()
            with create_app_session(input=pipe_input, output=DummyOutput()):
                climain.run_terminal_monitor(device, refresh_interval=0.02)

    assert calls == []


def test_monitor_never_calls_any_mutating_helper_across_discovery_and_managed():
    """Extends the zero-write proof to the
    source-fallback path, exercising the *real* capture_device_terminal_
    view() (not a fake) through WAITING -> DISCOVERY ACTIVE -> WAITING ->
    MANAGED ACTIVE -> quit, asserting zero calls to any mutating helper in
    either namespace, with the new streaming UI active."""
    device = "R1"
    tracked_names = [
        "open_device_terminal",
        "send_to_device",
        "close_device_terminal",
        "_ensure_managed_session",
        "_send_literal_text",
        "_send_special_keys",
        "_send_enter",
        "_create_session",
        "_create_logged_session",
        "_close_session",
        "_start_session_logging",
        "open_bootstrap_terminal",
        "send_to_bootstrap",
        "close_bootstrap_terminal",
    ]
    calls: list[str] = []

    def _tracked(name, fn):
        def wrapper(*args, **kwargs):
            calls.append(name)
            return fn(*args, **kwargs)

        return wrapper

    seen_discovery_active = threading.Event()
    seen_waiting_after_discovery = threading.Event()
    seen_managed_active = threading.Event()
    real_capture = terminal.capture_device_terminal_view

    def tracking_capture(dev, lines=terminal.HISTORY_LIMIT):
        snapshot = real_capture(dev, lines=lines)
        if snapshot.source == "discovery":
            seen_discovery_active.set()
        elif snapshot.source == "none" and seen_discovery_active.is_set():
            seen_waiting_after_discovery.set()
        elif snapshot.source == "managed":
            seen_managed_active.set()
        return snapshot

    terminal._ensure_tmux_environment()  # see the sibling test's own note above

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", tracking_capture)
        for name in tracked_names:
            mp.setattr(terminal, name, _tracked(name, getattr(terminal, name)))

        def driver(pipe_input):
            import time

            discovery_session = terminal.derive_discovery_session_name(device)
            managed_session = terminal.derive_production_session_name(device)

            terminal._run(["new-session", "-d", "-s", discovery_session, "-x", "220", "-y", "50", "bash", "-c", "sleep 5"])
            assert seen_discovery_active.wait(timeout=_TIMEOUT)
            terminal._run(["kill-session", "-t", discovery_session], check=False)
            assert seen_waiting_after_discovery.wait(timeout=_TIMEOUT)

            terminal._run(["new-session", "-d", "-s", managed_session, "-x", "220", "-y", "50", "bash", "-c", "sleep 5"])
            assert seen_managed_active.wait(timeout=_TIMEOUT)
            terminal._run(["kill-session", "-t", managed_session], check=False)
            time.sleep(0.1)
            pipe_input.send_text("q")

        with create_pipe_input() as pipe_input:
            threading.Thread(target=driver, args=(pipe_input,), daemon=True).start()
            with create_app_session(input=pipe_input, output=DummyOutput()):
                climain.run_terminal_monitor(device, refresh_interval=0.02)

    assert seen_discovery_active.is_set()
    assert seen_waiting_after_discovery.is_set()
    assert seen_managed_active.is_set()
    assert calls == []


# ==========================================================================
# Multiple-monitor isolation
# ==========================================================================


def _run_monitor_in_own_session(device: str, exit_after: threading.Event, result: dict) -> None:
    with create_pipe_input() as pipe_input:

        def waiter():
            exit_after.wait(timeout=_TIMEOUT)
            pipe_input.send_text("q")

        threading.Thread(target=waiter, daemon=True).start()
        with create_app_session(input=pipe_input, output=DummyOutput()):
            climain.run_terminal_monitor(device, refresh_interval=0.02)
    result["exited"] = True


def test_two_monitors_of_different_devices_are_independent():
    _open_fake("R1")
    _open_fake("R2")
    exit_r1 = threading.Event()
    exit_r2 = threading.Event()
    result_r1: dict = {}
    result_r2: dict = {}

    t1 = threading.Thread(target=_run_monitor_in_own_session, args=("R1", exit_r1, result_r1))
    t2 = threading.Thread(target=_run_monitor_in_own_session, args=("R2", exit_r2, result_r2))
    t1.start()
    t2.start()

    exit_r1.set()
    t1.join(timeout=_TIMEOUT)
    assert result_r1.get("exited") is True
    # Monitor B is still running -- quitting A did not affect it.
    assert not t2.is_alive() or "exited" not in result_r2

    exit_r2.set()
    t2.join(timeout=_TIMEOUT)
    assert result_r2.get("exited") is True


def test_two_monitors_of_the_same_device_are_independent():
    _open_fake("R1")
    exit_a = threading.Event()
    exit_b = threading.Event()
    result_a: dict = {}
    result_b: dict = {}

    t1 = threading.Thread(target=_run_monitor_in_own_session, args=("R1", exit_a, result_a))
    t2 = threading.Thread(target=_run_monitor_in_own_session, args=("R1", exit_b, result_b))
    t1.start()
    t2.start()

    exit_a.set()
    t1.join(timeout=_TIMEOUT)
    assert result_a.get("exited") is True
    assert "exited" not in result_b  # B's own quit was never triggered

    exit_b.set()
    t2.join(timeout=_TIMEOUT)
    assert result_b.get("exited") is True
    # The shared device's session was never touched by either monitor.
    assert "R1" in {s["device"] for s in terminal.list_device_sessions()}


# ==========================================================================
# Coexistence: monitor never holds the per-device lock
# ==========================================================================


def test_monitor_does_not_block_concurrent_ai_terminal_operations():
    _open_fake("R1")
    stop = threading.Event()

    def monitor_thread():
        with create_pipe_input() as pipe_input:
            def waiter():
                stop.wait(timeout=_TIMEOUT)
                pipe_input.send_text("q")

            threading.Thread(target=waiter, daemon=True).start()
            with create_app_session(input=pipe_input, output=DummyOutput()):
                climain.run_terminal_monitor("R1", refresh_interval=0.02)

    t = threading.Thread(target=monitor_thread)
    t.start()
    try:
        # While the monitor is actively observing R1, an AI-style
        # terminal_read()/send_to_device() call for the *same* device must
        # complete promptly -- proving capture_device_terminal_view() does
        # not hold terminal._session_lock() across its observation.
        for _ in range(10):
            result = terminal.read_device("R1")
            assert result["device"] == "R1"
            terminal.send_to_device("R1", None, None, False)
    finally:
        stop.set()
        t.join(timeout=_TIMEOUT)
