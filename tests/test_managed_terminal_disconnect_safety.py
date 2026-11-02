# SPDX-License-Identifier: Apache-2.0
"""Disconnect safety for managed terminals (Option C).

Requirement under test: once the SSH/Telnet transport of a managed session
has ended -- or its state cannot be established -- text sent through
terminal_send() must never be executed by a local shell. Nothing here
touches a real device: the transport is a local dummy process, and the
only thing ever sent is a harmless "canary" command that creates one file
inside the test's own tmp_path. If the canary file exists afterwards, the
command was executed by a local shell.

The tests run against real (isolated) tmux sessions, never mocks of the
session mechanics, so they observe what tmux actually does."""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time
from pathlib import Path

import pytest

from network_lab_mcp import terminal

_REAL_AUTHENTICATE = terminal._authenticate_managed_session
DEVICE = "DS1"
_CFG = {"transport": "ssh", "address": "192.0.2.1"}


@pytest.fixture(autouse=True)
def _isolated_managed_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")
    monkeypatch.setattr(terminal, "_authenticate_managed_session", lambda *a, **k: None)
    # Make any local shell that could ever exist in a pane a plain /bin/sh
    # with no user rc files, so a failure of the safety property is
    # observable without depending on the developer's own shell setup.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ZDOTDIR", str(tmp_path))
    terminal._ensure_tmux_environment()
    terminal._run(["set-option", "-g", "default-shell", "/bin/sh"])
    yield
    for session in terminal.list_device_sessions():
        terminal.close_device_terminal(session["device"])


def _transport(monkeypatch, tmp_path, body: str) -> Path:
    """Install a dummy transport (a script file, never inline `bash -c`
    text, so its source is never echoed into the pane). The script writes
    `<tmp>/transport.done` from an EXIT trap (so any exit status works), so a test can tell
    that the transport has really finished."""
    done = tmp_path / "transport.done"
    script = tmp_path / "transport.sh"
    script.write_text(f"#!/usr/bin/env bash\ntrap ': > {shlex.quote(str(done))}' EXIT\n{body}\n")
    script.chmod(0o755)
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["bash", str(script)]))
    return done


