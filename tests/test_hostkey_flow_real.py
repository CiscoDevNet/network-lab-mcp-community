# SPDX-License-Identifier: Apache-2.0
"""Real OpenSSH (isolated sandbox, loopback sshd, forced wrapper) tests of lab
TOFU: authentication-free probe, persistence before authentication, strict
connection on every hop, refusal of changed/other-type keys, and the human-only
replacement flow. No real device, jump server, known_hosts or ssh config is
involved; the real user's ~/.ssh is only stat()ed before and after."""

from __future__ import annotations

import getpass
import json
import os
import re
import socket
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from network_lab_mcp import hostkey_flow as hf
from network_lab_mcp import hostkey_store as hs
from network_lab_mcp import hostkey_tofu as tofu
from network_lab_mcp import ssh_isolation as iso
from hostkey_support import Sshd, make_host_key, make_real_runtime, real_binaries_available, run_ssh_pty

pytestmark = pytest.mark.skipif(not real_binaries_available(), reason="OpenSSH binaries unavailable")
USER = getpass.getuser()
PROBE_LINE = "Invalid user nlm-hostkey-probe"  # what the sandbox sshd logs for one probe connection


class Scripted:
    def __init__(self, lines):
        self.lines, self.out = list(lines), []

    def flush_input(self):
        pass

    def write(self, t):
        self.out.append(t)

    def readline(self, timeout):
        return self.lines.pop(0) if self.lines else None

    def close(self):
        pass

    @property
    def text(self):
        return "".join(self.out)


@pytest.fixture
def env(tmp_path):
    before = iso.snapshot_real_ssh_area()
    rt = make_real_runtime(tmp_path)
    servers = []

    def server(name, key, port=None, algorithms=None, extra_keys=()):
        s = Sshd(rt, name, key, port=port, algorithms=algorithms)
        if extra_keys:
            s.cfg.write_text(s.cfg.read_text() + "".join(f"HostKey {k['path']}\n" for k in extra_keys))
        s.start()
        servers.append(s)
        return s

    yield rt, server
    for s in servers:
        s.stop()
    assert all(s.stopped() for s in servers)
    assert [p for p in Path(rt.tmp_dir).iterdir() if p.name.startswith("nlm-probe-")] == []  # probe leftovers
    iso.uninstall_for_tests()
    assert iso.snapshot_real_ssh_area() == before  # the real user's ~/.ssh was never touched


def direct_cfg(port, user=USER):
    return {"transport": "ssh", "address": "127.0.0.1", "port": port, "username": user}


def jump_cfg(tport, jport):
    cfg = direct_cfg(tport)
    cfg["jump_host_config"] = {"address": "127.0.0.1", "port": jport, "username": USER}
    return cfg


def coordinator(rt, tmp_path, lines=None):
    shown = []
    ch = Scripted(lines) if lines is not None else None
    co = hf.Coordinator(ch, known_hosts=rt.product_known_hosts, user_known_hosts=rt.known_hosts,
                        global_known_hosts=rt.global_known_hosts, backup_dir=str(Path(rt.sandbox) / "backups"),
                        lock_dir=str(tmp_path / "locks"), emit=shown.append)
    co.shown = shown
    return co, ch


def put(path, text, mode=0o600):
    Path(path).write_text(text)
    os.chmod(path, mode)


def entry(port, key, host="127.0.0.1"):
    return f"[{host}]:{port} {key['type']} {key['blob']}\n"


def connect(rt, cfg, co=None, remote="echo CONNECTED"):
    cmd = hf.build_ssh_command(cfg, files=co.strict_files() if co else None)
    out, rc = run_ssh_pty(rt, cmd[1 + len(iso.required_ssh_args(rt)):] + remote.split(), timeout=20)
    return out, rc


def sshd_log(rt, name):
    return Path(rt.sandbox, f"sshd_{name}.log").read_text()


def registered(rt, port, files=None):
    return tofu.classify("127.0.0.1", port, files or [rt.product_known_hosts, rt.known_hosts], rt.global_known_hosts)


# ---- Group A/B: first use ----------------------------------------------------------------


