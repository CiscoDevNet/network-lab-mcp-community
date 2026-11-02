# SPDX-License-Identifier: Apache-2.0
"""Tunnel building blocks: telnet client pinning, loopback port, and
lsof -F based TCP ownership verification. Local only."""

from __future__ import annotations

import os
import socket
import stat
import subprocess

import pytest

from network_lab_mcp import terminal, tunnel_safety as ts

SSH, TEL, EVIL = 100, 200, 300
P = 40023


def snap(*recs):
    out = []
    for pid, cmd, name, state in recs:
        out += [f"p{pid}", f"c{cmd}", "f5", "tIPv4", f"n{name}", f"TST={state}", "TQR=0"]
    return "\n".join(out) + "\n"


GOOD = snap((SSH, "ssh", f"127.0.0.1:{P}", "LISTEN"),
            (TEL, "telnet", f"127.0.0.1:50000->127.0.0.1:{P}", "ESTABLISHED"),
            (SSH, "ssh", f"127.0.0.1:{P}->127.0.0.1:50000", "ESTABLISHED"))
KW = dict(tunnel_port=P, managed_ssh_pids=frozenset({SSH}), client_pids=frozenset({TEL}))


def test_good_snapshot_verifies():
    ident = ts.verify_tcp_ownership(GOOD, **KW)
    assert (ident.client_port, ident.tunnel_port, ident.server_pid) == (50000, P, SSH)


def test_A_port_taken_by_other_process():
    bad = snap((EVIL, "nc", f"127.0.0.1:{P}", "LISTEN"),
               (TEL, "telnet", f"127.0.0.1:50000->127.0.0.1:{P}", "ESTABLISHED"),
               (EVIL, "nc", f"127.0.0.1:{P}->127.0.0.1:50000", "ESTABLISHED"))
    with pytest.raises(ts.TunnelSafetyError):
        ts.verify_tcp_ownership(bad, **KW)


def test_E_controlmaster_other_owner_listener():
    bad = GOOD.replace(f"p{SSH}", "p999")
    with pytest.raises(ts.TunnelSafetyError):
        ts.verify_tcp_ownership(bad, **KW)


def test_B_server_side_foreign_owner():
    bad = snap((SSH, "ssh", f"127.0.0.1:{P}", "LISTEN"),
               (TEL, "telnet", f"127.0.0.1:50000->127.0.0.1:{P}", "ESTABLISHED"),
               (EVIL, "nc", f"127.0.0.1:{P}->127.0.0.1:50000", "ESTABLISHED"))
    with pytest.raises(ts.TunnelSafetyError):
        ts.verify_tcp_ownership(bad, **KW)


def test_C_listener_closed_and_D_connection_missing():
    only_conn = snap((TEL, "telnet", f"127.0.0.1:50000->127.0.0.1:{P}", "ESTABLISHED"))
    with pytest.raises(ts.TunnelSafetyError):
        ts.verify_tcp_ownership(only_conn, **KW)
    no_conn = snap((SSH, "ssh", f"127.0.0.1:{P}", "LISTEN"))
    with pytest.raises(ts.TunnelSafetyError):
        ts.verify_tcp_ownership(no_conn, **KW)


def test_F_missing_server_side_socket():
    bad = snap((SSH, "ssh", f"127.0.0.1:{P}", "LISTEN"),
               (TEL, "telnet", f"127.0.0.1:50000->127.0.0.1:{P}", "ESTABLISHED"))
    with pytest.raises(ts.TunnelSafetyError):
        ts.verify_tcp_ownership(bad, **KW)


def test_H_multiple_candidates():
    bad = GOOD + snap((TEL, "telnet", f"127.0.0.1:50001->127.0.0.1:{P}", "ESTABLISHED"),
                      (SSH, "ssh", f"127.0.0.1:{P}->127.0.0.1:50001", "ESTABLISHED"))
    with pytest.raises(ts.TunnelSafetyError):
        ts.verify_tcp_ownership(bad, **KW)


@pytest.mark.parametrize("junk", ["", "garbage", "pNOTANUMBER\nf1\n", "p1\nf1\nn127.0.0.1:1\n"])
def test_I_malformed_output(junk):
    with pytest.raises(ts.TunnelSafetyError):
        ts.verify_tcp_ownership(junk, **KW)


def test_G_lsof_missing_or_failing(monkeypatch):
    monkeypatch.setattr(ts.shutil, "which", lambda *a, **k: None)
    with pytest.raises(ts.TunnelSafetyError):
        ts.run_lsof()
    monkeypatch.setattr(ts.shutil, "which", lambda *a, **k: "/usr/sbin/lsof")
    monkeypatch.setattr(ts.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 2, "", "denied"))
    with pytest.raises(ts.TunnelSafetyError):
        ts.run_lsof()


def test_J_two_snapshots_must_agree():
    other = GOOD.replace("50000", "50001")
    with pytest.raises(ts.TunnelSafetyError):
        ts.verify_twice(GOOD, other, **KW)
    assert ts.verify_twice(GOOD, GOOD, **KW).client_port == 50000


def test_real_lsof_format_against_local_dummy_sockets():
    """Local loopback only: confirms the real macOS lsof -F shape parses."""
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    port = srv.getsockname()[1]
    cli = socket.create_connection(("127.0.0.1", port)); acc, _ = srv.accept()
    try:
        recs = ts.parse_lsof_f(ts.run_lsof())
        me = os.getpid()
        mine = [r for r in recs if r.pid == me]
        ident = ts.verify_tcp_ownership(
            ts.run_lsof(), tunnel_port=port, managed_ssh_pids=frozenset({me}), client_pids=frozenset({me}))
        assert ident.tunnel_port == port and mine
    finally:
        for s in (cli, acc, srv):
            s.close()


def test_telnet_client_resolution(tmp_path):
    with pytest.raises(ts.TunnelSafetyError, match="telnet client not found"):
        ts.resolve_telnet_client(str(tmp_path))
    fake = tmp_path / "telnet"
    fake.write_text("#!/bin/sh\n")
    with pytest.raises(ts.TunnelSafetyError):
        ts.resolve_telnet_client(str(tmp_path))  # not executable
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    resolved = ts.resolve_telnet_client(str(tmp_path))
    assert os.path.isabs(resolved)
    argv = ts.telnet_argv(resolved, 40023)
    assert argv[0] == resolved and argv[-2:] == ["127.0.0.1", "40023"] and "-K" in argv
    with pytest.raises(ts.TunnelSafetyError):
        ts.telnet_argv("telnet", 1)


def test_allocated_port_is_loopback_free_port():
    port = ts.allocate_loopback_port()
    assert 1024 <= port <= 65535


def test_discovery_refuses_telnet_via_jump_when_guard_off(monkeypatch):
    monkeypatch.setattr(terminal, "TELNET_VIA_JUMP_ENABLED", False)
    cfg = {"transport": "telnet", "address": "192.0.2.20", "jump_host_config": {"address": "192.0.2.10"}}
    with pytest.raises(terminal.TerminalError, match="not enabled"):
        terminal.open_bootstrap_terminal("LegacyRouter", cfg)
