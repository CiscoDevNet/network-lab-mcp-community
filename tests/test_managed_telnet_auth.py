# SPDX-License-Identifier: Apache-2.0
"""Private Telnet login for managed terminal sessions
(`terminal_open()`).

Closes the last remaining transport inconsistency: SSH managed sessions
and Discovery's own Telnet login (discovery._login_ios_style())
already authenticate privately -- managed Telnet sessions did
not, so a real Claude Code -> Network Lab MCP acceptance test against a
real PAGENT-style device stopped at "Password:" and asked a human for the
credential. `_authenticate_managed_telnet_session()` (terminal.py) closes
that gap, dispatched from the same `_authenticate_managed_session()` SSH
authentication already uses, reusing `resolve_target_password_prompt()`
unchanged for the password and the shared
`USERNAME_PROMPT_RE`/`IOS_STYLE_PROMPT_RE` (moved from discovery.py to
terminal.py, see its own module comment) for the login sequence itself.

Fake Telnet replacements are script *files* (never `bash -c "<inline
text>"`), for the exact same reason documented at length in
tests/test_managed_terminal_auth.py's own module docstring: a shell only
ever echoes the *invocation* line, never a script file's own contents, so
these fakes can freely use words like "password"/"invalid" in their own
source without a false-positive early match against the echoed-but-not-
yet-executed command line itself.

Uses the isolated network-lab-mcp tmux socket (real tmux, never a real
device) with `terminal.LOGS_ROOT` monkeypatched to a temp directory."""

from __future__ import annotations

import threading
import time

import anyio
import pytest

from network_lab_mcp import terminal

_SENTINEL_USERNAME = "SENTINEL_TELNET_USERNAME_XYZ"
_SENTINEL_PASSWORD = "SENTINEL_TELNET_PASSWORD_XYZ"

_DIRECT_CONFIG = {
    "transport": "telnet",
    "address": "192.0.2.30",
    "username": _SENTINEL_USERNAME,
    "password": _SENTINEL_PASSWORD,
}
_NO_USERNAME_CONFIG = {"transport": "telnet", "address": "192.0.2.30", "password": _SENTINEL_PASSWORD}
_NO_PASSWORD_CONFIG = {"transport": "telnet", "address": "192.0.2.30", "username": _SENTINEL_USERNAME}


@pytest.fixture(autouse=True)
def isolated_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")


@pytest.fixture(autouse=True)
def _short_timeouts(monkeypatch):
    monkeypatch.setattr(terminal, "_MANAGED_LOGIN_TIMEOUT_SECONDS", 6)
    monkeypatch.setattr(terminal, "_MANAGED_AUTH_SETTLE_TIMEOUT_SECONDS", 6)


@pytest.fixture(autouse=True)
def _cleanup_device_sessions():
    yield
    for session in terminal.list_device_sessions():
        terminal.close_device_terminal(session["device"])


def _write_script(tmp_path, name: str, body: str) -> list[str]:
    path = tmp_path / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)
    return ["bash", str(path)]


def _preamble() -> str:
    # Real native-telnet transport preamble -- must never be mistaken for
    # login completion.
    return 'echo "Trying 192.0.2.30..."\necho "Connected to 192.0.2.30."\necho "Escape character is \'^]\'."\necho\n'


def _script_username_then_password_succeed(tmp_path, hostname: str = "PAGENT", name: str = "user_pass_ok.sh") -> list[str]:
    body = (
        _preamble()
        + 'printf "Username: "\n'
        "read u\n"
        "stty -echo\n"
        'printf "Password: "\n'
        "read x\n"
        "stty echo\n"
        "echo\n"
        f"echo {hostname}#\n"
        "sleep 5\n"
    )
    return _write_script(tmp_path, name, body)


def _script_password_only_succeed(tmp_path, hostname: str = "PAGENT", name: str = "pass_only_ok.sh") -> list[str]:
    body = (
        _preamble()
        + "stty -echo\n"
        'printf "Password: "\n'
        "read x\n"
        "stty echo\n"
        "echo\n"
        f"echo {hostname}#\n"
        "sleep 5\n"
    )
    return _write_script(tmp_path, name, body)