@pytest.mark.parametrize("kind,bits", [("ed25519", None), ("ecdsa", None), ("rsa", "2048")])
def test_first_use_registers_the_negotiated_key_in_the_product_file_only(env, tmp_path, kind, bits):
    rt, server = env
    K = make_host_key(rt, "K", kind, bits)
    s = server("a", K)
    co, _ = coordinator(rt, tmp_path)
    co.prepare({"Router1": direct_cfg(s.port)})
    st = registered(rt, s.port, [rt.product_known_hosts])
    assert st.state == hs.REGISTERED_UNVERIFIED and [k.fingerprint for k in st.keys] == [K["fingerprint"]]
    assert Path(rt.product_known_hosts).read_text() == entry(s.port, K)  # exactly one line, nothing else
    assert stat.S_IMODE(os.stat(rt.product_known_hosts).st_mode) == 0o600
    assert not Path(rt.known_hosts).exists()  # the user's file is never created or edited
    assert co.shown == [f"SSH host key automatically accepted (TOFU):\nRouter1 (direct) {tofu.key_label(K['type'])} {K['fingerprint']}"]
    log = [json.loads(ln) for ln in Path(rt.sandbox, "tofu.log").read_text().splitlines()]
    assert log[-1]["result"] == "registered" and log[-1]["fingerprint"] == K["fingerprint"] and log[-1]["hop"] == "direct"
    assert "password" not in json.dumps(log).lower()
    out, rc = connect(rt, direct_cfg(s.port), co)  # strict connection with the registered key
    assert "CONNECTED" in out and rc == 0


def test_probe_sends_only_the_dummy_user_and_no_credential(env, tmp_path):
    rt, server = env
    K = make_host_key(rt, "K")
    s = server("a", K)
    s.stop()
    s.cfg.write_text(s.cfg.read_text().replace("LogLevel VERBOSE", "LogLevel DEBUG3").replace(
        "KbdInteractiveAuthentication no", "KbdInteractiveAuthentication yes"))
    s.start()
    tofu.register_unknown("Router1", direct_cfg(s.port), tofu.hops_for(direct_cfg(s.port))[0],
                          product_kh=rt.product_known_hosts, user_kh=rt.known_hosts, global_kh=rt.global_known_hosts,
                          lock_dir=str(tmp_path / "lk"))
    s.stop()
    log = sshd_log(rt, "a")
    methods = re.findall(r"userauth-request for user (\S+) service ssh-connection method (\S+)", log)
    assert methods == [("nlm-hostkey-probe", "none")]  # one none request, no publickey/password/keyboard-interactive
    assert USER not in " ".join(m[0] for m in methods)
    assert "Accepted" not in log and "session opened" not in log and "Starting session" not in log
    assert 'methods="publickey,keyboard-interactive"' in log  # the server did offer more; none was tried


def test_registered_host_is_never_probed_again(env, tmp_path):
    rt, server = env
    K = make_host_key(rt, "K")
    s = server("a", K)
    co, _ = coordinator(rt, tmp_path)
    co.prepare({"Router1": direct_cfg(s.port)})
    assert sshd_log(rt, "a").count(PROBE_LINE) == 1
    co2, _ = coordinator(rt, tmp_path)
    co2.prepare({"Router1": direct_cfg(s.port)})
    assert sshd_log(rt, "a").count(PROBE_LINE) == 1 and co2.shown == []


def test_one_probe_per_unknown_host_even_with_shared_address(env, tmp_path):
    rt, server = env
    K = make_host_key(rt, "K")
    s = server("a", K)
    co, _ = coordinator(rt, tmp_path)
    co.prepare({"R1": direct_cfg(s.port), "R2": direct_cfg(s.port)})
    assert sshd_log(rt, "a").count(PROBE_LINE) == 1


def test_probe_failure_is_not_retried_and_registers_nothing(env, tmp_path):
    rt, server = env
    port = socket.socket()
    port.bind(("127.0.0.1", 0))
    free = port.getsockname()[1]
    port.close()  # nothing listens: connection refused
    co, _ = coordinator(rt, tmp_path)
    with pytest.raises(hf.HostKeyError, match="TOFU_PROBE_ERROR.*refused"):
        co.prepare({"Router1": direct_cfg(free)})
    assert not Path(rt.product_known_hosts).exists() or Path(rt.product_known_hosts).read_text() == ""
    log = Path(rt.sandbox, "tofu.log").read_text()
    assert log.count("probe_failed") == 1  # one attempt, no automatic retry