def _wait_for(predicate, timeout=10.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {what}")


def _open_allowing_early_exit() -> None:
    """Open a session whose dummy transport may end at once; an open that
    reports "connection ended" is the expected outcome for those."""
    try:
        terminal.open_device_terminal(DEVICE, _CFG)
    except terminal.TerminalError as exc:
        assert "connection ended" in str(exc)


def _pane_text() -> str:
    return terminal.read_device(DEVICE, 50)["content"]


def _send_canary(tmp_path: Path) -> Path:
    canary = tmp_path / "canary"
    try:
        terminal.send_to_device(DEVICE, f"echo canary > {shlex.quote(str(canary))}", None, True)
    except terminal.TerminalError:
        pass  # a refusal is the desired outcome; only the canary file decides safety
    # Give any (wrongly) listening local shell ample time to execute it.
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline and not canary.exists():
        time.sleep(0.05)
    return canary


@pytest.mark.parametrize("exit_code", [0, 3], ids=["clean-exit", "nonzero-exit"])
def test_command_sent_after_transport_exit_never_reaches_a_local_shell(monkeypatch, tmp_path, exit_code):
    done = _transport(monkeypatch, tmp_path, f'echo CONNECTED\nread -r line\necho "dummy-closed"\n(exit {exit_code})')
    terminal.open_device_terminal(DEVICE, _CFG)
    _wait_for(lambda: "CONNECTED" in _pane_text(), what="transport banner")

    terminal.send_to_device(DEVICE, None, None, True)  # makes the dummy `read` return, so it exits
    _wait_for(done.exists, what="transport exit")
    time.sleep(0.5)

    assert not _send_canary(tmp_path).exists(), "command sent after the transport ended ran in a local shell"


def test_idle_timeout_style_exit_never_reaches_a_local_shell(monkeypatch, tmp_path):
    done = _transport(monkeypatch, tmp_path, "echo CONNECTED\nread -r -t 1 line\necho 'dummy: idle timeout'")
    terminal.open_device_terminal(DEVICE, _CFG)
    _wait_for(done.exists, what="transport idle exit")
    time.sleep(0.5)
    assert not _send_canary(tmp_path).exists()


def test_immediate_transport_failure_is_reported_and_leaves_no_local_shell(monkeypatch, tmp_path):
    done = _transport(monkeypatch, tmp_path, "echo 'dummy: connection refused' >&2\n(exit 255)")
    with pytest.raises(terminal.TerminalError):
        terminal.open_device_terminal(DEVICE, _CFG)  # must not look like a successful open
    _wait_for(done.exists, what="transport exit")
    assert not _send_canary(tmp_path).exists()
    sessions = {s["device"]: s["state"] for s in terminal.list_device_sessions()}
    assert sessions.get(DEVICE, "exited") == "exited"
    if DEVICE in sessions:
        assert "connection refused" in _pane_text()  # the final output is preserved for terminal_read()


def test_reopen_after_disconnect_is_not_reported_as_reuse(monkeypatch, tmp_path):
    done = _transport(monkeypatch, tmp_path, "echo CONNECTED\nread -r -t 1 line")
    first = terminal.open_device_terminal(DEVICE, _CFG)
    assert first["reused"] is False
    _wait_for(done.exists, what="transport exit")
    time.sleep(0.5)
    done.unlink()
    second = terminal.open_device_terminal(DEVICE, _CFG)
    assert second["reused"] is False


# ---------------------------------------------------------------------------
# Launch structure, gate failures, dead-pane behavior (raw tmux, no transport)
# ---------------------------------------------------------------------------


def _raw_wrapper_session(name_suffix: str, *argv: str) -> tuple[str, str]:
    """Start the fixed launch wrapper directly as a pane's main process,
    without the managed-session machinery, so each exit path of the wrapper
    itself can be observed. Returns (session_name, pane_id)."""
    session = f"{terminal.VALIDATION_PREFIX}gate-{name_suffix}"
    out = terminal._run(
        ["new-session", "-d", "-s", session, "-x", "220", "-y", "50", "-P", "-F", "#{pane_id}",
         "/bin/sh", "-c", terminal._LAUNCH_GATE_SCRIPT, "sh", *argv]
    )
    return session, out.stdout.strip()


def _pane_dead(pane_id: str) -> bool:
    res = terminal._run(["display-message", "-p", "-t", pane_id, "#{pane_dead}"], check=False)
    return res.stdout.strip() == "1"


def _dead_status(pane_id: str) -> tuple[str, str]:
    res = terminal._run(["display-message", "-p", "-t", pane_id, "#{pane_dead_status}|#{pane_dead_signal}"])
    status, _, signal = res.stdout.strip().partition("|")
    return status, signal


def _assert_dead_pane_is_inert(pane_id: str, tmp_path: Path):
    """send-keys and paste-buffer to a dead pane: whatever tmux returns, the
    canary command must not be executed by anything."""
    canary = tmp_path / "dead-canary"
    cmd = f"echo canary > {shlex.quote(str(canary))}"
    keys = terminal._run(["send-keys", "-t", pane_id, "--", cmd, "Enter"], check=False)
    load = subprocess.run(
        terminal._tmux_base() + ["load-buffer", "-b", "nlm-test-dead", "-"], input=cmd + "\n", text=True, capture_output=True
    )
    paste = terminal._run(["paste-buffer", "-r", "-d", "-b", "nlm-test-dead", "-t", pane_id], check=False)
    terminal._run(["delete-buffer", "-b", "nlm-test-dead"], check=False)
    time.sleep(1.0)
    assert not canary.exists()
    return keys.returncode, paste.returncode, load.returncode


def _cleanup_raw(session: str):
    terminal._run(["kill-session", "-t", session], check=False)


def test_gate_eof_leaves_no_shell(tmp_path):
    session, pane = _raw_wrapper_session("eof", "sleep", "30")
    try:
        terminal._run(["send-keys", "-t", pane, "C-d"])  # EOF on the gate's read
        _wait_for(lambda: _pane_dead(pane), what="gate EOF exit")
        assert _dead_status(pane)[0] == "1"
        _assert_dead_pane_is_inert(pane, tmp_path)
    finally:
        _cleanup_raw(session)


def test_gate_sighup_leaves_no_shell(tmp_path):
    session, pane = _raw_wrapper_session("hup", "sleep", "30")
    try:
        pid = terminal._run(["display-message", "-p", "-t", pane, "#{pane_pid}"]).stdout.strip()
        os.kill(int(pid), 1)
        _wait_for(lambda: _pane_dead(pane), what="gate SIGHUP exit")
        _assert_dead_pane_is_inert(pane, tmp_path)
    finally:
        _cleanup_raw(session)


def test_gate_without_a_command_exits_immediately(tmp_path):
    session, pane = _raw_wrapper_session("noargs")
    try:
        _wait_for(lambda: _pane_dead(pane), what="exit without command")
        assert _dead_status(pane)[0] == "2"
        _assert_dead_pane_is_inert(pane, tmp_path)
    finally:
        _cleanup_raw(session)


def test_exec_of_a_missing_transport_leaves_no_shell(tmp_path):
    session, pane = _raw_wrapper_session("missing", "/nonexistent/nlm-no-such-transport")
    try:
        terminal._run(["send-keys", "-t", pane, "--", "Enter"])  # release the gate
        _wait_for(lambda: _pane_dead(pane), what="exec failure exit")
        assert _dead_status(pane)[0] in ("126", "127")
        _assert_dead_pane_is_inert(pane, tmp_path)
    finally:
        _cleanup_raw(session)


def test_exited_transport_pane_has_no_process_and_input_never_runs(monkeypatch, tmp_path):
    done = _transport(monkeypatch, tmp_path, "echo CONNECTED\n(exit 7)")
    _open_allowing_early_exit()
    _wait_for(done.exists, what="transport exit")
    session = terminal.derive_production_session_name(DEVICE)
    pane = terminal._run(["list-panes", "-t", session, "-F", "#{pane_id}"]).stdout.strip()
    _wait_for(lambda: _pane_dead(pane), what="dead pane")
    assert _dead_status(pane) == ("7", "")
    _assert_dead_pane_is_inert(pane, tmp_path)


def test_launch_passes_arguments_verbatim_without_shell_evaluation(monkeypatch, tmp_path):
    evil = tmp_path / "evil"
    args = [
        "a b", f"x;touch {evil}", "$HOME", "`touch %s`" % evil, "it's", 'dq"q', "line1\nline2",
        "-J", "-p", "--", "*", "~", "$(touch %s)" % evil,
    ]
    out = tmp_path / "argv.out"
    script = tmp_path / "echoargs.sh"
    script.write_text(f'#!/usr/bin/env bash\nprintf \'%s\\0\' "$@" > {shlex.quote(str(out))}\n')
    script.chmod(0o755)
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["bash", str(script), *args]))
    try:
        terminal.open_device_terminal(DEVICE, _CFG)
    except terminal.TerminalError:
        pass  # the dummy exits at once; only the received argv matters here
    _wait_for(out.exists, what="argv capture")
    time.sleep(0.3)
    assert out.read_bytes().split(b"\0")[:-1] == [a.encode() for a in args]
    assert not evil.exists()