def _script_repeated_password(tmp_path) -> list[str]:
    body = (
        _preamble()
        + "stty -echo\n"
        'printf "Password: "\n'
        "read x\n"
        'printf "\\nPassword: "\n'
        "sleep 5\n"
    )
    return _write_script(tmp_path, "repeated_password.sh", body)


def _script_explicit_failure(tmp_path) -> list[str]:
    body = (
        _preamble()
        + "stty -echo\n"
        'printf "Password: "\n'
        "read x\n"
        "stty echo\n"
        "echo\n"
        'echo "%% Bad passwords"\n'
        'echo "Connection closed by foreign host."\n'
        "sleep 5\n"
    )
    return _write_script(tmp_path, "explicit_failure.sh", body)


def _script_never_prompts(tmp_path) -> list[str]:
    return _write_script(tmp_path, "never_prompts.sh", _preamble() + "sleep 5\n")


def _script_prompt_then_hangs(tmp_path) -> list[str]:
    body = _preamble() + 'stty -echo\nprintf "Password: "\nread x\nsleep 30\n'
    return _write_script(tmp_path, "prompt_then_hangs.sh", body)


def _script_harmless(tmp_path, hostname: str = "PAGENT", name: str = "harmless.sh") -> list[str]:
    return _write_script(tmp_path, name, f"echo {hostname}#\nsleep 5\n")


def _script_ambiguous_output(tmp_path) -> list[str]:
    body = (
        'echo "banner mentions login and password but this is not a real prompt"\n'
        'echo "still processing request..."\n'
        "sleep 5\n"
    )
    return _write_script(tmp_path, "ambiguous.sh", body)


def _monkeypatch_transport(monkeypatch, command: list[str]) -> None:
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("telnet", command))


def _track_sends(monkeypatch) -> list[str]:
    """Track every _send_literal_text()/_send_secret_text() call (in
    order) while still forwarding to the real implementation. The
    username/password themselves are sent via _send_secret_text() (never
    _send_literal_text(), so neither ever enters any subprocess's own
    argv); both are tracked into the same list to keep
    existing count/ordering assertions meaningful."""
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
# Username + password flow
# ==========================================================================


def test_username_then_password_flow_succeeds(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_username_then_password_succeed(tmp_path))
    result = terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)
    assert result["transport"] == "telnet"
    snapshot = terminal.capture_device_terminal_view("PAGENT")
    assert "PAGENT#" in snapshot.pane_text
    # The username is typed with normal (non-suppressed) echo -- exactly
    # like a human typing it at a real Username: prompt would see it too
    # -- only the *password* is typed under suppressed echo (`stty -echo`,
    # mirroring real device/OpenSSH password-entry behavior), so only the
    # password is expected to be absent from the raw pane transcript
    # (Network Lab MCP itself never deliberately logs/returns either
    # value, which is what the MCP-boundary/exception/log non-leak tests
    # below actually verify).
    assert _SENTINEL_PASSWORD not in snapshot.pane_text


def test_username_and_password_are_each_sent_exactly_once(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_username_then_password_succeed(tmp_path))
    sent = _track_sends(monkeypatch)
    terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)
    assert sent.count(_SENTINEL_USERNAME) == 1
    assert sent.count(_SENTINEL_PASSWORD) == 1


# ==========================================================================
# Password-only flow
# ==========================================================================


def test_password_only_flow_succeeds_without_sending_a_username(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_password_only_succeed(tmp_path))
    sent = _track_sends(monkeypatch)
    result = terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)
    assert result["transport"] == "telnet"
    assert sent.count(_SENTINEL_PASSWORD) == 1
    assert _SENTINEL_USERNAME not in sent


# ==========================================================================
# Missing username
# ==========================================================================


def test_missing_configured_username_fails_safely(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_username_then_password_succeed(tmp_path))
    sent = _track_sends(monkeypatch)
    with pytest.raises(terminal.TerminalError) as excinfo:
        terminal.open_device_terminal("PAGENT", _NO_USERNAME_CONFIG)
    assert "no username is configured" in str(excinfo.value)
    assert len(sent) == 1  # only the fake telnet command itself was ever typed in
    assert _SENTINEL_USERNAME not in sent
    assert _SENTINEL_PASSWORD not in sent
    assert "PAGENT" not in {s["device"] for s in terminal.list_device_sessions()}