def test_hanging_server_hits_the_probe_deadline_and_cleans_up(env, tmp_path):
    rt, _ = env
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    held = []
    threading.Thread(target=lambda: held.append(srv.accept()[0]), daemon=True).start()  # accepts, never speaks
    try:
        with pytest.raises(hf.HostKeyError, match="TOFU_PROBE_ERROR"):
            tofu.probe_host_key("Router1", direct_cfg(srv.getsockname()[1]), "target", timeout=2)
    finally:
        srv.close()
        for c in held:
            c.close()


# ---- registered keys are never extended, replaced or removed automatically -----------------


def test_changed_key_of_same_type_is_refused_and_nothing_changes(env, tmp_path):
    rt, server = env
    A, B = make_host_key(rt, "A"), make_host_key(rt, "B")
    s = server("a", A)
    port = s.port
    put(rt.product_known_hosts, entry(port, A))
    s.stop()
    server("b", B, port=port)
    before = Path(rt.product_known_hosts).read_bytes()
    co, _ = coordinator(rt, tmp_path)
    co.prepare({"Router1": direct_cfg(port)})  # registered: no probe, no TOFU
    out, rc = connect(rt, direct_cfg(port), co, "echo SHOULD_NOT_RUN")
    assert "SHOULD_NOT_RUN" not in out and rc != 0 and hf.detect_changed(out) == ("127.0.0.1", port)
    assert Path(rt.product_known_hosts).read_bytes() == before
    assert sshd_log(rt, "b").count(PROBE_LINE) == 0
    assert "Accepted publickey" not in sshd_log(rt, "b")  # authentication never started


def test_other_key_type_than_registered_is_refused_both_directions(env, tmp_path):
    rt, server = env
    ED, RSA = make_host_key(rt, "ED"), make_host_key(rt, "RSA", "rsa", "2048")
    for reg, offered, name in ((RSA, ED, "x"), (ED, RSA, "y")):
        s = server(name, offered)
        put(rt.product_known_hosts, entry(s.port, reg))
        before = Path(rt.product_known_hosts).read_bytes()
        co, _ = coordinator(rt, tmp_path)
        co.prepare({"R": direct_cfg(s.port)})
        out, rc = connect(rt, direct_cfg(s.port), co, "echo SHOULD_NOT_RUN")
        assert "SHOULD_NOT_RUN" not in out and rc != 0
        assert Path(rt.product_known_hosts).read_bytes() == before  # no second key type was added


def test_server_offering_several_types_connects_with_the_registered_one_and_adds_nothing(env, tmp_path):
    rt, server = env
    ED, RSA = make_host_key(rt, "ED"), make_host_key(rt, "RSA", "rsa", "2048")
    s = server("a", ED, extra_keys=[RSA])
    put(rt.product_known_hosts, entry(s.port, RSA))  # registered: RSA, though ED25519 is the server's first choice
    out, rc = connect(rt, direct_cfg(s.port))
    assert "CONNECTED" in out and rc == 0
    assert Path(rt.product_known_hosts).read_text() == entry(s.port, RSA)  # UpdateHostKeys=no: no addition


def test_user_known_hosts_is_consulted_but_never_modified_or_probed(env, tmp_path):
    rt, server = env
    K = make_host_key(rt, "K")
    s = server("a", K)
    put(rt.known_hosts, entry(s.port, K))
    before = Path(rt.known_hosts).read_bytes()
    co, _ = coordinator(rt, tmp_path)
    co.prepare({"Router1": direct_cfg(s.port)})
    assert sshd_log(rt, "a").count(PROBE_LINE) == 0 and not Path(rt.product_known_hosts).exists()
    out, rc = connect(rt, direct_cfg(s.port), co)
    assert "CONNECTED" in out and rc == 0 and Path(rt.known_hosts).read_bytes() == before


