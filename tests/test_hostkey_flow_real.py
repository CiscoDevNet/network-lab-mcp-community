# SPDX-License-Identifier: Apache-2.0
"""Real OpenSSH (isolated sandbox, loopback sshd, forced wrapper) tests of
host-key registration, strictness on every hop, and CHANGED recovery.
No real device, jump server, known_hosts or ssh config is involved."""

from __future__ import annotations

import getpass
import os
from pathlib import Path

import pytest

from network_lab_mcp import hostkey_flow as hf
from network_lab_mcp import hostkey_store as hs
from network_lab_mcp import ssh_isolation as iso
from hostkey_support import Sshd, make_host_key, make_real_runtime, real_binaries_available, run_ssh_pty

pytestmark = pytest.mark.skipif(not real_binaries_available(), reason="OpenSSH binaries unavailable")
USER = getpass.getuser()


class Scripted:
    def __init__(self, lines):
        self.lines, self.out = list(lines), []

    def flush_input(self):
        pass

    def write(self, t):
        self.out.append(t)

    def readline(self, timeout):
        return self.lines.pop(0) if self.lines else None

    @property
    def text(self):
        return "".join(self.out)


@pytest.fixture
def env(tmp_path):
    before = iso.snapshot_real_ssh_area()
    rt = make_real_runtime(tmp_path)
    servers = []

    def server(name, key, port=None):
        s = Sshd(rt, name, key, port=port).start()
        servers.append(s)
        return s

    yield rt, server
    for s in servers:
        s.stop()
    assert all(s.stopped() for s in servers)
    iso.uninstall_for_tests()
    assert iso.snapshot_real_ssh_area() == before


def direct_cfg(port):
    return {"transport": "ssh", "address": "127.0.0.1", "port": port, "username": USER}


def jump_cfg(tport, jport):
    cfg = direct_cfg(tport)
    cfg["jump_host_config"] = {"address": "127.0.0.1", "port": jport, "username": USER}
    return cfg


def coordinator(rt, tmp_path, lines):
    ch = Scripted(lines)
    return hf.Coordinator(ch, known_hosts=rt.known_hosts, global_known_hosts=rt.global_known_hosts,
                          backup_dir=str(tmp_path / "backups"), lock_dir=str(tmp_path / "locks")), ch


def approve(key, label="ED25519"):
    return [label, key["fingerprint"], "3", "yes"]


def test_unknown_direct_registers_approved_key_only(env, tmp_path):
    rt, server = env
    A = make_host_key(rt, "A")
    s = server("a", A)
    co, ch = coordinator(rt, tmp_path, approve(A))
    co.prepare({"Router1": direct_cfg(s.port)})
    st = hs.classify("127.0.0.1", s.port, rt.known_hosts)
    assert st.state == hs.REGISTERED_UNVERIFIED and st.keys[0].fingerprint == A["fingerprint"]
    # the approval is spent
    assert all(a.consumed for a in co.registry._items.values())
    # the credential-carrying connection is strict and works (key auth in the sandbox)
    cmd = hf.build_ssh_command(direct_cfg(s.port), kh=rt.known_hosts)
    out, rc = run_ssh_pty(rt, cmd[1 + len(iso.required_ssh_args(rt)):] + ["echo", "HELLO_OK"])
    assert "HELLO_OK" in out and rc == 0


def test_unknown_with_wrong_fingerprint_registers_nothing(env, tmp_path):
    rt, server = env
    A, B = make_host_key(rt, "A"), make_host_key(rt, "B")
    s = server("a", A)
    co, ch = coordinator(rt, tmp_path, approve(B))  # human expects B, server presents A
    with pytest.raises(hf.HostKeyError):
        co.prepare({"Router1": direct_cfg(s.port)})
    assert hs.classify("127.0.0.1", s.port, rt.known_hosts).state == hs.UNKNOWN_CANDIDATE


def test_unknown_with_wrong_key_type_registers_nothing(env, tmp_path):
    rt, server = env
    A = make_host_key(rt, "A")
    s = server("a", A)
    co, ch = coordinator(rt, tmp_path, approve(A, "RSA"))
    with pytest.raises(hf.HostKeyError):
        co.prepare({"Router1": direct_cfg(s.port)})
    assert hs.classify("127.0.0.1", s.port, rt.known_hosts).state == hs.UNKNOWN_CANDIDATE


