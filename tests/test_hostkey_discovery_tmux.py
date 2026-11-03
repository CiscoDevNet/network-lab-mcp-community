# SPDX-License-Identifier: Apache-2.0
"""Discovery login through a real tmux pane + real OpenSSH (forced wrapper,
isolated loopback sshd running a fake router CLI). Covers: TOFU before login,
strict hops, CHANGED detection from the pane, no credential/auth before
trust, and that the pane's ssh really goes through the isolation wrapper."""

from __future__ import annotations

import getpass
import os
import re
from pathlib import Path

import pytest

from network_lab_mcp import discovery, hostkey_flow as hf, ssh_isolation as iso, terminal
from hostkey_support import Sshd, make_host_key, make_real_runtime, real_binaries_available

pytestmark = pytest.mark.skipif(not real_binaries_available(), reason="OpenSSH binaries unavailable")
USER = getpass.getuser()

ROUTER = """#!/bin/sh
printf 'RP/0/RP0/CPU0:R1#'
while read line; do printf '\\r\\nRP/0/RP0/CPU0:R1#'; done
"""


class NullChannel:
    def flush_input(self): pass
    def write(self, t): pass
    def readline(self, timeout): return None
    def close(self): pass


@pytest.fixture
def env(tmp_path):
    before = iso.snapshot_real_ssh_area()
    rt = make_real_runtime(tmp_path)
    router = Path(rt.sandbox) / "router.sh"
    router.write_text(ROUTER)
    router.chmod(0o700)
    servers = []

    def server(name, key, port=None):
        s = Sshd(rt, name, key, port=port)
        auth = Path(rt.sandbox) / f"authorized_{name}"
        auth.write_text(f'command="{router}" ' + Path(rt.identity + ".pub").read_text())
        os.chmod(auth, 0o600)
        s.start()
        servers.append(s)
        return s

    co = hf.Coordinator(NullChannel(), known_hosts=rt.product_known_hosts, user_known_hosts=rt.known_hosts,
                        global_known_hosts=rt.global_known_hosts, backup_dir=str(tmp_path / "bk"),
                        lock_dir=str(tmp_path / "locks"), emit=lambda t: None)
    yield rt, server, co
    discovery._ACTIVE_HOSTKEYS = None
    terminal.close_bootstrap_terminal("R1")
    for s in servers:
        s.stop()
    iso.uninstall_for_tests()
    assert iso.snapshot_real_ssh_area() == before


def cfg(port):
    return {"transport": "ssh", "address": "127.0.0.1", "port": port, "username": USER}


def trust(rt, key, port):
    with open(rt.known_hosts, "a") as fh:
        fh.write(f"[127.0.0.1]:{port} {key['type']} {key['blob']}\n")
    os.chmod(rt.known_hosts, 0o600)


def test_registered_host_logs_in_through_wrapper_pane(env):
    rt, server, co = env
    A = make_host_key(rt, "A")
    s = server("a", A)
    trust(rt, A, s.port)
    discovery._ACTIVE_HOSTKEYS = co
    assert discovery._login("R1", cfg(s.port)) == "R1"
    cmd = hf.build_ssh_command(cfg(s.port), files=co.strict_files())
    assert cmd[0] == rt.wrapper("ssh")
    log = Path(rt.sandbox, "sshd_a.log").read_text()
    assert "Accepted publickey" in log


def test_changed_key_is_detected_and_nothing_authenticates(env):
    rt, server, co = env
    A, B = make_host_key(rt, "A"), make_host_key(rt, "B")
    s = server("a", A)
    port = s.port
    trust(rt, A, port)
    s.stop()
    s2 = server("b", B, port=port)
    discovery._ACTIVE_HOSTKEYS = co
    with pytest.raises(hf.HostKeyChanged) as ei:
        discovery._login("R1", cfg(port))
    assert (ei.value.hop, ei.value.host, ei.value.port) == ("target", "127.0.0.1", port)
    assert "Accepted" not in Path(rt.sandbox, "sshd_b.log").read_text()


def test_first_use_is_registered_by_prepare_then_discovery_logs_in_strictly(env):
    rt, server, co = env
    A = make_host_key(rt, "A")
    s = server("a", A)
    co.prepare({"R1": cfg(s.port)})  # TOFU: probe + persist before the credential-carrying pane starts
    assert Path(rt.product_known_hosts).read_text() == f"[127.0.0.1]:{s.port} {A['type']} {A['blob']}\n"
    discovery._ACTIVE_HOSTKEYS = co
    assert discovery._login("R1", cfg(s.port)) == "R1"  # probe output cannot disturb the login prompt detection
    log = Path(rt.sandbox, "sshd_a.log").read_text()
    assert log.index("Invalid user nlm-hostkey-probe") < log.index("Accepted publickey")  # key first, auth later
    assert not Path(rt.known_hosts).exists()