def test_hashed_user_entry_counts_as_registered(env, tmp_path):
    rt, server = env
    K = make_host_key(rt, "K")
    s = server("a", K)
    put(rt.known_hosts, entry(s.port, K))
    from hostkey_support import keygen
    keygen(rt, "-H", "-f", rt.known_hosts)
    assert Path(rt.known_hosts).read_text().startswith("|1|")
    co, _ = coordinator(rt, tmp_path)
    co.prepare({"Router1": direct_cfg(s.port)})
    assert sshd_log(rt, "a").count(PROBE_LINE) == 0
    out, rc = connect(rt, direct_cfg(s.port), co)
    assert "CONNECTED" in out and rc == 0


def test_global_known_hosts_entry_counts_as_registered(env, tmp_path):
    rt, server = env
    K = make_host_key(rt, "K")
    s = server("a", K)
    put(rt.global_known_hosts, entry(s.port, K))
    co, _ = coordinator(rt, tmp_path)
    co.prepare({"Router1": direct_cfg(s.port)})
    assert sshd_log(rt, "a").count(PROBE_LINE) == 0 and not Path(rt.product_known_hosts).exists()


def test_other_entries_of_the_product_file_are_preserved_on_first_use(env, tmp_path):
    rt, server = env
    K, other = make_host_key(rt, "K"), make_host_key(rt, "O")
    s = server("a", K)
    keep = f"[192.0.2.9]:22 {other['type']} {other['blob']}\n"
    put(rt.product_known_hosts, keep.rstrip("\n"))  # even without a trailing newline
    co, _ = coordinator(rt, tmp_path)
    co.prepare({"Router1": direct_cfg(s.port)})
    assert Path(rt.product_known_hosts).read_text() == keep + entry(s.port, K)


# ---- storage failures stop before any connection ---------------------------------------------


def test_unwritable_product_directory_stops_before_authentication(env, tmp_path):
    rt, server = env
    K = make_host_key(rt, "K")
    s = server("a", K)
    d = Path(tmp_path / "ro")
    d.mkdir(mode=0o700)
    co, _ = coordinator(rt, tmp_path)
    co.known_hosts = str(Path(rt.sandbox) / "ro" / "known_hosts")
    os.makedirs(Path(rt.sandbox) / "ro", mode=0o700)
    os.chmod(Path(rt.sandbox) / "ro", 0o500)
    try:
        with pytest.raises(hf.HostKeyError, match="TOFU_STORAGE_ERROR"):
            co.prepare({"Router1": direct_cfg(s.port)})
    finally:
        os.chmod(Path(rt.sandbox) / "ro", 0o700)
    assert sshd_log(rt, "a").count(PROBE_LINE) == 0 and "Accepted" not in sshd_log(rt, "a")


@pytest.mark.parametrize("how", ["readonly", "symlink", "directory", "world_writable"])
def test_unsafe_product_file_is_refused(env, tmp_path, how):
    rt, server = env
    K = make_host_key(rt, "K")
    s = server("a", K)
    p = Path(rt.product_known_hosts)
    if how == "readonly":
        put(p, "", 0o400)
    elif how == "symlink":
        target = Path(rt.sandbox) / "elsewhere"
        put(target, "")
        p.symlink_to(target)
    elif how == "directory":
        p.mkdir()
    else:
        put(p, "", 0o666)
    co, _ = coordinator(rt, tmp_path)
    with pytest.raises(hf.HostKeyError):
        co.prepare({"Router1": direct_cfg(s.port)})
    if how in ("symlink",):
        assert (Path(rt.sandbox) / "elsewhere").read_text() == ""
    assert "Accepted" not in sshd_log(rt, "a")


def test_registered_key_that_does_not_match_the_probe_is_a_storage_error(env, tmp_path, monkeypatch):
    rt, server = env
    K, other = make_host_key(rt, "K"), make_host_key(rt, "O")
    s = server("a", K)
    real = tofu.append_entry

    def wrong_key(path, ident, key):  # simulates a write that does not persist the probed key
        real(path, ident, tofu.ProbedKey(other["type"], other["blob"], other["fingerprint"]))

    monkeypatch.setattr(tofu, "append_entry", wrong_key)
    co, _ = coordinator(rt, tmp_path)
    with pytest.raises(hf.HostKeyError, match="TOFU_STORAGE_ERROR.*does not match"):
        co.prepare({"Router1": direct_cfg(s.port)})


