# SPDX-License-Identifier: Apache-2.0
"""Private SSH password authentication for managed terminal
sessions (`terminal_open()`).

Closes the gap identified in the first real end-to-end Claude Code test:
`terminal_open()` previously never automated a password prompt at all (only
Discovery's separate bootstrap login did), so Claude had to ask a human for
the router password. Now `terminal_open()` completes authentication itself
using the active access-info definition's own private password, once the
prompt can be safely confirmed to belong to the target device -- reusing
`terminal.resolve_target_password_prompt()`, the exact same target-vs-
jump-host attribution Discovery already relies on (see
tests/test_proxyjump.py for the ProxyJump-specific regression tests, and
its own pre-existing `test_login_password_*` tests for the shared function
exercised directly through discovery._resolve_login_password()).

Fake SSH replacements are small script *files* (never `bash -c "<inline
text>"`) invoked as `["bash", "<path>"]`: a shell only ever echoes the
*invocation* line it was typed, never a script file's own contents, so
this is what lets the script bodies below freely use words like
"password" or "Permission denied" without those literal words ever
appearing in the pane before the script actually runs and prints them --
avoiding a false-positive early match against the *echoed command source*
itself (which an inline `bash -c "...password..."` would produce, since
the whole command line is typed into the shell and echoed back before
executing). This also mirrors what a real `ssh user@host` invocation
looks like: a short, unremarkable command line. Script bodies use `stty
-echo; ...; read x; stty echo` to mimic real OpenSSH's own password-entry
behavior (the terminal never echoes what is typed at a password prompt)
-- this is what makes "the secret is absent from the pane/logs/errors" a
meaningful assertion, not an artifact of a fake script that merely never
echoes anything.

Uses the isolated network-lab-mcp tmux socket (real tmux, never a real
router) with `terminal.LOGS_ROOT` monkeypatched to a temp directory."""

from __future__ import annotations

import threading
import time

import anyio
import pytest

from network_lab_mcp import terminal

_SENTINEL_PASSWORD = "SENTINEL_PRIVATE_PASSWORD_XYZ"

_DIRECT_CONFIG = {
    "transport": "ssh",
    "address": "192.0.2.20",
    "username": "r1-user",
    "password": _SENTINEL_PASSWORD,
}

_NO_PASSWORD_CONFIG = {
    "transport": "ssh",
    "address": "192.0.2.20",
    "username": "r1-user",
}


@pytest.fixture(autouse=True)
def isolated_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")


@pytest.fixture(autouse=True)
def _short_timeouts(monkeypatch):
    # Bounded, but short, so tests that must genuinely exhaust a timeout
    # (no prompt/failure ever appears) stay fast. Production keeps its own
    # unchanged values -- see terminal.py's own constants.
    monkeypatch.setattr(terminal, "_MANAGED_LOGIN_TIMEOUT_SECONDS", 6)
    monkeypatch.setattr(terminal, "_MANAGED_AUTH_SETTLE_TIMEOUT_SECONDS", 6)


@pytest.fixture(autouse=True)
def _cleanup_device_sessions():
    yield
    for session in terminal.list_device_sessions():
        terminal.close_device_terminal(session["device"])


def _write_script(tmp_path, name: str, body: str) -> list[str]:
    """Write `body` to its own script file and return the argv to run it
    -- see the module docstring for why this, and not `bash -c "<body>"`,
    is what makes these fakes realistic."""
    path = tmp_path / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)
    return ["bash", str(path)]


def _script_prompt_and_succeed(tmp_path, user_host: str = "r1-user@192.0.2.20", name: str = "succeed.sh") -> list[str]:
    body = (
        f'stty -echo\n'
        f'printf "%s\'s password: " "{user_host}"\n'
        f"read x\n"
        f"stty echo\n"
        f"echo\n"
        f"echo AUTH-OK\n"
        f"sleep 5\n"
    )
    return _write_script(tmp_path, name, body)