def test_session_has_identity_logging_and_remain_on_exit(monkeypatch, tmp_path):
    _transport(monkeypatch, tmp_path, "echo CONNECTED\nsleep 30")
    terminal.open_device_terminal(DEVICE, _CFG)
    session = terminal.derive_production_session_name(DEVICE)
    pane = terminal._managed_pane_target(session)
    assert pane.startswith("%")
    assert terminal._run(["display-message", "-p", "-t", pane, "#{pane_pipe}"]).stdout.strip() == "1"
    assert terminal._run(["show-options", "-w", "-v", "-t", pane, "remain-on-exit"]).stdout.strip() == "on"
    start = terminal._run(["display-message", "-p", "-t", pane, "#{pane_start_command}"]).stdout
    assert "read -r _nlm_gate" in start and "exec" in start


# ---------------------------------------------------------------------------
# terminal_send guard: exited / identity / not-ready, stable reasons
# ---------------------------------------------------------------------------


def test_send_to_exited_session_is_refused_with_a_stable_reason(monkeypatch, tmp_path):
    done = _transport(monkeypatch, tmp_path, "echo CONNECTED\n(exit 0)")
    _open_allowing_early_exit()
    _wait_for(done.exists, what="transport exit")
    _wait_for(lambda: dict((s["device"], s["state"]) for s in terminal.list_device_sessions()).get(DEVICE) == "exited", what="exited state")
    with pytest.raises(terminal.TerminalError, match="session exited"):
        terminal.send_to_device(DEVICE, "show version", ["Tab"], True)
    assert "CONNECTED" in terminal.read_device(DEVICE, 100)["content"]  # terminal_read still works