def test_hostkeyalias_in_ssh_config_fails_closed_without_registration(env, tmp_path):
    rt, server = env
    K = make_host_key(rt, "K")
    s = server("a", K)
    with open(rt.ssh_config, "a") as fh:
        fh.write("  HostKeyAlias lab-alias\n")
    co, _ = coordinator(rt, tmp_path)
    with pytest.raises(hf.HostKeyError, match="TOFU_IDENTITY_ERROR"):
        co.prepare({"Router1": direct_cfg(s.port)})
    assert not Path(rt.product_known_hosts).exists() or Path(rt.product_known_hosts).read_text() == ""


def test_undetermined_known_hosts_fails_closed_before_any_probe(env, tmp_path):
    rt, server = env
    K = make_host_key(rt, "K")
    s = server("a", K)
    put(rt.known_hosts, "x", 0)
    try:
        co, _ = coordinator(rt, tmp_path)
        with pytest.raises(hf.HostKeyError, match="TOFU_IDENTITY_ERROR.*cannot be determined"):
            co.prepare({"Router1": direct_cfg(s.port)})
    finally:
        os.chmod(rt.known_hosts, 0o600)
    assert sshd_log(rt, "a").count(PROBE_LINE) == 0


def test_tofu_then_authentication_failure_keeps_the_key_and_reports_auth_failure(env, tmp_path):
    rt, server = env
    K = make_host_key(rt, "K")
    s = server("a", K)
    cfg = direct_cfg(s.port, user="nlm-unknown-login")
    co, _ = coordinator(rt, tmp_path)
    co.prepare({"R": cfg})
    out, rc = connect(rt, cfg, co)
    assert rc != 0 and "Permission denied" in out and "CONNECTED" not in out
    assert Path(rt.product_known_hosts).read_text() == entry(s.port, K)


# ---- legacy / algorithms ---------------------------------------------------------------------------


def test_sha1_only_rsa_device_is_not_enabled_automatically(env, tmp_path):
    rt, server = env
    R = make_host_key(rt, "R", "rsa", "2048")
    s = server("a", R, algorithms="ssh-rsa")
    co, _ = coordinator(rt, tmp_path)
    with pytest.raises(hf.HostKeyError, match="HOST_KEY_ALGORITHM_MISMATCH.*never enabled automatically"):
        co.prepare({"Legacy": direct_cfg(s.port)})
    assert not Path(rt.product_known_hosts).exists() or Path(rt.product_known_hosts).read_text() == ""


def test_failure_classification_of_openssh_messages():
    c = tofu.classify_ssh_failure
    assert c("Unable to negotiate with 192.0.2.1 port 22: no matching host key type found. Their offer: ssh-rsa")[0] == tofu.HOST_KEY_ALGORITHM_MISMATCH
    assert c("Unable to negotiate with 192.0.2.1 port 22: no matching key exchange method found.")[0] == tofu.SSH_KEX_MISMATCH
    assert c("ssh: connect to host 192.0.2.1 port 22: Connection refused")[0] == tofu.TOFU_PROBE_ERROR
    reset = c("Connection closed by 192.0.2.1 port 22")
    assert reset[0] == tofu.TOFU_PROBE_ERROR and "not known" in reset[1] and "block" not in reset[1].lower()
    assert c("something else entirely") is None


# ---- ProxyJump: independent hops --------------------------------------------------------------------


def test_proxyjump_jump_and_target_are_registered_independently_then_strict(env, tmp_path):
    rt, server = env
    J, T = make_host_key(rt, "J"), make_host_key(rt, "T")
    sj, st = server("j", J), server("t", T)
    cfg = jump_cfg(st.port, sj.port)
    co, _ = coordinator(rt, tmp_path)
    co.prepare({"Router1": cfg})
    lines = Path(rt.product_known_hosts).read_text().splitlines()
    assert lines == [entry(sj.port, J).strip(), entry(st.port, T).strip()]  # jump first
    assert [m.splitlines()[1].split()[1] for m in co.shown] == ["(jump)", "(target)"]
    assert sshd_log(rt, "t").count(PROBE_LINE) == 1 and "Accepted" not in sshd_log(rt, "t")  # target: probe only
    assert "Accepted publickey" in sshd_log(rt, "j")  # the jump hop authenticated for the target probe
    out, rc = connect(rt, cfg, co, "echo VIA_JUMP")
    assert "VIA_JUMP" in out and rc == 0


