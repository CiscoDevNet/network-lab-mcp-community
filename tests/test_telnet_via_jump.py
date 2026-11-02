# SPDX-License-Identifier: Apache-2.0
"""Stage C: Telnet over an SSH local port forward, end to end against local
fakes only: a fake ssh (password prompt + loopback listener that relays to a
local dummy device), a fake telnet client, a dummy Telnet device server, and
the isolated tmux. No real ssh/telnet binary, host or credential is used."""

from __future__ import annotations

import socket
import sys
import textwrap
import threading
import time

import pytest

from network_lab_mcp import terminal, tunnel_safety

JUMP_PW, DEV_USER, DEV_PW = "JUMP-SENTINEL", "dev-user-sentinel", "DEV-SENTINEL"

FAKE_SSH = '''#!{py}
import os, sys, socket, threading, termios, time
D = {d!r}
args = sys.argv[1:]
open(D + "/ssh_argv.txt", "a").write(" ".join(args) + "\\n")
port = int(args[args.index("-L") + 1].split(":")[1])
mode = open(D + "/mode.txt").read().strip()
n = int(open(D + "/launches.txt").read() or 0) if os.path.exists(D + "/launches.txt") else 0
open(D + "/launches.txt", "w").write(str(n + 1))
if mode == "hostkey":
    print("Host key verification failed."); sys.exit(255)
def noecho():
    a = termios.tcgetattr(0); a[3] &= ~termios.ECHO; termios.tcsetattr(0, termios.TCSANOW, a)
noecho()
def ask():
    sys.stdout.write("jump-user@192.0.2.10's password: "); sys.stdout.flush()
    line = sys.stdin.readline().strip()
    open(D + "/jump_pw.txt", "a").write(line + "\\n")
    return line
pw = ask()
if pw != {pw!r}:
    print("\\nPermission denied, please try again.")
    ask(); time.sleep(30)
print("\\nAuthenticated to 192.0.2.10 ([192.0.2.10]:22) using \\"password\\".", flush=True)
if mode == "conflict" or (mode == "conflict2" and n < 2):
    print("bind [127.0.0.1]:%d: Address already in use" % port)
    print("Could not request local forwarding.", flush=True); sys.exit(255)
if mode == "nolisten":
    time.sleep(60)
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", port)); srv.listen(5)
dev_port = int(open(D + "/device_port.txt").read())
def pump(a, b):
    try:
        while True:
            data = a.recv(4096)
            if not data: break
            b.sendall(data)
    except OSError: pass
    for s in (a, b):
        try: s.shutdown(socket.SHUT_RDWR)
        except OSError: pass
while True:
    c, _ = srv.accept()
    u = socket.create_connection(("127.0.0.1", dev_port))
    threading.Thread(target=pump, args=(c, u), daemon=True).start()
    threading.Thread(target=pump, args=(u, c), daemon=True).start()
'''

FAKE_TELNET = '''#!{py}
import sys, socket, threading, termios, tty, os
open({d!r} + "/telnet_argv.txt", "a").write(" ".join(sys.argv[1:]) + "\\n")
a = termios.tcgetattr(0); a[3] &= ~termios.ECHO; termios.tcsetattr(0, termios.TCSANOW, a)
tty.setcbreak(0)
s = socket.create_connection(("127.0.0.1", int(sys.argv[-1])))
def up():
    while True:
        b = os.read(0, 1024)
        if not b: break
        s.sendall(b)
threading.Thread(target=up, daemon=True).start()
while True:
    data = s.recv(4096)
    if not data: break
    os.write(1, data)
print("\\nConnection closed by foreign host.")
'''


