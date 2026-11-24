# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Failure-injection: a temporary sshd started by a test must never outlive
the test, whatever way the test (or its fixture) fails. Each scenario runs an
inner pytest session in a subprocess and then checks the process table."""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import hostkey_support as hsup
from hostkey_support import ProcessTableError, owned_sshd_alive_under, real_binaries_available

pytestmark = pytest.mark.skipif(not real_binaries_available(), reason="OpenSSH binaries unavailable")
TESTS_DIR = Path(__file__).parent

INNER = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, {tests!r})
    import pytest
    from hostkey_support import Sshd, make_host_key, make_real_runtime
    from network_lab_mcp import ssh_isolation as iso

    @pytest.fixture
    def srv(tmp_path):
        rt = make_real_runtime(tmp_path)
        s = Sshd(rt, "a", make_host_key(rt, "A")).start()
        yield rt, s
        s.stop()
        iso.uninstall_for_tests()

    def test_assert_fails(srv):
        assert False, "injected assertion failure"

    def test_raises(srv):
        raise RuntimeError("injected exception")

    def test_sshd_config_breaks_startup(tmp_path):
        rt = make_real_runtime(tmp_path)
        try:
            s = Sshd(rt, "bad", make_host_key(rt, "B"), port=1)  # privileged port: sshd cannot bind
            s.start()
        finally:
            iso.uninstall_for_tests()

    def test_second_server_fails_after_first_started(tmp_path):
        rt = make_real_runtime(tmp_path)
        first = Sshd(rt, "one", make_host_key(rt, "A")).start()
        try:
            raise ValueError("injected after first server")
        finally:
            first.stop()
            iso.uninstall_for_tests()

    def test_hang(srv):
        time.sleep(600)
''')


FAKE_PS = textwrap.dedent('''\
    #!/bin/sh
    # Test double: emulate a ps that truncates the command column unless -ww is given.
    # Only the child pytest's PATH sees it; the real ps is called by absolute path.
    echo "$*" >> "$FAKE_PS_LOG"
    case " $* " in
      *" -ww "*) exec "$REAL_PS" "$@" ;;
    esac
    "$REAL_PS" "$@" | cut -c1-40
    exit 0
''')


def install_fake_ps(tmp_path) -> dict[str, str]:
    d = tmp_path / "fakebin"
    d.mkdir()
    fake = d / "ps"
    fake.write_text(FAKE_PS)
    fake.chmod(0o755)
    real = shutil.which("ps")
    assert real and real != str(fake)
    return {"PATH": f"{d}{os.pathsep}{os.environ['PATH']}", "REAL_PS": real, "FAKE_PS_LOG": str(tmp_path / "fake_ps.log")}


def run_inner(tmp_path, select: str, *, kill_after: float | None = None, sig=signal.SIGTERM, env_extra=None):
    inner = tmp_path / "inner_tests"
    inner.mkdir()
    (inner / "test_inner.py").write_text(INNER.format(tests=str(TESTS_DIR)))
    base = tmp_path / "inner_base"
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env.update(env_extra or {})
    cmd = [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(inner),
           "--basetemp", str(base), "-c", "/dev/null", str(inner / "test_inner.py"), "-k", select]
    proc = subprocess.Popen(cmd, cwd=str(inner), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)
    if kill_after is None:
        out, _ = proc.communicate(timeout=120)
    else:
        time.sleep(kill_after)
        os.kill(proc.pid, sig)
        out, _ = proc.communicate(timeout=60)
    return str(base), out


@pytest.mark.parametrize("select", ["assert_fails", "raises", "sshd_config_breaks_startup", "second_server_fails"])
@pytest.mark.parametrize("round_", [1, 2])
def test_no_orphan_sshd_after_failure(tmp_path, select, round_):
    base, out = run_inner(tmp_path, select)
    assert "failed" in out or "error" in out.lower(), out[-400:]
    assert owned_sshd_alive_under(base) == []


def test_no_orphan_sshd_after_sigterm_during_hang(tmp_path):
    base, out = run_inner(tmp_path, "hang", kill_after=8)
    time.sleep(1)
    assert owned_sshd_alive_under(base) == []


def test_no_orphan_sshd_after_ctrl_c_during_hang(tmp_path):
    base, out = run_inner(tmp_path, "hang", kill_after=8, sig=signal.SIGINT)
    time.sleep(1)
    assert owned_sshd_alive_under(base) == []


def test_sigterm_cleanup_does_not_depend_on_ps_width(tmp_path):
    """Regression (Linux ARM64): a width-limited ps in the child must not stop cleanup of an owned sshd.
    The parent's leftover check below uses the real ps, never the fake."""
    env = install_fake_ps(tmp_path)
    base, out = run_inner(tmp_path, "hang", kill_after=8, env_extra=env)
    time.sleep(1)
    assert owned_sshd_alive_under(base) == []