def test_registered_jump_and_unknown_target_probes_only_the_target(env, tmp_path):
    rt, server = env
    J, T = make_host_key(rt, "J"), make_host_key(rt, "T")
    sj, st = server("j", J), server("t", T)
    put(rt.product_known_hosts, entry(sj.port, J))
    co, _ = coordinator(rt, tmp_path)
    co.prepare({"Router1": jump_cfg(st.port, sj.port)})
    assert sshd_log(rt, "j").count(PROBE_LINE) == 0 and sshd_log(rt, "t").count(PROBE_LINE) == 1
    assert [m.splitlines()[1].split()[1] for m in co.shown] == ["(target)"]


def test_changed_jump_key_stops_the_target_probe_and_registers_nothing(env, tmp_path):
    rt, server = env
    J, J2, T = make_host_key(rt, "J"), make_host_key(rt, "J2"), make_host_key(rt, "T")
    sj0 = server("j0", J)
    port = sj0.port
    sj0.stop()
    server("j", J2, port=port)  # the jump host now presents another key
    st = server("t", T)
    put(rt.product_known_hosts, entry(port, J))
    before = Path(rt.product_known_hosts).read_bytes()
    co, _ = coordinator(rt, tmp_path)
    with pytest.raises(hf.HostKeyError, match="TOFU_PROBE_ERROR"):
        co.prepare({"Router1": jump_cfg(st.port, port)})
    assert Path(rt.product_known_hosts).read_bytes() == before
    assert sshd_log(rt, "t").count(PROBE_LINE) == 0  # nothing reached the target
    assert "Accepted" not in sshd_log(rt, "j")  # and the jump hop never authenticated against a changed key


def test_telnet_over_tunnel_registers_only_the_jump_host_key(env, tmp_path):
    rt, server = env
    J = make_host_key(rt, "J")
    s = server("j", J)
    cfg = {"transport": "telnet", "address": "192.0.2.30", "port": 23,
           "jump_host_config": {"address": "127.0.0.1", "port": s.port, "username": USER}}
    co, _ = coordinator(rt, tmp_path)
    co.prepare({"LegacyRouter": cfg})
    assert Path(rt.product_known_hosts).read_text() == entry(s.port, J)  # the Telnet device has no SSH key to verify
    assert [h.role for h in tofu.hops_for(cfg)] == ["jump"]


# ---- concurrency ----------------------------------------------------------------------------------------


def test_concurrent_first_use_of_one_host_probes_and_registers_once(env, tmp_path):
    rt, server = env
    K = make_host_key(rt, "K")
    s = server("a", K)
    cfg = direct_cfg(s.port)

    def one(_):
        hop = tofu.hops_for(cfg)[0]
        return tofu.register_unknown("R", cfg, hop, product_kh=rt.product_known_hosts, user_kh=rt.known_hosts,
                                     global_kh=rt.global_known_hosts, lock_dir=str(tmp_path / "lk"))

    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(one, range(6)))
    assert sum(r is not None for r in results) == 1
    assert Path(rt.product_known_hosts).read_text() == entry(s.port, K)
    assert sshd_log(rt, "a").count(PROBE_LINE) == 1


def test_concurrent_registration_of_different_hosts_keeps_every_entry(env, tmp_path):
    rt, server = env
    keys = [make_host_key(rt, f"K{i}") for i in range(4)]
    servers = [server(f"s{i}", k) for i, k in enumerate(keys)]

    def one(i):
        cfg = direct_cfg(servers[i].port)
        return tofu.register_unknown(f"R{i}", cfg, tofu.hops_for(cfg)[0], product_kh=rt.product_known_hosts,
                                     user_kh=rt.known_hosts, global_kh=rt.global_known_hosts, lock_dir=str(tmp_path / "lk"))

    with ThreadPoolExecutor(4) as pool:
        assert all(list(pool.map(one, range(4))))
    assert sorted(Path(rt.product_known_hosts).read_text().splitlines()) == sorted(
        entry(s.port, k).strip() for s, k in zip(servers, keys))