def _script_prompt_twice(tmp_path, user_host: str = "r1-user@192.0.2.20") -> list[str]:
    body = (
        f'stty -echo\n'
        f'printf "%s\'s password: " "{user_host}"\n'
        f"read x\n"
        f'printf "\\n%s\'s password: " "{user_host}"\n'
        f"sleep 5\n"
    )
    return _write_script(tmp_path, "prompt_twice.sh", body)


def _script_prompt_then_failure(tmp_path, user_host: str = "r1-user@192.0.2.20") -> list[str]:
    body = (
        f'stty -echo\n'
        f'printf "%s\'s password: " "{user_host}"\n'
        f"read x\n"
        f"stty echo\n"
        f"echo\n"
        f'echo "Permission denied (password)."\n'
        f"sleep 5\n"
    )
    return _write_script(tmp_path, "prompt_then_failure.sh", body)


def _script_never_prompts(tmp_path) -> list[str]:
    return _write_script(tmp_path, "never_prompts.sh", "echo already-connected\nsleep 5\n")


def _script_prompt_then_hangs(tmp_path, user_host: str = "r1-user@192.0.2.20") -> list[str]:
    body = f'stty -echo\nprintf "%s\'s password: " "{user_host}"\nread x\nsleep 30\n'
    return _write_script(tmp_path, "prompt_then_hangs.sh", body)


def _script_harmless(tmp_path, text: str = "hello", name: str = "harmless.sh") -> list[str]:
    return _write_script(tmp_path, name, f"echo {text}\nsleep 5\n")


def _monkeypatch_transport(monkeypatch, command: list[str]) -> None:
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", command))


def _track_sends(monkeypatch) -> list[str]:
    """Track every _send_literal_text()/_send_secret_text() call (in the
    order they happen) while still forwarding to the real implementation --
    a fake replacement that merely records and discards would also
    silently swallow _create_logged_session()'s own use of
    _send_literal_text() to type the fake ssh command itself into the pane,
    so nothing would ever actually run there. The password itself is sent
    via _send_secret_text() (never _send_literal_text(), so it
    never enters any subprocess's own argv), so both are tracked into the
    same list to keep existing count/ordering assertions meaningful."""
    sent: list[str] = []
    real_send = terminal._send_literal_text
    real_secret_send = terminal._send_secret_text

    def tracked(session_name, text):
        sent.append(text)
        return real_send(session_name, text)

    def tracked_secret(session_name, secret):
        sent.append(secret)
        return real_secret_send(session_name, secret)

    monkeypatch.setattr(terminal, "_send_literal_text", tracked)
    monkeypatch.setattr(terminal, "_send_secret_text", tracked_secret)
    return sent


# ==========================================================================
# Direct SSH: positive path
# ==========================================================================


def test_direct_target_prompt_recognized_and_password_sent_once(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_prompt_and_succeed(tmp_path))
    result = terminal.open_device_terminal("R1", _DIRECT_CONFIG)
    assert result["transport"] == "ssh"
    snapshot = terminal.capture_device_terminal_view("R1")
    assert "AUTH-OK" in snapshot.pane_text
    assert _SENTINEL_PASSWORD not in snapshot.pane_text


def test_password_send_count_is_exactly_one_on_success(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_prompt_and_succeed(tmp_path))
    sent = _track_sends(monkeypatch)
    terminal.open_device_terminal("R1", _DIRECT_CONFIG)
    assert sent.count(_SENTINEL_PASSWORD) == 1


# ==========================================================================
# Wrong-host / ambiguous prompt
# ==========================================================================


def test_wrong_host_prompt_never_sends_password(monkeypatch, tmp_path):
    jump_config = dict(_DIRECT_CONFIG)
    jump_config["jump_host_config"] = {"address": "192.0.2.10", "username": "jump-user"}
    # A prompt attributable to neither the target's nor the jump host's
    # own address -- must fail closed, zero sends.
    _monkeypatch_transport(monkeypatch, _script_prompt_and_succeed(tmp_path, user_host="someone@another-host.example"))
    sent = _track_sends(monkeypatch)
    with pytest.raises(terminal.TerminalError, match="ambiguous password prompt"):
        terminal.open_device_terminal("R1", jump_config)
    assert _SENTINEL_PASSWORD not in sent