def test_cancel_registers_nothing(env, tmp_path):
    rt, server = env
    A = make_host_key(rt, "A")
    s = server("a", A)
    co, ch = coordinator(rt, tmp_path, [""])
    with pytest.raises(hf.HostKeyError):
        co.prepare({"Router1": direct_cfg(s.port)})
    assert not Path(rt.known_hosts).exists()


def test_trusted_host_is_not_prompted(env, tmp_path):
    rt, server = env
    A = make_host_key(rt, "A")
    s = server("a", A)
    Path(rt.known_hosts).write_text(f"[127.0.0.1]:{s.port} {A['type']} {A['blob']}\n")
    os.chmod(rt.known_hosts, 0o600)
    co, ch = coordinator(rt, tmp_path, [])
    co.prepare({"Router1": direct_cfg(s.port)})
    assert ch.text == ""


def test_proxyjump_both_hops_strict_and_jump_registered_first(env, tmp_path):
    rt, server = env
    J, T = make_host_key(rt, "J"), make_host_key(rt, "T")
    sj, st = server("j", J), server("t", T)
    cfg = jump_cfg(st.port, sj.port)
    co, ch = coordinator(rt, tmp_path, approve(J) + approve(T))
    co.prepare({"Router1": cfg})
    assert hs.classify("127.0.0.1", sj.port, rt.known_hosts).state == hs.REGISTERED_UNVERIFIED
    assert hs.classify("127.0.0.1", st.port, rt.known_hosts).state == hs.REGISTERED_UNVERIFIED
    pre = 1 + len(iso.required_ssh_args(rt))
    cmd = hf.build_ssh_command(cfg, kh=rt.known_hosts)
    out, rc = run_ssh_pty(rt, cmd[pre:] + ["echo", "VIA_JUMP"])
    assert "VIA_JUMP" in out and rc == 0


def test_proxyjump_unknown_jump_is_strict_even_with_strict_option_only_on_target(env, tmp_path):
    """Regression for the observed OpenSSH behaviour: with -J the jump hop does
    NOT inherit command-line -o StrictHostKeyChecking=yes. The per-hop
    ProxyCommand form must refuse an unknown jump key."""
    rt, server = env
    J, T = make_host_key(rt, "J"), make_host_key(rt, "T")
    sj, st = server("j", J), server("t", T)
    Path(rt.known_hosts).write_text(f"[127.0.0.1]:{st.port} {T['type']} {T['blob']}\n")
    os.chmod(rt.known_hosts, 0o600)
    cmd = hf.build_ssh_command(jump_cfg(st.port, sj.port), kh=rt.known_hosts)
    pre = 1 + len(iso.required_ssh_args(rt))
    out, rc = run_ssh_pty(rt, cmd[pre:] + ["echo", "SHOULD_NOT_RUN"], timeout=15)
    assert "SHOULD_NOT_RUN" not in out and rc != 0
    assert "authenticity of host" not in out  # no interactive prompt at all
    assert hs.classify("127.0.0.1", sj.port, rt.known_hosts).state == hs.UNKNOWN_CANDIDATE


def test_target_unknown_registration_sends_nothing_to_target_and_needs_trusted_jump(env, tmp_path):
    rt, server = env
    J, T = make_host_key(rt, "J"), make_host_key(rt, "T")
    sj, st = server("j", J), server("t", T)
    # jump NOT registered: registering the target through it must fail closed
    res = hf.run_registration_connection("Router1", jump_cfg(st.port, sj.port), "target", rt.known_hosts,
                                         ("ED25519", T["fingerprint"]))
    assert not res.ok
    assert hs.classify("127.0.0.1", st.port, rt.known_hosts).state == hs.UNKNOWN_CANDIDATE
    log = Path(rt.sandbox, "sshd_t.log").read_text()
    assert "Accepted publickey" not in log  # target never authenticated