def test_lock_held_by_another_process_blocks_then_fails_closed(env, tmp_path, monkeypatch):
    import fcntl

    rt, server = env
    K = make_host_key(rt, "K")
    s = server("a", K)
    cfg = direct_cfg(s.port)
    lockdir = tmp_path / "lk"
    lockdir.mkdir(mode=0o700)
    name = hs.hashlib.sha256(os.path.realpath(rt.product_known_hosts).encode()).hexdigest()[:16] + ".lock"
    fd = os.open(lockdir / name, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)  # another holder (flock conflicts across open file descriptions)
    monkeypatch.setattr(hs.KnownHostsLock, "wait_seconds", 0.5)
    try:
        with pytest.raises(hf.HostKeyError, match="TOFU_STORAGE_ERROR.*locked"):
            tofu.register_unknown("R", cfg, tofu.hops_for(cfg)[0], product_kh=rt.product_known_hosts,
                                  user_kh=rt.known_hosts, global_kh=rt.global_known_hosts, lock_dir=str(lockdir))
    finally:
        os.close(fd)
    assert sshd_log(rt, "a").count(PROBE_LINE) == 0


# ---- replacement of a registered key: human only ------------------------------------------------------------


def _changed_setup(env, name_a="A", name_b="B", user_file=False, kind_b="ed25519", bits=None):
    rt, server = env
    A, B = make_host_key(rt, name_a), make_host_key(rt, name_b, kind_b, bits)
    other = make_host_key(rt, "O")
    s = server("a", A)
    port = s.port
    text = entry(port, A) + f"[192.0.2.9]:22 {other['type']} {other['blob']}\n"
    put(rt.known_hosts if user_file else rt.product_known_hosts, text)
    s.stop()
    server("b", B, port=port)
    return rt, A, B, other, port


def test_replacement_after_explicit_yes_backs_up_replaces_only_that_host_and_keeps_modes(env, tmp_path):
    rt, A, B, other, port = _changed_setup(env)
    before = Path(rt.product_known_hosts).read_text()
    co, ch = coordinator(rt, tmp_path, ["yes"])
    assert co.recover_changed(hf.HostKeyChanged("Router1", "target", "127.0.0.1", port), direct_cfg(port)) is True, ch.text
    assert "WARNING:\nThe SSH host key has changed." in ch.text and "man-in-the-middle" in ch.text
    assert A["fingerprint"] in ch.text and B["fingerprint"] in ch.text and "HOST_KEY_CHANGED" in ch.text
    assert "Replace the trusted host key? [yes/no] (default: no): " in ch.text
    st = registered(rt, port, [rt.product_known_hosts])
    assert [k.fingerprint for k in st.keys] == [B["fingerprint"]]
    assert f"[192.0.2.9]:22 {other['type']} {other['blob']}" in Path(rt.product_known_hosts).read_text()
    backups = list((Path(rt.sandbox) / "backups").iterdir())
    assert len(backups) == 1 and backups[0].read_text() == before
    assert stat.S_IMODE(os.stat(rt.product_known_hosts).st_mode) == 0o600
    assert not os.path.lexists(rt.product_known_hosts + ".old")
    out, rc = connect(rt, direct_cfg(port), co)
    assert "CONNECTED" in out and rc == 0


@pytest.mark.parametrize("answer", ["", "no", "y", "YES", "yes please"])
def test_replacement_is_cancelled_by_anything_but_exact_yes(env, tmp_path, answer):
    rt, A, B, other, port = _changed_setup(env)
    before = Path(rt.product_known_hosts).read_bytes()
    co, ch = coordinator(rt, tmp_path, [answer])
    assert co.recover_changed(hf.HostKeyChanged("Router1", "target", "127.0.0.1", port), direct_cfg(port)) is False
    assert Path(rt.product_known_hosts).read_bytes() == before and not (Path(rt.sandbox) / "backups").exists()