def test_pane_identity_mismatch_fails_closed(monkeypatch, tmp_path):
    _transport(monkeypatch, tmp_path, "echo CONNECTED\nsleep 30")
    terminal.open_device_terminal(DEVICE, _CFG)
    session = terminal.derive_production_session_name(DEVICE)
    pane = terminal._managed_pane_target(session)
    terminal._run(["set-option", "-w", "-t", pane, terminal._OPT_PANE_ID, "%99999"])
    with pytest.raises(terminal.TerminalError, match="identity mismatch"):
        terminal.send_to_device(DEVICE, "x", None, True)


def test_session_without_identity_marker_is_refused_and_not_reused(tmp_path):
    session = terminal.derive_production_session_name("LEG1")
    terminal._run(["new-session", "-d", "-s", session, "-x", "80", "-y", "24"])  # shaped like a pre-fix session
    with pytest.raises(terminal.TerminalError, match="identity could not be verified"):
        terminal.send_to_device("LEG1", "echo canary", None, True)
    with pytest.raises(terminal.TerminalError, match="older version"):
        terminal.open_device_terminal("LEG1", _CFG)
    assert terminal.close_device_terminal("LEG1")["closed"] is True


def test_send_before_the_start_gate_opens_is_refused(tmp_path):
    session = terminal.derive_production_session_name("GATEP")
    out = terminal._run(
        ["new-session", "-d", "-s", session, "-P", "-F", "#{pane_id}", "/bin/sh", "-c", terminal._LAUNCH_GATE_SCRIPT, "sh", "sleep", "30"]
    )
    pane = out.stdout.strip()
    terminal._run(["set-option", "-w", "-t", pane, terminal._OPT_PANE_ID, pane])
    terminal._run(["set-option", "-w", "-t", pane, terminal._OPT_GATE, terminal._GATE_PENDING])
    try:
        with pytest.raises(terminal.TerminalError, match="not ready"):
            terminal.send_to_device("GATEP", "echo canary", None, True)
        assert not _pane_dead(pane)  # nothing was typed into the gate: it is still waiting
    finally:
        terminal.close_device_terminal("GATEP")


def test_failure_while_configuring_the_session_removes_it(monkeypatch, tmp_path):
    _transport(monkeypatch, tmp_path, "sleep 30")
    monkeypatch.setattr(terminal, "_start_session_logging", lambda *a, **k: (_ for _ in ()).throw(terminal.TerminalError("boom")))
    with pytest.raises(terminal.TerminalError, match="boom"):
        terminal.open_device_terminal(DEVICE, _CFG)
    assert terminal.list_device_sessions() == []


# ---------------------------------------------------------------------------
# Reconnect, identity across sessions, buffers
# ---------------------------------------------------------------------------


def test_reopen_after_disconnect_creates_a_new_pane_and_keeps_the_old_log(monkeypatch, tmp_path):
    done = _transport(monkeypatch, tmp_path, "echo FIRST-OUTPUT\n(exit 0)")
    _open_allowing_early_exit()
    _wait_for(done.exists, what="first transport exit")
    session = terminal.derive_production_session_name(DEVICE)
    first_pane = terminal._run(["list-panes", "-t", session, "-F", "#{pane_id}"]).stdout.strip()
    _wait_for(lambda: _pane_dead(first_pane), what="first pane dead")
    done.unlink()
    _transport(monkeypatch, tmp_path, "echo SECOND-OUTPUT\nsleep 30")
    result = terminal.open_device_terminal(DEVICE, _CFG)
    assert result["reused"] is False
    second_pane = terminal._managed_pane_target(session)
    assert second_pane != first_pane
    logs = sorted((terminal.LOGS_ROOT / DEVICE).glob("*.log"))

    def log_text():
        return "".join(p.read_text(errors="replace") for p in sorted((terminal.LOGS_ROOT / DEVICE).glob("*.log")))

    _wait_for(lambda: "SECOND-OUTPUT" in log_text(), what="second log content")
    assert len(sorted((terminal.LOGS_ROOT / DEVICE).glob("*.log"))) == 2
    assert "FIRST-OUTPUT" in logs[0].read_text(errors="replace")


def test_failed_paste_leaves_no_tmux_buffer_behind(monkeypatch, tmp_path):
    done = _transport(monkeypatch, tmp_path, "echo CONNECTED\n(exit 0)")
    _open_allowing_early_exit()
    session = terminal.derive_production_session_name(DEVICE)
    pane = terminal._run(["list-panes", "-t", session, "-F", "#{pane_id}"]).stdout.strip()
    _wait_for(done.exists, what="transport exit")
    _wait_for(lambda: _pane_dead(pane), what="dead pane")
    # Simulate the TOCTOU case: the guard already passed against a live pane
    # that has since died, so load-buffer succeeds and paste-buffer fails.
    monkeypatch.setattr(terminal, "_send_target", lambda name: pane)
    with pytest.raises(terminal.TerminalError):
        terminal._send_literal_text(session, "FICTIONAL-SENTINEL-TEXT")
    buffers = terminal._run(["list-buffers", "-F", "#{buffer_name}"], check=False).stdout
    assert f"text-{session}" not in buffers