def test_unregistered_key_is_not_auto_accepted_by_the_login_connection_itself(env):
    rt, server, co = env
    A = make_host_key(rt, "A")
    s = server("a", A)
    discovery._ACTIVE_HOSTKEYS = co
    with pytest.raises(discovery.DiscoveryError, match="host key verification failed"):
        discovery._login("R1", cfg(s.port))
    assert not Path(rt.known_hosts).exists() and not Path(rt.product_known_hosts).exists()
    assert "Accepted" not in Path(rt.sandbox, "sshd_a.log").read_text()


def test_pane_ssh_with_unsafe_known_hosts_is_refused_by_wrapper(env):
    """If the product tried to point ssh at a file outside the sandbox, the
    wrapper in the pane refuses before any OpenSSH binary starts."""
    rt, server, co = env
    A = make_host_key(rt, "A")
    s = server("a", A)
    bad = hf.build_ssh_command(cfg(s.port), files=["/etc/hosts"])
    terminal.open_bootstrap_terminal("R1", cfg(s.port), ssh_command=bad)
    import time
    deadline = time.monotonic() + 8
    text = ""
    while time.monotonic() < deadline:
        try:
            text = terminal.read_bootstrap("R1")
        except terminal.TerminalError:
            break
        if "refused" in text:
            break
        time.sleep(0.2)
    assert "ssh_isolation: refused" in text
    assert "Accepted" not in Path(rt.sandbox, "sshd_a.log").read_text()


def test_unexpected_ssh_executable_is_rejected_by_terminal(env):
    rt, server, co = env
    with pytest.raises(terminal.TerminalError):
        terminal.open_bootstrap_terminal("R1", cfg(1), ssh_command=["/bin/echo", "hi"])


# ---- non-interactive Discovery (no coordinator): strict, never accept-new ----


def test_noninteractive_unknown_key_is_not_registered_and_nothing_authenticates(env):
    rt, server, co = env
    A = make_host_key(rt, "A")
    s = server("a", A)
    discovery._ACTIVE_HOSTKEYS = None
    cfgd = dict(cfg(s.port), password="SENTINEL_TARGET_PW")
    with pytest.raises(discovery.DiscoveryError, match="not registered"):
        discovery._login("R1", cfgd)
    assert not Path(rt.known_hosts).exists()
    assert "Accepted" not in Path(rt.sandbox, "sshd_a.log").read_text()
    assert "SENTINEL_TARGET_PW" not in terminal.read_bootstrap("R1")


def test_noninteractive_changed_key_is_not_replaced(env):
    rt, server, co = env
    A, B = make_host_key(rt, "A"), make_host_key(rt, "B")
    s = server("a", A)
    port = s.port
    trust(rt, A, port)
    s.stop()
    server("b", B, port=port)
    before = Path(rt.known_hosts).read_bytes()
    discovery._ACTIVE_HOSTKEYS = None
    with pytest.raises(hf.HostKeyChanged):
        discovery._login("R1", cfg(port))
    assert Path(rt.known_hosts).read_bytes() == before
    assert not list((Path(rt.sandbox)).glob("known_hosts.*"))


def test_noninteractive_trusted_host_still_logs_in(env):
    rt, server, co = env
    A = make_host_key(rt, "A")
    s = server("a", A)
    trust(rt, A, s.port)
    discovery._ACTIVE_HOSTKEYS = None
    assert discovery._login("R1", cfg(s.port)) == "R1"


def test_credentials_are_not_sent_when_hop_untrusted_with_coordinator(env, monkeypatch):
    rt, server, co = env
    A = make_host_key(rt, "A")
    s = server("a", A)
    trust(rt, A, s.port)
    sent = []
    monkeypatch.setattr(terminal, "send_secret_to_bootstrap", lambda *a, **k: sent.append(a))
    co.device_hops["R1"] = hf.hops_for(cfg(s.port))  # hop state PENDING: never prepared/verified
    discovery._ACTIVE_HOSTKEYS = co
    # key-auth login would not send a secret anyway; exercise the guard directly
    with pytest.raises(discovery.DiscoveryError, match="not trusted"):
        discovery._require_trusted_hops("R1")
    assert sent == []
    co.device_hops["R1"][0].host_state = hf.HostKeyState.TRUSTED
    discovery._require_trusted_hops("R1")


def test_accept_new_exists_only_in_the_authentication_free_probe():
    src = Path(terminal.__file__).parent
    for path in src.rglob("*.py"):
        hits = [ln for ln in path.read_text().splitlines() if "accept-new" in ln]
        if path.name == "hostkey_tofu.py":
            assert [ln.strip() for ln in hits if ln.lstrip().startswith('"-o", "StrictHostKeyChecking=')] \
                == ['"-o", "StrictHostKeyChecking=accept-new", "-o", known_hosts_option([tmp_kh]),']  # the probe, nothing else
        elif path.name != "ssh_isolation.py":  # the wrapper's allow-list names the value it permits
            assert all(ln.lstrip().startswith(("#", '"', "`")) or "never" in ln or "not" in ln for ln in hits), (path.name, hits)