class DeviceServer:
    def __init__(self, behavior="normal"):
        self.behavior = behavior
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.creds: list[tuple[str, str]] = []
        self.connections = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _line(self, c):
        buf = b""
        while True:
            ch = c.recv(1)
            if not ch:
                raise OSError
            if ch in b"\r\n":
                if buf:
                    return buf.decode()
                continue
            buf += ch

    def _client(self, c):
        try:
            if self.behavior == "close":
                return
            c.sendall(b"\r\nUsername: ")
            user = self._line(c)
            c.sendall(b"\r\nPassword: ")
            pw = self._line(c)
            self.creds.append((user, pw))
            c.sendall(b"\r\nPAGENT#")
            while True:
                cmd = self._line(c)
                if cmd == "quit":
                    return
                c.sendall(f"\r\nout:{cmd}\r\nPAGENT#".encode())
        except OSError:
            pass
        finally:
            c.close()

    def close(self):
        self.sock.close()


def _cfg(address="192.0.2.20"):
    return {
        "transport": "telnet", "address": address, "port": 23, "username": DEV_USER, "password": DEV_PW,
        "jump_host_config": {"address": "192.0.2.10", "username": "jump-user", "password": JUMP_PW},
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")
    d = tmp_path / "ctl"
    d.mkdir()
    (d / "mode.txt").write_text("ok")
    dev = DeviceServer()
    (d / "device_port.txt").write_text(str(dev.port))
    ssh, tel = tmp_path / "fake_ssh", tmp_path / "telnet"
    ssh.write_text(textwrap.dedent(FAKE_SSH).format(py=sys.executable, d=str(d), pw=JUMP_PW))
    tel.write_text(FAKE_TELNET.format(py=sys.executable, d=str(d)))
    for f in (ssh, tel):
        f.chmod(0o755)
    monkeypatch.setattr(terminal, "_resolve_ssh_binary", lambda: str(ssh))
    monkeypatch.setattr(tunnel_safety, "resolve_telnet_client", lambda path=None: str(tel))
    monkeypatch.setattr(terminal, "_TUNNEL_AUTH_TIMEOUT_SECONDS", 12)
    monkeypatch.setattr(terminal, "_TUNNEL_LISTEN_TIMEOUT_SECONDS", 4)
    monkeypatch.setattr(terminal, "_MANAGED_LOGIN_TIMEOUT_SECONDS", 8)
    monkeypatch.setattr(terminal, "_MANAGED_AUTH_SETTLE_TIMEOUT_SECONDS", 8)

    class E:
        pass

    e = E()
    e.dir, e.dev, e.ssh, e.tel = d, dev, ssh, tel
    e.mode = lambda m: (d / "mode.txt").write_text(m)
    e.read = lambda name: (d / name).read_text() if (d / name).exists() else ""
    yield e
    for s in terminal.list_device_sessions():
        terminal.close_device_terminal(s["device"])
    for name in ("PAGENT", "PAGENT2", "ORPH"):
        terminal._close_session(terminal.derive_discovery_session_name(name))
        terminal._kill_session_if_exists(terminal.derive_discovery_tunnel_session_name(name))
        terminal._close_session(terminal.derive_production_session_name(name))
        terminal._kill_session_if_exists(terminal.derive_tunnel_session_name(name))
    dev.close()


def _sessions():
    # the shared bootstrap initializer may legitimately exist from other tests
    return [n for n in terminal._list_sessions("network-lab-") if n != terminal.BOOTSTRAP_SESSION]


def test_full_lifecycle(env):
    result = terminal.open_device_terminal("PAGENT", _cfg())
    assert result == {"device": "PAGENT", "session_name": "network-lab-device-PAGENT", "reused": False,
                      "transport": "telnet"}
    assert env.read("jump_pw.txt").split() == [JUMP_PW]
    assert env.dev.creds == [(DEV_USER, DEV_PW)]
    # one logical session in list; the tunnel is internal
    assert terminal.list_device_sessions() == [
        {"device": "PAGENT", "session_name": "network-lab-device-PAGENT", "state": "running"}]
    assert terminal._session_exists("network-lab-tunnel-PAGENT")
    terminal.send_to_device("PAGENT", "show version", None, True)
    time.sleep(1)
    content = terminal.read_device("PAGENT")["content"]
    assert "out:show version" in content
    for secret in (JUMP_PW, DEV_USER, DEV_PW):
        assert secret not in content
        assert secret not in env.read("ssh_argv.txt") + env.read("telnet_argv.txt")
    ssh_argv, tel_argv = env.read("ssh_argv.txt"), env.read("telnet_argv.txt")
    assert "-N -T" in ssh_argv and "ExitOnForwardFailure=yes" in ssh_argv and "ControlPath=none" in ssh_argv
    assert "-L 127.0.0.1:" in ssh_argv and "StrictHostKeyChecking=yes" in ssh_argv
    assert tel_argv.startswith("-K -E 127.0.0.1 ")
    closed = terminal.close_device_terminal("PAGENT")
    assert closed["closed"] is True and _sessions() == []


def test_logs_do_not_contain_credentials(env, tmp_path):
    terminal.open_device_terminal("PAGENT", _cfg())
    terminal.send_to_device("PAGENT", "show clock", None, True)
    time.sleep(1)
    terminal.close_device_terminal("PAGENT")
    logs = list((tmp_path / "logs" / "terminal" / "PAGENT").glob("*.log"))
    assert len(logs) == 2  # tunnel pane + device pane, both preserved
    blob = "".join(p.read_text(errors="replace") for p in logs)
    assert "Authenticated to" in blob and "out:show clock" in blob
    for secret in (JUMP_PW, DEV_USER, DEV_PW):
        assert secret not in blob


def test_disabled_guard_opens_nothing(env, monkeypatch):
    monkeypatch.setattr(terminal, "TELNET_VIA_JUMP_ENABLED", False)
    with pytest.raises(terminal.TerminalError, match="telnet via jump host is not enabled"):
        terminal.open_device_terminal("PAGENT", _cfg())
    assert _sessions() == [] and env.read("ssh_argv.txt") == ""


def test_missing_telnet_client_starts_no_tunnel(env, monkeypatch):
    def missing(path=None):
        raise tunnel_safety.TunnelSafetyError("telnet client not found")
    monkeypatch.setattr(tunnel_safety, "resolve_telnet_client", missing)
    with pytest.raises(terminal.TerminalError, match="telnet client not found"):
        terminal.open_device_terminal("PAGENT", _cfg())
    assert _sessions() == [] and env.read("ssh_argv.txt") == ""


def test_host_key_failure(env):
    env.mode("hostkey")
    with pytest.raises(terminal.TerminalError, match="host key verification failed"):
        terminal.open_device_terminal("PAGENT", _cfg())
    assert _sessions() == [] and env.dev.connections == 0


def test_jump_password_rejected_sent_once(env):
    cfg = _cfg()
    cfg["jump_host_config"]["password"] = "WRONG-SENTINEL"
    with pytest.raises(terminal.TerminalError, match="jump host authentication failed"):
        terminal.open_device_terminal("PAGENT", cfg)
    assert env.read("jump_pw.txt").split() == ["WRONG-SENTINEL"]
    assert _sessions() == [] and env.dev.connections == 0


def test_unattributable_jump_prompt_sends_nothing(env, tmp_path):
    cfg = _cfg()
    cfg["jump_host_config"]["username"] = "someone-else"
    with pytest.raises(terminal.TerminalError, match="cannot safely"):
        terminal.open_device_terminal("PAGENT", cfg)
    assert env.read("jump_pw.txt") == ""


def test_port_conflict_retries_then_succeeds(env):
    env.mode("conflict2")
    terminal.open_device_terminal("PAGENT", _cfg())
    assert int(env.read("launches.txt")) == 3
    assert env.dev.creds == [(DEV_USER, DEV_PW)]


def test_port_conflict_gives_up_after_three(env):
    env.mode("conflict")
    with pytest.raises(terminal.TerminalError, match="port conflict"):
        terminal.open_device_terminal("PAGENT", _cfg())
    assert int(env.read("launches.txt")) == 3 and _sessions() == []


def test_foreign_listener_is_not_trusted(env, monkeypatch):
    attacker = socket.socket()
    attacker.bind(("127.0.0.1", 0))
    attacker.listen(5)
    received = []
    def _accept():
        try:
            received.append(attacker.accept())
        except OSError:
            pass

    threading.Thread(target=_accept, daemon=True).start()
    monkeypatch.setattr(tunnel_safety, "allocate_loopback_port", lambda: attacker.getsockname()[1])
    monkeypatch.setattr(terminal, "_TUNNEL_AUTH_TIMEOUT_SECONDS", 6)
    env.mode("nolisten")
    with pytest.raises(terminal.TerminalError, match="tunnel ownership could not be verified"):
        terminal.open_device_terminal("PAGENT", _cfg())
    assert received == [] and _sessions() == []
    attacker.close()


def test_ownership_change_before_credentials_blocks_login(env, monkeypatch):
    real, calls = tunnel_safety.run_lsof, {"n": 0}

    def flaky():
        out = real()
        if "p" in out and calls["n"] >= 0:
            calls["n"] += 1
        # from the second ownership proof on, the server side belongs to a stranger
        if calls["n"] >= 3:
            out = out.replace(f"\np{terminal._verified_pane_pid('network-lab-tunnel-PAGENT')}\n", "\np999999\n")
        return out

    monkeypatch.setattr(tunnel_safety, "run_lsof", flaky)
    with pytest.raises(terminal.TerminalError, match="tunnel ownership could not be verified"):
        terminal.open_device_terminal("PAGENT", _cfg())
    assert env.dev.creds == [] and _sessions() == []


def test_target_unreachable(env):
    env.dev.behavior = "close"
    with pytest.raises(terminal.TerminalError, match="unreachable|ended|ownership"):
        terminal.open_device_terminal("PAGENT", _cfg())
    assert env.dev.creds == [] and _sessions() == []


def test_tunnel_death_refuses_send_and_lists_exited(env):
    terminal.open_device_terminal("PAGENT", _cfg())
    terminal._run(["kill-session", "-t", "network-lab-tunnel-PAGENT"])
    # Either the tunnel check or (once telnet noticed the dropped TCP) the
    # dead device pane refuses the send; it is never delivered anywhere.
    with pytest.raises(terminal.TerminalError, match="tunnel ended|exited"):
        terminal.send_to_device("PAGENT", "show version", None, True)
    assert terminal.list_device_sessions()[0]["state"] == "exited"
    terminal.close_device_terminal("PAGENT")
    assert _sessions() == []


def test_telnet_exit_reaps_tunnel_and_refuses_send(env):
    terminal.open_device_terminal("PAGENT", _cfg())
    terminal.send_to_device("PAGENT", "quit", None, True)
    for _ in range(40):
        if terminal._pane_state("network-lab-device-PAGENT") == "exited":
            break
        time.sleep(0.25)
    assert terminal.list_device_sessions()[0]["state"] == "exited"
    assert not terminal._session_exists("network-lab-tunnel-PAGENT")
    assert "Connection closed" in terminal.read_device("PAGENT")["content"]
    with pytest.raises(terminal.TerminalError, match="exited"):
        terminal.send_to_device("PAGENT", "show version", None, True)
    # reconnect works and sends credentials again only because it is a new session
    terminal.open_device_terminal("PAGENT", _cfg())
    assert len(env.dev.creds) == 2


def test_reuse_after_restart_does_not_resend_credentials(env):
    terminal.open_device_terminal("PAGENT", _cfg())
    terminal._session_locks.clear()  # a fresh MCP process has no in-memory state
    again = terminal.open_device_terminal("PAGENT", _cfg())
    assert again["reused"] is True
    assert env.dev.creds == [(DEV_USER, DEV_PW)] and env.read("jump_pw.txt").split() == [JUMP_PW]
    assert int(env.read("launches.txt")) == 1


def test_orphan_tunnel_is_neither_reused_nor_deleted(env):
    terminal._ensure_tmux_environment()
    terminal._create_session("network-lab-tunnel-ORPH", ["cat"])
    with pytest.raises(terminal.TerminalError, match="orphaned tunnel"):
        terminal.open_device_terminal("ORPH", _cfg())
    assert terminal._session_exists("network-lab-tunnel-ORPH")
    with pytest.raises(terminal.TerminalError, match="cannot be identified"):
        terminal.close_device_terminal("ORPH")
    assert terminal._session_exists("network-lab-tunnel-ORPH")
    terminal._kill_session_if_exists("network-lab-tunnel-ORPH")


def test_two_devices_are_independent(env):
    env.dev2 = DeviceServer()
    results, errors = {}, []

    def run(name):
        try:
            results[name] = terminal.open_device_terminal(name, _cfg())
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    # second device shares the same fake ssh/device; separate tunnels and ports
    threads = [threading.Thread(target=run, args=(n,)) for n in ("PAGENT", "PAGENT2")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(results) == 2
    assert {s["device"] for s in terminal.list_device_sessions()} == {"PAGENT", "PAGENT2"}
    terminal.close_device_terminal("PAGENT")
    assert terminal._tunnel_alive("network-lab-tunnel-PAGENT2")
    terminal.send_to_device("PAGENT2", "show clock", None, True)
    time.sleep(1)
    assert "out:show clock" in terminal.read_device("PAGENT2")["content"]
    env.dev2.close()


def test_tunnel_session_unreachable_from_terminal_send_path(env):
    terminal.open_device_terminal("PAGENT", _cfg())
    # the public API only derives production names; a tunnel name is not a device
    with pytest.raises(terminal.TerminalError):
        terminal.send_to_device("tunnel-PAGENT", "x", None, True)
    assert "network-lab-tunnel-PAGENT" not in {s["session_name"] for s in terminal.list_device_sessions()}


# ---- Stage D: Discovery uses the same connection foundation ----


def test_discovery_login_over_tunnel_and_cleanup(env):
    from network_lab_mcp import discovery

    cfg = _cfg()
    hostname = discovery._login_ios_style("PAGENT", cfg)
    assert hostname == "PAGENT"
    assert env.dev.creds == [(DEV_USER, DEV_PW)] and env.read("jump_pw.txt").split() == [JUMP_PW]
    # Discovery namespace only: no production session was created
    assert terminal.list_device_sessions() == []
    assert terminal._session_exists("network-lab-discoverytunnel-PAGENT")
    terminal.send_to_bootstrap("PAGENT", "show lldp neighbors", None, True)
    time.sleep(1)
    assert "out:show lldp neighbors" in terminal.read_bootstrap("PAGENT")
    assert terminal.close_bootstrap_terminal("PAGENT") is True
    assert _sessions() == []


def test_discovery_refused_without_guard_and_creates_nothing(env, monkeypatch):
    monkeypatch.setattr(terminal, "TELNET_VIA_JUMP_ENABLED", False)
    with pytest.raises(terminal.TerminalError, match="not enabled"):
        terminal.open_bootstrap_terminal("PAGENT", _cfg())
    assert _sessions() == [] and env.read("ssh_argv.txt") == ""


def test_discovery_and_production_do_not_interfere(env):
    terminal.open_device_terminal("PAGENT", _cfg())
    terminal.open_bootstrap_terminal("PAGENT", _cfg())
    assert terminal._tunnel_alive("network-lab-tunnel-PAGENT")
    assert terminal._tunnel_alive("network-lab-discoverytunnel-PAGENT")
    terminal.close_bootstrap_terminal("PAGENT")
    assert terminal.list_device_sessions()[0]["state"] == "running"
    terminal.close_device_terminal("PAGENT")
    assert _sessions() == []