# ---------------------------------------------------------------------------
# Deterministic races (synchronization points, no sleeps to provoke them)
# ---------------------------------------------------------------------------


def _stoppable_transport(monkeypatch, tmp_path):
    stop = tmp_path / "stop"
    done = _transport(
        monkeypatch, tmp_path, f"echo CONNECTED\nwhile [ ! -e {shlex.quote(str(stop))} ]; do sleep 0.05; done\n(exit 0)"
    )
    return stop, done


def test_transport_ends_between_the_guard_check_and_the_paste(monkeypatch, tmp_path):
    stop, done = _stoppable_transport(monkeypatch, tmp_path)
    terminal.open_device_terminal(DEVICE, _CFG)
    session = terminal.derive_production_session_name(DEVICE)
    pane = terminal._managed_pane_target(session)
    original = terminal._managed_pane_target

    def guard_then_disconnect(name):
        target = original(name)  # T1: the guard sees a live, connected pane
        stop.write_text("")  # T2: the transport ends...
        _wait_for(lambda: _pane_dead(pane), what="pane death")  # ...before T3
        return target

    monkeypatch.setattr(terminal, "_managed_pane_target", guard_then_disconnect)
    canary = tmp_path / "race-canary"
    with pytest.raises(terminal.TerminalError):
        terminal.send_to_device(DEVICE, f"echo canary > {shlex.quote(str(canary))}", None, True)
    time.sleep(1.0)
    assert not canary.exists()


def test_transport_ends_between_text_and_enter(monkeypatch, tmp_path):
    stop, done = _stoppable_transport(monkeypatch, tmp_path)
    terminal.open_device_terminal(DEVICE, _CFG)
    session = terminal.derive_production_session_name(DEVICE)
    pane = terminal._managed_pane_target(session)
    real_enter = terminal._send_enter

    def disconnect_then_enter(name):
        stop.write_text("")
        _wait_for(lambda: _pane_dead(pane), what="pane death")
        return real_enter(name)

    monkeypatch.setattr(terminal, "_send_enter", disconnect_then_enter)
    canary = tmp_path / "race-canary2"
    with pytest.raises(terminal.TerminalError, match="session exited"):
        terminal.send_to_device(DEVICE, f"echo canary > {shlex.quote(str(canary))}", None, True)
    time.sleep(1.0)
    assert not canary.exists()


def test_close_then_send_is_refused_and_reaches_nothing(monkeypatch, tmp_path):
    _stoppable_transport(monkeypatch, tmp_path)
    terminal.open_device_terminal(DEVICE, _CFG)
    assert terminal.close_device_terminal(DEVICE)["closed"] is True
    with pytest.raises(terminal.TerminalError, match="does not exist"):
        terminal.send_to_device(DEVICE, "echo canary", None, True)


def test_send_waits_for_an_in_progress_open_and_other_devices_are_unaffected(monkeypatch, tmp_path):
    _stoppable_transport(monkeypatch, tmp_path)
    inside_auth = threading.Event()
    release_auth = threading.Event()

    def slow_auth(device_name, *a, **k):
        if device_name == DEVICE:
            inside_auth.set()
            assert release_auth.wait(10)

    monkeypatch.setattr(terminal, "_authenticate_managed_session", slow_auth)
    other = "DS2"
    terminal.open_device_terminal(other, _CFG)  # a different device is independent
    opener = threading.Thread(target=terminal.open_device_terminal, args=(DEVICE, _CFG))
    opener.start()
    assert inside_auth.wait(10)
    done = threading.Event()
    errors: list[BaseException] = []

    def sender():
        try:
            terminal.send_to_device(DEVICE, "echo hi", None, True)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion below
            errors.append(exc)
        done.set()

    sender_thread = threading.Thread(target=sender)
    sender_thread.start()
    assert not done.wait(0.5)  # the per-device lock holds it back while open is in progress
    terminal.send_to_device(other, "echo other", None, True)  # the other device is not blocked
    release_auth.set()
    opener.join(10)
    sender_thread.join(10)
    assert done.is_set() and not errors