# ==========================================================================
# Key/agent success: zero password sends
# ==========================================================================


def test_key_auth_success_sends_zero_passwords(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_never_prompts(tmp_path))
    sent = _track_sends(monkeypatch)
    result = terminal.open_device_terminal("R1", _DIRECT_CONFIG)
    assert result["transport"] == "ssh"
    assert _SENTINEL_PASSWORD not in sent


# ==========================================================================
# No password configured
# ==========================================================================


def test_missing_configured_password_fails_safely(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_prompt_and_succeed(tmp_path))
    sent = _track_sends(monkeypatch)
    with pytest.raises(terminal.TerminalError) as excinfo:
        terminal.open_device_terminal("R1", _NO_PASSWORD_CONFIG)
    assert "no private password is configured" in str(excinfo.value)
    assert len(sent) == 1  # only the fake ssh command itself was ever typed in


# ==========================================================================
# Repeated prompt after one send: fail closed, no second send
# ==========================================================================


def test_repeated_prompt_after_send_fails_closed_without_resending(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_prompt_twice(tmp_path))
    sent = _track_sends(monkeypatch)
    with pytest.raises(terminal.TerminalError, match="password rejected"):
        terminal.open_device_terminal("R1", _DIRECT_CONFIG)
    assert sent.count(_SENTINEL_PASSWORD) == 1  # never sent a second time


# ==========================================================================
# Explicit authentication failure
# ==========================================================================


def test_explicit_authentication_failure_message(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_prompt_then_failure(tmp_path))
    with pytest.raises(terminal.TerminalError) as excinfo:
        terminal.open_device_terminal("R1", _DIRECT_CONFIG)
    assert "authentication failed" in str(excinfo.value).lower()
    assert _SENTINEL_PASSWORD not in str(excinfo.value)


# ==========================================================================
# Timeout
# ==========================================================================


def test_timeout_after_password_sent_fails_bounded(monkeypatch, tmp_path):
    # A prompt appears, the password is sent, but nothing further ever
    # happens -- must fail after the (shortened, for this test) bounded
    # settle timeout, not hang.
    _monkeypatch_transport(monkeypatch, _script_prompt_then_hangs(tmp_path))
    start = time.monotonic()
    with pytest.raises(terminal.TerminalError, match="timed out"):
        terminal.open_device_terminal("R1", _DIRECT_CONFIG)
    assert time.monotonic() - start < 15  # bounded, well under the real 30s sleep


# ==========================================================================
# Session cleanup ownership
# ==========================================================================


def test_newly_created_session_closed_on_definitive_auth_failure(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_prompt_then_failure(tmp_path))
    with pytest.raises(terminal.TerminalError):
        terminal.open_device_terminal("R1", _DIRECT_CONFIG)
    assert "R1" not in {s["device"] for s in terminal.list_device_sessions()}


def test_pre_existing_session_is_not_destroyed_by_a_later_failed_open(monkeypatch, tmp_path):
    # First, a normal (non-auth) session exists for a *different* device --
    # an unrelated failure on R1 must never touch it.
    _monkeypatch_transport(monkeypatch, _script_harmless(tmp_path, "hello-r2", name="r2.sh"))
    terminal.open_device_terminal("R2", {"transport": "ssh", "address": "192.0.2.21"})

    _monkeypatch_transport(monkeypatch, _script_prompt_then_failure(tmp_path))
    with pytest.raises(terminal.TerminalError):
        terminal.open_device_terminal("R1", _DIRECT_CONFIG)

    assert "R2" in {s["device"] for s in terminal.list_device_sessions()}
    assert "R1" not in {s["device"] for s in terminal.list_device_sessions()}