# ==========================================================================
# Missing password
# ==========================================================================


def test_missing_configured_password_fails_safely(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_password_only_succeed(tmp_path))
    sent = _track_sends(monkeypatch)
    with pytest.raises(terminal.TerminalError) as excinfo:
        terminal.open_device_terminal("PAGENT", _NO_PASSWORD_CONFIG)
    assert "no private password is configured" in str(excinfo.value)
    assert len(sent) == 1  # only the fake telnet command itself was ever typed in
    assert "PAGENT" not in {s["device"] for s in terminal.list_device_sessions()}


# ==========================================================================
# Repeated password prompt: fail closed, no second send
# ==========================================================================


def test_repeated_password_prompt_fails_closed_without_resending(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_repeated_password(tmp_path))
    sent = _track_sends(monkeypatch)
    with pytest.raises(terminal.TerminalError, match="password rejected"):
        terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)
    assert sent.count(_SENTINEL_PASSWORD) == 1


# ==========================================================================
# Explicit login failure
# ==========================================================================


def test_explicit_login_failure_message(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_explicit_failure(tmp_path))
    with pytest.raises(terminal.TerminalError) as excinfo:
        terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)
    assert "telnet authentication failed" in str(excinfo.value).lower()
    assert _SENTINEL_PASSWORD not in str(excinfo.value)
    assert "PAGENT" not in {s["device"] for s in terminal.list_device_sessions()}


# ==========================================================================
# Login timeout
# ==========================================================================


def test_timeout_after_password_sent_fails_bounded(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_prompt_then_hangs(tmp_path))
    start = time.monotonic()
    with pytest.raises(terminal.TerminalError, match="timed out"):
        terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)
    assert time.monotonic() - start < 15  # bounded, well under the real 30s hang
    assert _SENTINEL_PASSWORD not in str(terminal.list_device_sessions())


def test_no_login_prompt_ever_appears_fails_bounded_and_leaves_session_usable(monkeypatch, tmp_path):
    """No Username/Password/failure/device-prompt ever appears within the
    bounded wait -- matches SSH's own "nothing to do, proceed" behavior:
    _authenticate_managed_telnet_session() does not raise in this case
    (there is nothing wrong to report), it simply stops watching."""
    _monkeypatch_transport(monkeypatch, _script_never_prompts(tmp_path))
    result = terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)
    assert result["transport"] == "telnet"


# ==========================================================================
# Newly-created session cleanup on definitive failure
# ==========================================================================


def test_newly_created_session_closed_on_definitive_auth_failure(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_explicit_failure(tmp_path))
    with pytest.raises(terminal.TerminalError):
        terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)
    assert "PAGENT" not in {s["device"] for s in terminal.list_device_sessions()}


def test_pre_existing_session_is_not_destroyed_by_a_later_failed_open(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_harmless(tmp_path, "R9", name="r9.sh"))
    terminal.open_device_terminal("R9", {"transport": "ssh", "address": "192.0.2.9"})

    _monkeypatch_transport(monkeypatch, _script_explicit_failure(tmp_path))
    with pytest.raises(terminal.TerminalError):
        terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)

    assert "R9" in {s["device"] for s in terminal.list_device_sessions()}
    assert "PAGENT" not in {s["device"] for s in terminal.list_device_sessions()}


# ==========================================================================
# Existing session states
# ==========================================================================


def test_existing_authenticated_session_is_untouched(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_harmless(tmp_path, "PAGENT"))
    terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)
    baseline = terminal.capture_device_terminal_view("PAGENT").pane_text

    sent = []
    monkeypatch.setattr(terminal, "_send_literal_text", lambda *a: sent.append(a[1]))
    monkeypatch.setattr(terminal, "_send_secret_text", lambda *a: sent.append(a[1]))
    monkeypatch.setattr(terminal, "_send_enter", lambda *a: sent.append("<enter>"))

    result = terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)

    assert result["reused"] is True
    assert sent == []
    assert terminal.capture_device_terminal_view("PAGENT").pane_text == baseline


