# SPDX-License-Identifier: Apache-2.0
"""Test isolation: ordinary pytest must never be able to list,
read, send to, or close a real production managed session -- only the
disposable, per-run tmux socket the `_isolated_tmux_socket` autouse
fixture (conftest.py) redirects `terminal.TMUX_SOCKET_NAME` onto.

This file creates a *fake* session directly on the real production socket
(bypassing `terminal.py` entirely, via a raw `tmux` invocation, so it is
completely independent of whatever socket name `terminal.TMUX_SOCKET_NAME`
currently holds), then proves normal test-suite operations -- listing
sessions, running another test file's own cleanup-style sweep, closing an
*isolated* same-named device -- never see or touch it, and that it is
still present and unharmed at the end."""

from __future__ import annotations

import subprocess

from network_lab_mcp import terminal

_FAKE_PRODUCTION_SESSION = "network-lab-device-REALPROD"


def _production_tmux(socket_name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", "-L", socket_name, *args], capture_output=True, text=True)


def _production_session_exists(socket_name: str) -> bool:
    result = _production_tmux(socket_name, "has-session", "-t", _FAKE_PRODUCTION_SESSION)
    return result.returncode == 0


def test_test_suite_socket_is_never_the_production_socket(production_tmux_socket_name):
    assert terminal.TMUX_SOCKET_NAME != production_tmux_socket_name
    assert terminal.TMUX_SOCKET_NAME.startswith("network-lab-mcp-test-")


def test_fake_production_session_survives_unrelated_test_operations(
    production_tmux_socket_name, monkeypatch, tmp_path
):
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["bash", "-c", "sleep 5"]))
    monkeypatch.setattr(terminal, "_authenticate_managed_session", lambda *a, **k: None)

    _production_tmux(production_tmux_socket_name, "new-session", "-d", "-s", _FAKE_PRODUCTION_SESSION, "bash", "-c", "sleep 30")
    try:
        assert _production_session_exists(production_tmux_socket_name)

        # Ordinary, unrelated test-suite activity: create/close a same-
        # *named* device on the isolated test socket, and run the exact
        # kind of blanket "close every session" sweep several test files'
        # own cleanup fixtures perform.
        terminal.open_device_terminal("REALPROD", {"transport": "ssh", "address": "192.0.2.1"})
        assert "REALPROD" in {s["device"] for s in terminal.list_device_sessions()}
        for session in terminal.list_device_sessions():
            terminal.close_device_terminal(session["device"])
        assert terminal.list_device_sessions() == []

        # The real production session -- same device name, different
        # socket -- was never listed, read, sent to, or closed.
        assert _production_session_exists(production_tmux_socket_name)
    finally:
        _production_tmux(production_tmux_socket_name, "kill-session", "-t", _FAKE_PRODUCTION_SESSION)


def test_list_device_sessions_never_reports_the_fake_production_session(production_tmux_socket_name):
    _production_tmux(production_tmux_socket_name, "new-session", "-d", "-s", _FAKE_PRODUCTION_SESSION, "bash", "-c", "sleep 30")
    try:
        assert "REALPROD" not in {s["device"] for s in terminal.list_device_sessions()}
    finally:
        _production_tmux(production_tmux_socket_name, "kill-session", "-t", _FAKE_PRODUCTION_SESSION)