# ---------------------------------------------------------------------------
# Logging, secrets
# ---------------------------------------------------------------------------


def test_immediate_failure_output_is_logged_exactly_once(monkeypatch, tmp_path):
    done = _transport(monkeypatch, tmp_path, "echo BANNER-FIRST\necho 'dummy: connection refused' >&2\n(exit 255)")
    with pytest.raises(terminal.TerminalError, match="connection ended before login completed"):
        terminal.open_device_terminal(DEVICE, _CFG)
    _wait_for(done.exists, what="transport exit")

    def log_text():
        logs = list((terminal.LOGS_ROOT / DEVICE).glob("*.log"))
        return logs[0].read_text(errors="replace") if logs else ""

    _wait_for(lambda: "connection refused" in log_text(), what="failure text in the log")
    text = log_text()
    assert text.count("BANNER-FIRST") == 1 and text.count("connection refused") == 1


def test_password_never_appears_in_argv_pane_start_command_buffers_or_log(monkeypatch, tmp_path):
    secret = "FICTIONAL-PW-4f7c2a"
    monkeypatch.setattr(terminal, "_authenticate_managed_session", _REAL_AUTHENTICATE)
    received = tmp_path / "received"
    script = tmp_path / "pwprompt.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "stty -echo\nprintf \"user@192.0.2.1's password: \"\n"
        f"IFS= read -rs pw; echo \"$pw\" > {shlex.quote(str(received))}\n"
        "echo; echo AUTHENTICATED; sleep 30\n"
    )
    script.chmod(0o755)
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["bash", str(script)]))
    config = {"transport": "ssh", "address": "192.0.2.1", "username": "user", "password": secret}
    terminal.open_device_terminal(DEVICE, config)
    _wait_for(received.exists, what="password delivery")
    assert received.read_text().strip() == secret  # delivered through stdin
    session = terminal.derive_production_session_name(DEVICE)
    observable = [
        subprocess.run(["ps", "-axo", "args"], capture_output=True, text=True).stdout,
        terminal._run(["display-message", "-p", "-t", session, "#{pane_start_command}|#{pane_current_command}"]).stdout,
        terminal._run(["list-buffers"], check=False).stdout,
        terminal.read_device(DEVICE, 50)["content"],
    ]
    time.sleep(0.5)
    observable += [p.read_text(errors="replace") for p in (terminal.LOGS_ROOT / DEVICE).glob("*.log")]
    assert all(secret not in text for text in observable)


# ---------------------------------------------------------------------------
# Jump host forms (local dummies only; no network)
#
# Supported: native `ssh -J` -- the jump host runs no interactive shell, so
# when the target connection ends the local ssh process (the pane's main
# process) ends too; that is exactly what every test above exercises.
#
# Not supported: a nested form in which an interactive shell *on the jump
# host* starts the second connection. These two tests use `/bin/sh -i` as a
# stand-in for such a jump-host shell and an inner dummy for the router
# connection, to pin down the guarantee's boundary.
# ---------------------------------------------------------------------------


def _nested_setup(monkeypatch, tmp_path, inner_prefix: str):
    inner = tmp_path / "inner.sh"
    inner.write_text("#!/usr/bin/env bash\necho INNER-CONNECTED\nread -r -t 1 x\necho INNER-CLOSED\n")
    inner.chmod(0o755)
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["/bin/sh", "-i"]))
    terminal.open_device_terminal(DEVICE, _CFG)
    terminal.send_to_device(DEVICE, f"{inner_prefix}bash {shlex.quote(str(inner))}", None, True)
    _wait_for(lambda: "INNER-CLOSED" in _pane_text(), what="inner connection end")
    time.sleep(0.5)


def test_nested_jump_shell_without_exec_is_outside_the_guarantee(monkeypatch, tmp_path):
    """Documents the unsupported form: the jump-host shell survives the inner
    connection, so the pane stays alive and a later command reaches that
    shell. (This is why the nested form is not supported; see SECURITY.md.)"""
    _nested_setup(monkeypatch, tmp_path, "")
    assert _send_canary(tmp_path).exists()


def test_nested_jump_shell_with_exec_leaves_nothing_behind(monkeypatch, tmp_path):
    """The mitigation a future nested mode would use: `exec` the inner
    connection so the jump-host shell is replaced by it."""
    _nested_setup(monkeypatch, tmp_path, "exec ")
    assert not _send_canary(tmp_path).exists()