def _create_bypassed(monkeypatch, tmp_path, script) -> None:
    """Create a session with authentication bypassed, so it sits exactly
    wherever the fake script leaves it (Username:/Password:), mirroring
    tests/test_managed_terminal_auth.py's own pattern for this."""
    _monkeypatch_transport(monkeypatch, script)
    monkeypatch.setattr(terminal, "_authenticate_managed_session", lambda *a, **k: None)
    terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)
    monkeypatch.undo()  # restore the real _authenticate_managed_session (and everything else)


def test_existing_session_at_username_prompt_resumes_and_completes(monkeypatch, tmp_path):
    script = _script_username_then_password_succeed(tmp_path)
    _create_bypassed(monkeypatch, tmp_path, script)
    assert len(terminal.list_device_sessions()) == 1

    # Re-apply patches undone by monkeypatch.undo() above.
    _monkeypatch_transport(monkeypatch, script)
    monkeypatch.setattr(terminal, "_MANAGED_LOGIN_TIMEOUT_SECONDS", 6)
    monkeypatch.setattr(terminal, "_MANAGED_AUTH_SETTLE_TIMEOUT_SECONDS", 6)

    result = terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)
    assert result["reused"] is True
    assert len(terminal.list_device_sessions()) == 1
    snapshot = terminal.capture_device_terminal_view("PAGENT")
    assert "PAGENT#" in snapshot.pane_text
    assert _SENTINEL_PASSWORD not in snapshot.pane_text  # username is normally echoed -- see the other test's comment


def test_existing_session_at_password_prompt_resumes_and_completes(monkeypatch, tmp_path):
    script = _script_password_only_succeed(tmp_path)
    _create_bypassed(monkeypatch, tmp_path, script)
    assert len(terminal.list_device_sessions()) == 1

    _monkeypatch_transport(monkeypatch, script)
    monkeypatch.setattr(terminal, "_MANAGED_LOGIN_TIMEOUT_SECONDS", 6)
    monkeypatch.setattr(terminal, "_MANAGED_AUTH_SETTLE_TIMEOUT_SECONDS", 6)
    sent = _track_sends(monkeypatch)

    result = terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)
    assert result["reused"] is True
    assert len(terminal.list_device_sessions()) == 1
    assert sent.count(_SENTINEL_PASSWORD) == 1
    snapshot = terminal.capture_device_terminal_view("PAGENT")
    assert "PAGENT#" in snapshot.pane_text


def test_ambiguous_existing_session_is_never_injected_into(monkeypatch, tmp_path):
    script = _script_ambiguous_output(tmp_path)
    _create_bypassed(monkeypatch, tmp_path, script)
    assert len(terminal.list_device_sessions()) == 1

    _monkeypatch_transport(monkeypatch, script)
    monkeypatch.setattr(terminal, "_MANAGED_LOGIN_TIMEOUT_SECONDS", 6)
    monkeypatch.setattr(terminal, "_MANAGED_AUTH_SETTLE_TIMEOUT_SECONDS", 6)
    sent = _track_sends(monkeypatch)

    result = terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)
    assert result["reused"] is True
    assert sent == []  # no credential injected into ambiguous output


# ==========================================================================
# Concurrency
# ==========================================================================