def test_changed_key_detected_then_recovered_with_backup(env, tmp_path):
    rt, server = env
    A, B = make_host_key(rt, "A"), make_host_key(rt, "B")
    other = make_host_key(rt, "O")
    s = server("a", A)
    port = s.port
    Path(rt.known_hosts).write_text(
        f"[127.0.0.1]:{port} {A['type']} {A['blob']}\n[192.0.2.9]:22 {other['type']} {other['blob']}\n")
    os.chmod(rt.known_hosts, 0o600)
    s.stop()
    s2 = server("b", B, port=port)
    cfg = direct_cfg(port)
    pre = 1 + len(iso.required_ssh_args(rt))
    out, rc = run_ssh_pty(rt, hf.build_ssh_command(cfg, kh=rt.known_hosts)[pre:] + ["echo", "NO"], timeout=15)
    assert "NO" not in out.replace("NOTHING", "") or rc != 0
    assert hf.detect_changed(out) == ("127.0.0.1", port)
    before = Path(rt.known_hosts).read_text()
    co, ch = coordinator(rt, tmp_path, ["3"] + approve(B) + ["yes"])
    ok = co.recover_changed(hf.HostKeyChanged("Router1", "target", "127.0.0.1", port), cfg)
    assert ok is True, ch.text
    st = hs.classify("127.0.0.1", port, rt.known_hosts)
    assert [k.fingerprint for k in st.keys] == [B["fingerprint"]]
    assert f"[192.0.2.9]:22 {other['type']} {other['blob']}" in Path(rt.known_hosts).read_text()
    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 1 and backups[0].read_text() == before
    out, rc = run_ssh_pty(rt, hf.build_ssh_command(cfg, kh=rt.known_hosts)[pre:] + ["echo", "RECOVERED"])
    assert "RECOVERED" in out and rc == 0


def test_changed_recovery_wrong_expected_key_leaves_known_hosts_untouched(env, tmp_path):
    rt, server = env
    A, B, C = make_host_key(rt, "A"), make_host_key(rt, "B"), make_host_key(rt, "C")
    s = server("a", A)
    port = s.port
    Path(rt.known_hosts).write_text(f"[127.0.0.1]:{port} {A['type']} {A['blob']}\n")
    os.chmod(rt.known_hosts, 0o600)
    s.stop()
    server("b", B, port=port)
    before = Path(rt.known_hosts).read_bytes()
    co, ch = coordinator(rt, tmp_path, ["3"] + approve(C) + ["yes"])  # human expects C, device presents B
    assert co.recover_changed(hf.HostKeyChanged("Router1", "target", "127.0.0.1", port), direct_cfg(port)) is False
    assert Path(rt.known_hosts).read_bytes() == before
    assert not (tmp_path / "backups").exists()


def test_changed_recovery_declined_final_confirmation_changes_nothing(env, tmp_path):
    rt, server = env
    A, B = make_host_key(rt, "A"), make_host_key(rt, "B")
    s = server("a", A)
    port = s.port
    Path(rt.known_hosts).write_text(f"[127.0.0.1]:{port} {A['type']} {A['blob']}\n")
    os.chmod(rt.known_hosts, 0o600)
    s.stop()
    server("b", B, port=port)
    before = Path(rt.known_hosts).read_bytes()
    co, ch = coordinator(rt, tmp_path, ["3"] + approve(B) + ["no"])
    assert co.recover_changed(hf.HostKeyChanged("Router1", "target", "127.0.0.1", port), direct_cfg(port)) is False
    assert Path(rt.known_hosts).read_bytes() == before


def test_key_swapped_between_approval_and_registration_is_refused(env, tmp_path):
    """Approval of A, but the device presents B when the registration
    connection happens: nothing is registered."""
    rt, server = env
    A, B = make_host_key(rt, "A"), make_host_key(rt, "B")
    s = server("a", A)
    port = s.port
    s.stop()
    server("b", B, port=port)
    co, ch = coordinator(rt, tmp_path, approve(A))
    with pytest.raises(hf.HostKeyError):
        co.prepare({"Router1": direct_cfg(port)})
    assert not Path(rt.known_hosts).exists() or hs.classify("127.0.0.1", port, rt.known_hosts).state == hs.UNKNOWN_CANDIDATE