def test_replacement_without_a_terminal_channel_is_impossible(env, tmp_path):
    rt, A, B, other, port = _changed_setup(env)
    before = Path(rt.product_known_hosts).read_bytes()
    co, _ = coordinator(rt, tmp_path)  # no channel
    assert co.recover_changed(hf.HostKeyChanged("Router1", "target", "127.0.0.1", port), direct_cfg(port)) is False
    assert Path(rt.product_known_hosts).read_bytes() == before
    assert sshd_log(rt, "b").count(PROBE_LINE) == 0  # not even probed


def test_other_key_type_is_labelled_key_type_mismatch_and_needs_the_same_approval(env, tmp_path):
    rt, A, B, other, port = _changed_setup(env, kind_b="rsa", bits="2048")
    co, ch = coordinator(rt, tmp_path, ["yes"])
    assert co.recover_changed(hf.HostKeyChanged("Router1", "target", "127.0.0.1", port), direct_cfg(port)) is True
    assert "KEY_TYPE_MISMATCH" in ch.text


def test_entry_in_the_users_own_file_is_never_edited_manual_guidance_only(env, tmp_path):
    rt, A, B, other, port = _changed_setup(env, user_file=True)
    before = Path(rt.known_hosts).read_bytes()
    co, ch = coordinator(rt, tmp_path, ["yes"])
    assert co.recover_changed(hf.HostKeyChanged("Router1", "target", "127.0.0.1", port), direct_cfg(port)) is False
    assert Path(rt.known_hosts).read_bytes() == before and not Path(rt.product_known_hosts).exists()
    assert "ssh-keygen -R" in ch.text and "~/.ssh/known_hosts" in ch.text and ch.lines == ["yes"]  # never even asked


def test_failed_registration_after_removal_restores_the_previous_entries(env, tmp_path, monkeypatch):
    rt, A, B, other, port = _changed_setup(env)
    before = Path(rt.product_known_hosts).read_text()

    def boom(*a, **kw):
        raise hf.HostKeyError("disk full", tofu.TOFU_STORAGE_ERROR)

    monkeypatch.setattr(tofu, "append_entry", boom)
    co, ch = coordinator(rt, tmp_path, ["yes"])
    with pytest.raises(hf.HostKeyError, match="previous entries were restored"):
        co.recover_changed(hf.HostKeyChanged("Router1", "target", "127.0.0.1", port), direct_cfg(port))
    assert sorted(Path(rt.product_known_hosts).read_text().splitlines()) == sorted(before.splitlines())
    assert len(list((Path(rt.sandbox) / "backups").iterdir())) == 1


def test_known_hosts_changed_during_review_modifies_nothing(env, tmp_path, monkeypatch):
    rt, A, B, other, port = _changed_setup(env)
    co, ch = coordinator(rt, tmp_path, ["yes"])
    real = hf.confirm_replace

    def racing(channel, *a, **kw):  # another process edits the file while the human reads the warning
        ok = real(channel, *a, **kw)
        with open(rt.product_known_hosts, "a") as fh:
            fh.write(f"[192.0.2.77]:22 {other['type']} {other['blob']}\n")
        return ok

    monkeypatch.setattr(hf, "confirm_replace", racing)
    with pytest.raises(hf.HostKeyError, match="changed during review"):
        co.recover_changed(hf.HostKeyChanged("Router1", "target", "127.0.0.1", port), direct_cfg(port))
    assert entry(port, A) in Path(rt.product_known_hosts).read_text()
    assert not (Path(rt.sandbox) / "backups").exists()


def test_managed_terminal_layer_never_removes_replaces_or_extends_a_registered_key(env, tmp_path, monkeypatch):
    """What MCP/managed terminals can reach (terminal._ensure_host_keys -> hostkey_tofu) leaves a changed key alone."""
    from network_lab_mcp import terminal

    rt, A, B, other, port = _changed_setup(env)
    before = Path(rt.product_known_hosts).read_bytes()

    def forbidden(*a, **kw):
        raise AssertionError("removal/backup must be unreachable from the terminal layer")

    monkeypatch.setattr(hs, "remove_host", forbidden)
    monkeypatch.setattr(hs, "make_backup", forbidden)
    terminal._ensure_host_keys("Router1", direct_cfg(port))  # registered: nothing to do, nothing probed
    assert Path(rt.product_known_hosts).read_bytes() == before
    assert sshd_log(rt, "b").count(PROBE_LINE) == 0
