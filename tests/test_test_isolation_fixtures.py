# SPDX-License-Identifier: Apache-2.0
"""The suite's own isolation guarantees (see conftest.py): a private
TMUX_TMPDIR with no inherited TMUX/TMUX_PANE, a safe owned-directory
cleanup, and a LOGS_ROOT that never points into the repository."""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

import conftest
from network_lab_mcp import terminal

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_logs_root_never_points_into_the_repository():
    root = Path(os.path.realpath(terminal.LOGS_ROOT))
    assert _REPO_ROOT not in root.parents and root != _REPO_ROOT


def test_default_terminal_logs_are_written_under_the_isolated_logs_root(monkeypatch):
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["bash", "-c", "cat"]))
    monkeypatch.setattr(terminal, "_authenticate_managed_session", lambda *a, **k: None)
    terminal.open_device_terminal("ISOLLOG", {"transport": "ssh", "address": "192.0.2.1"})
    try:
        logs = list((terminal.LOGS_ROOT / "ISOLLOG").glob("*.log"))
    finally:
        terminal.close_device_terminal("ISOLLOG")
    assert len(logs) == 1
    assert not (_REPO_ROOT / "logs" / "terminal" / "ISOLLOG").exists()


def test_tmux_runs_in_a_private_directory_without_inherited_tmux_env():
    assert "TMUX" not in os.environ and "TMUX_PANE" not in os.environ
    tmpdir = Path(os.environ["TMUX_TMPDIR"])
    assert tmpdir.is_absolute() and tmpdir == tmpdir.resolve()
    assert stat.S_IMODE(tmpdir.stat().st_mode) == 0o700

    terminal.open_validation_session("isolprobe", ["sleep", "30"])
    try:
        name = terminal.derive_validation_session_name("isolprobe")
        result = terminal._run(["display-message", "-t", name, "-p", "#{socket_path}"])
        socket_path = Path(os.path.realpath(result.stdout.strip()))
    finally:
        terminal.close_validation_session("isolprobe")
    assert tmpdir in socket_path.parents
    assert socket_path.name == terminal.TMUX_SOCKET_NAME
    assert terminal.TMUX_SOCKET_NAME.startswith("network-lab-mcp-test-")


def _make_owned_tmux_like_dir() -> tuple[str, str, int, str]:
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="nlm-", dir="/tmp")
    os.chmod(tmpdir, 0o700)
    real = os.path.realpath(tmpdir)
    sock_dir = os.path.join(real, f"tmux-{os.getuid()}")
    os.mkdir(sock_dir, 0o700)
    sock_path = os.path.join(sock_dir, "s")
    server = socket.socket(socket.AF_UNIX)
    server.bind(sock_path)
    server.close()  # leaves a stale socket file, exactly like an exited tmux server
    return tmpdir, real, os.lstat(real).st_ino, sock_path


def test_owned_tmpdir_cleanup_removes_directory_with_stale_socket():
    tmpdir, real, inode, sock_path = _make_owned_tmux_like_dir()
    assert stat.S_ISSOCK(os.lstat(sock_path).st_mode)
    conftest._remove_owned_tmux_tmpdir(tmpdir, real, inode)
    assert not os.path.exists(real)


def test_owned_tmpdir_cleanup_refuses_unexpected_content_or_identity_mismatch():
    tmpdir, real, inode, sock_path = _make_owned_tmux_like_dir()
    try:
        conftest._remove_owned_tmux_tmpdir(tmpdir, real, inode + 1)  # inode differs from what was recorded
        assert os.path.exists(sock_path)
        conftest._remove_owned_tmux_tmpdir(tmpdir, real + "-other", inode)  # real path differs
        assert os.path.exists(sock_path)
        stray = os.path.join(real, "unexpected.txt")
        Path(stray).write_text("x")
        conftest._remove_owned_tmux_tmpdir(tmpdir, real, inode)  # regular file present
        assert os.path.exists(stray) and os.path.exists(sock_path)
    finally:
        os.unlink(os.path.join(real, "unexpected.txt")) if os.path.exists(os.path.join(real, "unexpected.txt")) else None
        os.unlink(sock_path)
        os.rmdir(os.path.dirname(sock_path))
        os.rmdir(real)