# ==========================================================================
# Pre-existing session already sitting at the password prompt
# ==========================================================================


def test_preexisting_session_at_password_prompt_completes_authentication(monkeypatch, tmp_path):
    script = _script_prompt_and_succeed(tmp_path)
    _monkeypatch_transport(monkeypatch, script)
    monkeypatch.setattr(terminal, "_authenticate_managed_session", lambda *a, **k: None)
    terminal.open_device_terminal("R1", _DIRECT_CONFIG)  # creates the session, auth bypassed
    assert len(terminal.list_device_sessions()) == 1

    monkeypatch.undo()  # restore the real _authenticate_managed_session
    # Re-apply the transport/timeout patches undone by the blanket undo().
    _monkeypatch_transport(monkeypatch, script)
    monkeypatch.setattr(terminal, "_MANAGED_LOGIN_TIMEOUT_SECONDS", 6)
    monkeypatch.setattr(terminal, "_MANAGED_AUTH_SETTLE_TIMEOUT_SECONDS", 6)

    result = terminal.open_device_terminal("R1", _DIRECT_CONFIG)
    assert result["reused"] is True
    assert len(terminal.list_device_sessions()) == 1  # no second session created
    snapshot = terminal.capture_device_terminal_view("R1")
    assert "AUTH-OK" in snapshot.pane_text
    assert _SENTINEL_PASSWORD not in snapshot.pane_text


# ==========================================================================
# Pre-existing already-authenticated session remains fully idempotent
# ==========================================================================


def test_preexisting_authenticated_session_is_untouched(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_harmless(tmp_path, "READY-ALREADY"))
    terminal.open_device_terminal("R1", _DIRECT_CONFIG)
    baseline = terminal.capture_device_terminal_view("R1").pane_text

    sent = []
    monkeypatch.setattr(terminal, "_send_literal_text", lambda *a: sent.append(a[1]))
    monkeypatch.setattr(terminal, "_send_secret_text", lambda *a: sent.append(a[1]))
    monkeypatch.setattr(terminal, "_send_enter", lambda *a: sent.append("<enter>"))

    result = terminal.open_device_terminal("R1", _DIRECT_CONFIG)

    assert result["reused"] is True
    assert sent == []  # no send, no Enter -- session left completely undisturbed
    assert terminal.capture_device_terminal_view("R1").pane_text == baseline


# ==========================================================================
# Concurrency
# ==========================================================================


def test_concurrent_same_device_open_sends_password_at_most_once(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_prompt_and_succeed(tmp_path))
    sent = []
    send_lock = threading.Lock()
    real_send = terminal._send_literal_text
    real_secret_send = terminal._send_secret_text

    def tracked(session_name, text):
        with send_lock:
            sent.append(text)
        return real_send(session_name, text)

    def tracked_secret(session_name, secret):
        with send_lock:
            sent.append(secret)
        return real_secret_send(session_name, secret)

    monkeypatch.setattr(terminal, "_send_literal_text", tracked)
    monkeypatch.setattr(terminal, "_send_secret_text", tracked_secret)

    results: list[dict] = []
    errors: list[Exception] = []

    def run():
        try:
            results.append(terminal.open_device_terminal("R1", _DIRECT_CONFIG))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == []
    assert len(results) == 3
    assert sent.count(_SENTINEL_PASSWORD) == 1
    assert len(terminal.list_device_sessions()) == 1


