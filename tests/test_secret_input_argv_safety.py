# SPDX-License-Identifier: Apache-2.0
"""Security hardening: private authentication input (a password, or a
Telnet username) must never appear in any subprocess's own argv.

Investigation found that `_send_literal_text()` (used by every credential
send before this fix) invokes `tmux send-keys -l -- <text>` -- placing
`text` directly as a literal argv element of that `tmux` subprocess for as
long as it runs. A process listing on the host (`ps`, `/proc/<pid>/
cmdline`, etc.) could observe that argv while the command is executing.
This is a real, confirmed gap for a *credential* specifically (ordinary
operator `terminal_send()` input is not subject to the same expectation).

The fix, `_send_secret_text()`: the secret is piped to `tmux load-buffer -`
over stdin (never an argv element) into a session-specific named buffer,
then `tmux paste-buffer -d` types that buffer's content into the pane and
deletes the buffer immediately afterward. This file proves, directly
against the real `subprocess.run` boundary, that the secret only ever
travels via `input=`, never `args=`."""

from __future__ import annotations

import subprocess

import pytest

from network_lab_mcp import terminal

_SENTINEL = "SENTINEL_ARGV_SAFETY_XYZ"


@pytest.fixture(autouse=True)
def _cleanup_validation_sessions():
    yield
    for session_name in list(terminal.list_validation_sessions()):
        validation_id = session_name[len(terminal.VALIDATION_PREFIX) :]
        terminal.close_validation_session(validation_id)


def _spy_on_subprocess_run(monkeypatch):
    real_run = subprocess.run
    calls: list[tuple[list[str], object]] = []

    def spy(args, **kwargs):
        calls.append((list(args), kwargs.get("input")))
        return real_run(args, **kwargs)

    monkeypatch.setattr(terminal.subprocess, "run", spy)
    return calls


def test_send_secret_text_never_places_the_secret_in_subprocess_argv(monkeypatch):
    calls = _spy_on_subprocess_run(monkeypatch)
    terminal.open_validation_session("argvtest", ["bash", "-c", "cat"])
    session_name = terminal.derive_validation_session_name("argvtest")

    terminal._send_secret_text(session_name, _SENTINEL)

    assert calls, "expected at least one subprocess.run call"
    for args, _stdin_input in calls:
        joined = " ".join(args)
        assert _SENTINEL not in joined, f"secret leaked into subprocess argv: {args}"
    # Confirm the secret *did* travel, just via stdin, not merely absent
    # everywhere (a no-op fake would trivially "pass" the check above).
    assert any(stdin_input == _SENTINEL for _, stdin_input in calls)


def test_send_secret_text_types_the_secret_into_the_pane(monkeypatch):
    """The secret still reaches the pane -- paste-buffer genuinely typed
    it in, this isn't merely "never sent anywhere"."""
    terminal.open_validation_session("argvtest2", ["bash", "-c", "cat"])
    session_name = terminal.derive_validation_session_name("argvtest2")

    terminal._send_secret_text(session_name, _SENTINEL)
    terminal._send_enter(session_name)

    import time

    time.sleep(0.3)
    pane = terminal._capture_pane(session_name, terminal.HISTORY_LIMIT)
    assert _SENTINEL in pane  # `cat` echoes back whatever it reads from stdin


def test_send_secret_text_cleans_up_its_named_buffer(monkeypatch):
    terminal.open_validation_session("argvtest3", ["bash", "-c", "cat"])
    session_name = terminal.derive_validation_session_name("argvtest3")

    terminal._send_secret_text(session_name, _SENTINEL)

    result = subprocess.run(
        terminal._tmux_base() + ["list-buffers"], capture_output=True, text=True
    )
    assert f"secret-{session_name}" not in result.stdout


def test_send_secret_to_bootstrap_never_places_the_secret_in_subprocess_argv(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")
    calls = _spy_on_subprocess_run(monkeypatch)
    monkeypatch.setattr(
        terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["bash", "-c", "cat"])
    )

    terminal.open_bootstrap_terminal("ARGVDEV", {"transport": "ssh", "address": "192.0.2.1"})
    try:
        terminal.send_secret_to_bootstrap("ARGVDEV", _SENTINEL)
    finally:
        terminal.close_bootstrap_terminal("ARGVDEV")

    for args, _stdin_input in calls:
        assert _SENTINEL not in " ".join(args)
    assert any(stdin_input == _SENTINEL for _, stdin_input in calls)