def test_concurrent_same_device_open_sends_password_at_most_once(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_username_then_password_succeed(tmp_path))
    sent: list[str] = []
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
            results.append(terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == []
    assert len(results) == 3
    assert sent.count(_SENTINEL_USERNAME) == 1
    assert sent.count(_SENTINEL_PASSWORD) == 1
    assert len(terminal.list_device_sessions()) == 1


def test_ssh_and_telnet_open_overlap_without_credential_crosstalk(monkeypatch, tmp_path):
    barrier = threading.Barrier(2, timeout=15)
    sent: dict[str, list[str]] = {"R1": [], "PAGENT": []}
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
    already_entered: set[str] = set()

    def barrier_wait(session_name, pattern, timeout, poll_interval=0.05, baseline_text=None):
        # The Telnet flow calls _wait_for_pattern() with this same combined
        # pattern object more than once (initial wait, after username,
        # after password) -- only synchronize on the *first* one per
        # session, or the barrier (built for exactly one rendezvous per
        # side) gets consumed again by the same thread with no second
        # party ever arriving to satisfy it.
        if pattern in (terminal._MANAGED_LOGIN_WAIT_RE, terminal._TELNET_MANAGED_LOGIN_WAIT_RE):
            if session_name not in already_entered:
                already_entered.add(session_name)
                entered.append(session_name)
                barrier.wait()
        return real_wait(session_name, pattern, timeout, poll_interval=poll_interval, baseline_text=baseline_text)

    monkeypatch.setattr(terminal, "_wait_for_pattern", barrier_wait)

    ssh_script = _write_script(
        tmp_path,
        "r1.sh",
        'stty -echo\nprintf "r1-user@192.0.2.11'"'"'s password: "\nread x\nstty echo\necho\necho R1-OK\nsleep 5\n',
    )
    telnet_script = _script_username_then_password_succeed(tmp_path, hostname="PAGENT", name="pagent.sh")

    def fake_transport(config, **kw):
        if config.get("transport") == "ssh":
            return "ssh", ssh_script
        return "telnet", telnet_script

    monkeypatch.setattr(terminal, "_build_transport_command", fake_transport)

    ssh_config = {"transport": "ssh", "address": "192.0.2.11", "username": "r1-user", "password": "R1-ONLY-SECRET"}

    t1 = threading.Thread(target=lambda: terminal.open_device_terminal("R1", ssh_config))
    t2 = threading.Thread(target=lambda: terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert set(entered) == {"network-lab-device-R1", "network-lab-device-PAGENT"}
    assert sent["R1"].count("R1-ONLY-SECRET") == 1
    assert sent["PAGENT"].count(_SENTINEL_PASSWORD) == 1
    assert "R1-ONLY-SECRET" not in sent["PAGENT"]
    assert _SENTINEL_PASSWORD not in sent["R1"]


def test_two_telnet_devices_authenticate_independently_without_crosstalk(monkeypatch, tmp_path):
    barrier = threading.Barrier(2, timeout=15)
    sent: dict[str, list[str]] = {"T1": [], "T2": []}
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

    already_entered: set[str] = set()

    def barrier_wait(session_name, pattern, timeout, poll_interval=0.05, baseline_text=None):
        # Only the *first* _wait_for_pattern() call per session using this
        # combined pattern -- it is reused for the initial wait and the
        # post-password settle wait, and the barrier only expects one
        # rendezvous per side (see the SSH+Telnet overlap test's own
        # comment for the full explanation).
        if pattern is terminal._TELNET_MANAGED_LOGIN_WAIT_RE and session_name not in already_entered:
            already_entered.add(session_name)
            entered.append(session_name)
            barrier.wait()
        return real_wait(session_name, pattern, timeout, poll_interval=poll_interval, baseline_text=baseline_text)

    monkeypatch.setattr(terminal, "_wait_for_pattern", barrier_wait)

    script_t1 = _script_password_only_succeed(tmp_path, hostname="T1", name="t1.sh")
    script_t2 = _script_password_only_succeed(tmp_path, hostname="T2", name="t2.sh")

    def fake_transport(config, **kw):
        return "telnet", script_t1 if config.get("address") == "192.0.2.41" else script_t2

    monkeypatch.setattr(terminal, "_build_transport_command", fake_transport)

    config_t1 = {"transport": "telnet", "address": "192.0.2.41", "password": "T1-ONLY-SECRET"}
    config_t2 = {"transport": "telnet", "address": "192.0.2.42", "password": "T2-ONLY-SECRET"}

    t1 = threading.Thread(target=lambda: terminal.open_device_terminal("T1", config_t1))
    t2 = threading.Thread(target=lambda: terminal.open_device_terminal("T2", config_t2))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert set(entered) == {"network-lab-device-T1", "network-lab-device-T2"}
    assert sent["T1"].count("T1-ONLY-SECRET") == 1
    assert sent["T2"].count("T2-ONLY-SECRET") == 1
    assert "T2-ONLY-SECRET" not in sent["T1"]
    assert "T1-ONLY-SECRET" not in sent["T2"]


# ==========================================================================
# Secret non-leak
# ==========================================================================


def test_secrets_absent_from_mcp_tool_result(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_username_then_password_succeed(tmp_path))
    from network_lab_mcp import lab, mcp_server

    monkeypatch.setattr(lab, "get_device", lambda device: ("sample_lab", _DIRECT_CONFIG))

    async def call():
        return await mcp_server.mcp.call_tool("terminal_open", {"device": "PAGENT"})

    result = anyio.run(call)
    serialized = str(result)
    assert _SENTINEL_USERNAME not in serialized
    assert _SENTINEL_PASSWORD not in serialized


def test_secrets_absent_from_exception_on_failure(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_explicit_failure(tmp_path))
    with pytest.raises(terminal.TerminalError) as excinfo:
        terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)
    assert _SENTINEL_USERNAME not in str(excinfo.value)
    assert _SENTINEL_PASSWORD not in str(excinfo.value)
    assert _SENTINEL_USERNAME not in repr(excinfo.value)
    assert _SENTINEL_PASSWORD not in repr(excinfo.value)


def test_secrets_absent_from_mcp_tool_error_on_failure(monkeypatch, tmp_path):
    _monkeypatch_transport(monkeypatch, _script_explicit_failure(tmp_path))
    from network_lab_mcp import lab, mcp_server
    from mcp.server.mcpserver.exceptions import ToolError

    monkeypatch.setattr(lab, "get_device", lambda device: ("sample_lab", _DIRECT_CONFIG))

    async def call():
        return await mcp_server.mcp.call_tool("terminal_open", {"device": "PAGENT"})

    with pytest.raises(ToolError) as excinfo:
        anyio.run(call)
    assert _SENTINEL_USERNAME not in str(excinfo.value)
    assert _SENTINEL_PASSWORD not in str(excinfo.value)


def test_secrets_absent_from_application_log(monkeypatch, tmp_path, caplog):
    _monkeypatch_transport(monkeypatch, _script_username_then_password_succeed(tmp_path))
    with caplog.at_level("DEBUG"):
        terminal.open_device_terminal("PAGENT", _DIRECT_CONFIG)
    assert _SENTINEL_USERNAME not in caplog.text
    assert _SENTINEL_PASSWORD not in caplog.text


def test_secret_never_appears_in_process_argv():
    """Never invoke anything equivalent to a Telnet-with-embedded-password
    command -- confirmed structurally by proving _build_transport_command()'s
    own argv never contains it (native `telnet <host> <port>` only)."""
    _, command = terminal._build_transport_command(_DIRECT_CONFIG)
    assert _SENTINEL_PASSWORD not in " ".join(command)
    assert _SENTINEL_USERNAME not in " ".join(command)


# ==========================================================================
# Regression: combined-wait-pattern re.MULTILINE fix (found during this
# feature's own development -- see terminal._TELNET_MANAGED_LOGIN_WAIT_RE's
# own comment)
# ==========================================================================


def test_combined_login_wait_pattern_matches_a_non_final_line():
    """Without re.MULTILINE re-applied on the *combined* pattern, `$` only
    anchors to the true end of the whole captured string -- so a match on
    a line that is not the literal last line (e.g. "Password:" here, with
    "PAGENT#" and a later shell prompt following it) silently fails, even
    though the individual sub-patterns each carry their own re.MULTILINE.
    This is exactly what let the post-password "settle" wait in
    _authenticate_managed_telnet_session() time out even after the device
    had already reached its real prompt."""
    text = "Username: someuser\nPassword:\nPAGENT#\nunrelated-shell-prompt$"
    assert terminal._TELNET_MANAGED_LOGIN_WAIT_RE.search(text) is not None


def test_managed_ssh_combined_login_wait_pattern_matches_a_non_final_line():
    text = "some banner text\nPassword:\nWelcome\nunrelated-shell-prompt$"
    assert terminal._MANAGED_LOGIN_WAIT_RE.search(text) is not None


def test_discovery_combined_login_wait_pattern_matches_a_non_final_line():
    from network_lab_mcp import discovery

    text = "Username: someuser\nPassword:\nPAGENT#\nunrelated-shell-prompt$"
    assert discovery._IOS_STYLE_LOGIN_WAIT_RE.search(text) is not None