def test_concurrent_different_device_authentication_overlaps_and_is_isolated(monkeypatch, tmp_path):
    barrier = threading.Barrier(2, timeout=15)
    sent: dict[str, list[str]] = {"R1": [], "R2": []}
    sent_lock = threading.Lock()
    real_send = terminal._send_literal_text
    real_secret_send = terminal._send_secret_text

    def tracked(session_name, text):
        device = terminal.production_device_name(session_name)
        with sent_lock:
            sent[device].append(text)
        return real_send(session_name, text)

    def tracked_secret(session_name, secret):
        device = terminal.production_device_name(session_name)
        with sent_lock:
            sent[device].append(secret)
        return real_secret_send(session_name, secret)

    monkeypatch.setattr(terminal, "_send_literal_text", tracked)
    monkeypatch.setattr(terminal, "_send_secret_text", tracked_secret)

    real_wait = terminal._wait_for_pattern
    entered: list[str] = []

    def barrier_wait(session_name, pattern, timeout, poll_interval=0.05, baseline_text=None):
        if pattern is terminal._MANAGED_LOGIN_WAIT_RE:
            entered.append(session_name)
            barrier.wait()
        return real_wait(session_name, pattern, timeout, poll_interval=poll_interval, baseline_text=baseline_text)

    monkeypatch.setattr(terminal, "_wait_for_pattern", barrier_wait)

    config_r1 = dict(_DIRECT_CONFIG)
    config_r1["password"] = "R1-ONLY-SECRET"
    config_r2 = {"transport": "ssh", "address": "192.0.2.22", "username": "r2-user", "password": "R2-ONLY-SECRET"}

    script_r1 = _script_prompt_and_succeed(tmp_path, user_host="r1-user@192.0.2.20", name="r1.sh")
    script_r2 = _script_prompt_and_succeed(tmp_path, user_host="r2-user@192.0.2.22", name="r2.sh")

    monkeypatch.setattr(
        terminal,
        "_build_transport_command",
        lambda config, **kw: ("ssh", script_r1 if config.get("address") == "192.0.2.20" else script_r2),
    )

    t1 = threading.Thread(target=lambda: terminal.open_device_terminal("R1", config_r1))
    t2 = threading.Thread(target=lambda: terminal.open_device_terminal("R2", config_r2))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert set(entered) == {"network-lab-device-R1", "network-lab-device-R2"}
    assert sent["R1"].count("R1-ONLY-SECRET") == 1
    assert sent["R2"].count("R2-ONLY-SECRET") == 1
    assert "R2-ONLY-SECRET" not in sent["R1"]
    assert "R1-ONLY-SECRET" not in sent["R2"]


# ==========================================================================
# Secret non-leak
# ==========================================================================


def test_secret_absent_from_mcp_tool_result(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_prompt_and_succeed(tmp_path))
    from network_lab_mcp import lab, mcp_server

    monkeypatch.setattr(lab, "get_device", lambda device: ("sample_lab", _DIRECT_CONFIG))

    async def call():
        return await mcp_server.mcp.call_tool("terminal_open", {"device": "R1"})

    result = anyio.run(call)
    serialized = str(result)
    assert _SENTINEL_PASSWORD not in serialized


def test_secret_absent_from_exception_on_failure(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_prompt_then_failure(tmp_path))
    with pytest.raises(terminal.TerminalError) as excinfo:
        terminal.open_device_terminal("R1", _DIRECT_CONFIG)
    assert _SENTINEL_PASSWORD not in str(excinfo.value)
    assert _SENTINEL_PASSWORD not in repr(excinfo.value)


def test_secret_absent_from_mcp_tool_error_on_failure(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_prompt_then_failure(tmp_path))
    from network_lab_mcp import lab, mcp_server
    from mcp.server.mcpserver.exceptions import ToolError

    monkeypatch.setattr(lab, "get_device", lambda device: ("sample_lab", _DIRECT_CONFIG))

    async def call():
        return await mcp_server.mcp.call_tool("terminal_open", {"device": "R1"})

    with pytest.raises(ToolError) as excinfo:
        anyio.run(call)
    assert _SENTINEL_PASSWORD not in str(excinfo.value)


def test_secret_never_appears_in_process_argv():
    """Never invoke anything equivalent to `sshpass -p PASSWORD` or bake
    the password into the ssh command line itself -- confirmed structurally
    by proving _build_transport_command()'s own argv never contains it."""
    _, command = terminal._build_transport_command(_DIRECT_CONFIG)
    assert _SENTINEL_PASSWORD not in " ".join(command)