def test_ecdsa_and_rsa_registration_by_type(env, tmp_path):
    rt, server = env
    E = make_host_key(rt, "E", "ecdsa")
    R = make_host_key(rt, "R", "rsa", bits="2048")
    for name, key, label in (("e", E, "ECDSA"), ("r", R, "RSA")):
        s = server(name, key)
        co, ch = coordinator(rt, tmp_path, approve(key, label))
        co.prepare({"R": direct_cfg(s.port)})
        st = hs.classify("127.0.0.1", s.port, rt.known_hosts)
        assert st.keys[0].fingerprint == key["fingerprint"]


def test_prompt_parser_variants():
    txt = ("The authenticity of host '[127.0.0.1]:52501 (<no hostip for proxy command>)' can't be established.\n"
           "ED25519 key fingerprint is: SHA256:" + "A" * 43 + "\nThis key is not known by any other names.\n"
           "Are you sure you want to continue connecting (yes/no/[fingerprint])? ")
    p = hf.parse_host_key_prompt(txt)
    assert (p.host, p.port, p.key_label, p.supports_fingerprint_answer) == ("127.0.0.1", 52501, "ED25519", True)
    old = txt.replace("is: SHA", "is SHA").replace("/[fingerprint]", "")
    assert hf.parse_host_key_prompt(old).supports_fingerprint_answer is False
    assert hf.parse_host_key_prompt("random output") is None


def test_key_swapped_after_probe_before_real_registration_is_refused(env, tmp_path, monkeypatch):
    rt, server = env
    A, B, C = make_host_key(rt, "A"), make_host_key(rt, "B"), make_host_key(rt, "C")
    s = server("a", A)
    port = s.port
    Path(rt.known_hosts).write_text(f"[127.0.0.1]:{port} {A['type']} {A['blob']}\n")
    os.chmod(rt.known_hosts, 0o600)
    s.stop()
    sb = server("b", B, port=port)
    original = hf.run_registration_connection
    calls = []

    def swapping(*a, **kw):
        calls.append(1)
        if len(calls) == 2:  # the real-file registration after the probe: device now presents C
            sb.stop()
            server("c", C, port=port)
        return original(*a, **kw)

    monkeypatch.setattr(hf, "run_registration_connection", swapping)
    co, ch = coordinator(rt, tmp_path, ["3"] + approve(B) + ["yes"])
    with pytest.raises(hf.HostKeyError):
        co.recover_changed(hf.HostKeyChanged("Router1", "target", "127.0.0.1", port), direct_cfg(port))
    st = hs.classify("127.0.0.1", port, rt.known_hosts)
    assert all(k.fingerprint != C["fingerprint"] for k in st.keys)
    assert len(list((tmp_path / "backups").iterdir())) == 1  # old entry is recoverable


def test_telnet_over_tunnel_with_unknown_jump_key_is_disabled_fail_closed(env, tmp_path):
    rt, server = env
    J = make_host_key(rt, "J")
    s = server("j", J)
    cfg = {"transport": "telnet", "address": "192.0.2.30", "port": 23,
           "jump_host_config": {"address": "127.0.0.1", "port": s.port, "username": USER}}
    co, ch = coordinator(rt, tmp_path, [])
    with pytest.raises(hf.HostKeyError, match="not supported for Telnet"):
        co.prepare({"LegacyRouter": cfg})
    assert not Path(rt.known_hosts).exists()


def test_undetermined_known_hosts_fails_closed(env, tmp_path):
    rt, server = env
    A = make_host_key(rt, "A")
    s = server("a", A)
    Path(rt.known_hosts).write_text("x")
    os.chmod(rt.known_hosts, 0)
    try:
        co, ch = coordinator(rt, tmp_path, approve(A))
        with pytest.raises(hf.HostKeyError, match="cannot be determined"):
            co.prepare({"Router1": direct_cfg(s.port)})
    finally:
        os.chmod(rt.known_hosts, 0o600)
    assert ch.text == ""  # the human was never prompted