def test_exit_status_of_sigterm_cleanup_is_killed_by_sigterm(tmp_path):
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(TESTS_DIR)!r})
        import hostkey_support
        print("ready", flush=True)
        time.sleep(60)
    """)
    p = subprocess.Popen([sys.executable, "-B", "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "ready"
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=30) == -signal.SIGTERM


def test_detector_raises_when_ps_fails(monkeypatch):
    monkeypatch.setattr(hsup.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "boom"))
    with pytest.raises(ProcessTableError):
        owned_sshd_alive_under("/x")
    monkeypatch.setattr(hsup.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "garbage line\n", ""))
    with pytest.raises(ProcessTableError):
        owned_sshd_alive_under("/x")


def test_detector_uses_full_width_and_ignores_zombies(monkeypatch):
    seen = {}

    def fake_run(argv, **k):
        seen["argv"] = argv
        long = "/t/" + "d" * 300 + "/sshiso/etc/sshd_a.conf"
        out = f"11 1 11 Ss /usr/sbin/sshd -D -e -f {long}\n12 1 12 Z sshd -f {long}\n13 1 13 Ss sshd -f /other/sshiso/etc/sshd_a.conf\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(hsup.subprocess, "run", fake_run)
    hits = owned_sshd_alive_under("/t/" + "d" * 300)
    assert "-ww" in seen["argv"] or "-axww" in seen["argv"]
    assert len(hits) == 1 and hits[0].startswith("pid=11 ")  # not the zombie, not the other root


def _own(proc, cfg="/sandbox/sshiso/etc/sshd_t.conf", pgid=None):
    rec = hsup._Owned(proc, cfg, proc.pid if pgid is None else pgid)
    hsup._OWNED[proc.pid] = rec
    return rec


@pytest.fixture
def clean_owned():
    before = dict(hsup._OWNED)
    hsup.DISOWNED.clear()
    yield
    hsup._OWNED.clear()
    hsup._OWNED.update(before)


def _spawn_group():
    """A leader in its own session with one descendant in the same group (what sshd does)."""
    return subprocess.Popen(["sh", "-c", "sleep 60 & wait"], start_new_session=True)


def _group_members(pgid):
    return [r for r in hsup.ps_table() if r[2] == pgid and not r[3].startswith("Z")]


def test_owned_group_is_terminated_and_leader_reaped(clean_owned):
    leader = _spawn_group()
    time.sleep(0.5)
    _own(leader)
    assert _group_members(leader.pid)
    assert hsup.reap_owned() == 1
    assert leader.returncode is not None and leader.pid not in hsup._OWNED
    assert _group_members(leader.pid) == []
    assert hsup.reap_owned() == 0  # idempotent


def test_sigterm_ignoring_leader_is_escalated_to_sigkill(clean_owned, monkeypatch):
    monkeypatch.setattr(hsup, "TERM_GRACE", 0.5)
    leader = subprocess.Popen(["sh", "-c", "trap '' TERM; sleep 60 & while :; do sleep 1; done"], start_new_session=True)
    time.sleep(0.5)
    _own(leader)
    assert hsup.reap_owned() == 1
    assert leader.returncode == -signal.SIGKILL
    assert _group_members(leader.pid) == []


def test_exited_but_unreaped_leader_is_still_group_killed_then_reaped(clean_owned):
    leader = subprocess.Popen(["sh", "-c", "sleep 60 & exit 0"], start_new_session=True)
    time.sleep(0.5)  # leader is now a zombie (we have not polled), descendant alive in the group
    assert leader.returncode is None and hsup._leader_state(leader) == "exited"
    _own(leader)
    assert hsup.reap_owned() == 1
    assert _group_members(leader.pid) == []


def test_reaped_leader_never_gets_killpg(clean_owned, monkeypatch):
    leader = subprocess.Popen(["true"], start_new_session=True)
    leader.wait()
    _own(leader)
    calls = []
    monkeypatch.setattr(hsup.os, "killpg", lambda *a: calls.append(("killpg", a)))
    monkeypatch.setattr(hsup.os, "kill", lambda *a: calls.append(("kill", a)))
    hsup.reap_owned()
    assert calls == [] and leader.pid not in hsup._OWNED


def test_externally_reaped_leader_never_gets_killpg(clean_owned, monkeypatch):
    leader = subprocess.Popen(["true"], start_new_session=True)
    os.waitpid(leader.pid, 0)  # reaped behind Popen's back: returncode still None
    _own(leader)
    calls = []
    monkeypatch.setattr(hsup.os, "killpg", lambda *a: calls.append(a))
    hsup.reap_owned()
    assert calls == []


def test_unknown_leader_state_without_waitid_still_checks_returncode(clean_owned, monkeypatch):
    leader = subprocess.Popen(["true"], start_new_session=True)
    leader.wait()
    _own(leader)
    monkeypatch.delattr(hsup.os, "waitid")
    calls = []
    monkeypatch.setattr(hsup.os, "killpg", lambda *a: calls.append(a))
    hsup.reap_owned()
    assert calls == []


@pytest.mark.parametrize("pgid", [0, 1, -5, "own", "other"])
def test_group_signal_refused_when_pgid_is_not_our_created_group(clean_owned, monkeypatch, pgid):
    victim = subprocess.Popen(["sleep", "30"])  # same group as pytest: NOT a group we created
    try:
        bad = os.getpgrp() if pgid == "own" else (victim.pid + 12345 if pgid == "other" else pgid)
        _own(victim, pgid=bad)
        calls = []
        monkeypatch.setattr(hsup.os, "killpg", lambda *a: calls.append(a))
        monkeypatch.setattr(hsup.os, "kill", lambda *a: calls.append(a))
        hsup.reap_owned()
        assert calls == [] and victim.poll() is None
        assert victim.pid not in hsup._OWNED and hsup.DISOWNED  # quarantined and reported, not retried
    finally:
        monkeypatch.undo()  # os.kill is patched above; Popen.kill() needs the real one
        victim.kill()
        victim.wait()


def test_pgid_recycled_to_unrelated_group_is_not_signalled(clean_owned, monkeypatch):
    leader = _spawn_group()
    time.sleep(0.3)
    try:
        _own(leader)
        monkeypatch.setattr(hsup.os, "getpgid", lambda pid: os.getpgrp())  # simulate identity mismatch
        calls = []
        monkeypatch.setattr(hsup.os, "killpg", lambda *a: calls.append(a))
        hsup.reap_owned()
        assert calls == []
    finally:
        monkeypatch.undo()
        os.killpg(leader.pid, signal.SIGKILL)
        leader.wait()


def test_unconfirmed_termination_keeps_the_record(clean_owned, monkeypatch):
    monkeypatch.setattr(hsup, "TERM_GRACE", 0.1)
    monkeypatch.setattr(hsup, "KILL_GRACE", 0.1)
    leader = _spawn_group()
    time.sleep(0.3)
    try:
        _own(leader)
        monkeypatch.setattr(hsup.os, "killpg", lambda *a: None)  # signals "lost": nothing dies
        assert hsup.reap_owned() == 0
        assert leader.pid in hsup._OWNED and hsup._OWNED[leader.pid].state == "unconfirmed"
        monkeypatch.undo()
        assert hsup.reap_owned() == 1  # a later cleanup re-evaluates and succeeds
        assert _group_members(leader.pid) == []
    finally:
        monkeypatch.undo()
        if leader.poll() is None:
            os.killpg(leader.pid, signal.SIGKILL)
            leader.wait()


def test_cleanup_error_does_not_abort_other_records(clean_owned, monkeypatch):
    a, b = _spawn_group(), _spawn_group()
    time.sleep(0.3)
    _own(a)
    _own(b)
    real = hsup._terminate
    monkeypatch.setattr(hsup, "_terminate", lambda rec: (_ for _ in ()).throw(RuntimeError("x")) if rec.proc is a else real(rec))
    assert hsup.reap_owned() == 1
    assert b.returncode is not None and a.pid in hsup._OWNED
    monkeypatch.undo()
    assert hsup.reap_owned() == 1


def test_reentrant_cleanup_is_a_noop(clean_owned, monkeypatch):
    leader = _spawn_group()
    time.sleep(0.3)
    _own(leader)
    nested = []
    real = hsup._terminate

    def wrapped(rec):
        nested.append(hsup.reap_owned())  # e.g. atexit/handler arriving mid-cleanup
        return real(rec)

    monkeypatch.setattr(hsup, "_terminate", wrapped)
    assert hsup.reap_owned() == 1 and nested == [0]


def test_second_sigterm_during_cleanup_does_not_abandon_it(tmp_path):
    marker = tmp_path / "in_cleanup"
    code = textwrap.dedent(f"""
        import os, subprocess, sys, time, signal
        sys.path.insert(0, {str(TESTS_DIR)!r})
        import hostkey_support as h
        leader = subprocess.Popen(["sh", "-c", "trap '' TERM; sleep 60 & while :; do sleep 1; done"], start_new_session=True)
        time.sleep(0.5)
        h._OWNED[leader.pid] = h._Owned(leader, "/x/sshiso/etc/sshd_t.conf", leader.pid)
        h.TERM_GRACE = 2.0
        real = h._terminate
        def slow(rec):
            open({str(marker)!r}, "w").write("1")
            return real(rec)
        h._terminate = slow
        print(leader.pid, flush=True)
        time.sleep(60)
    """)
    p = subprocess.Popen([sys.executable, "-B", "-c", code], stdout=subprocess.PIPE, text=True)
    pgid = int(p.stdout.readline())
    p.send_signal(signal.SIGTERM)
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists()
    p.send_signal(signal.SIGTERM)  # arrives while cleanup is waiting out the SIGTERM-ignoring leader
    assert p.wait(timeout=30) == -signal.SIGTERM
    time.sleep(0.5)
    assert [r for r in hsup.ps_table() if r[2] == pgid and not r[3].startswith("Z")] == []


def test_sigterm_during_spawn_is_deferred(monkeypatch):
    monkeypatch.setattr(hsup, "_spawning", True)
    monkeypatch.setattr(hsup, "_pending_term", False)
    called = []
    monkeypatch.setattr(hsup, "reap_owned", lambda: called.append(1))
    hsup._on_term(signal.SIGTERM, None)
    assert hsup._pending_term is True and called == []


def test_handler_still_terminates_when_cleanup_raises(monkeypatch):
    sent = []
    monkeypatch.setattr(hsup, "_term_started", False)
    monkeypatch.setattr(hsup, "reap_owned", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(hsup.os, "write", lambda *a: (_ for _ in ()).throw(OSError("stderr closed")))
    monkeypatch.setattr(hsup.os, "kill", lambda pid, sig: sent.append(sig))
    restored = []
    monkeypatch.setattr(hsup.signal, "signal", lambda s, h: restored.append(h))
    hsup._on_term(signal.SIGTERM, None)
    assert sent == [signal.SIGTERM] and restored == [signal.SIG_DFL]


def test_reap_only_touches_processes_this_process_started(tmp_path, clean_owned):
    """reap_owned() must never signal a process that is not a group we created (pid recorded or not)."""
    victim = subprocess.Popen(["sleep", "30"])
    try:
        _own(victim, cfg="/definitely/not/this/sandbox/sshd_x.conf")  # recorded pid, but not our own session/group
        hsup.reap_owned()
        assert victim.poll() is None  # still alive: no ownership evidence for it
        assert victim.pid not in hsup._OWNED
    finally:
        victim.kill()
        victim.wait()
