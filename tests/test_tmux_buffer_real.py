# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Real-tmux conformance, data-integrity and concurrency tests for the
buffer transport (`terminal._send_text_via_stdin` / `_cleanup_buffer`).

Every test runs on its own disposable tmux server (unique socket under the
suite's private TMUX_TMPDIR); the production `network-lab-mcp` socket is never
used. Only dummy data. Stress tests need NETWORK_LAB_STRESS=1 (see
docs/architecture.md); pass `-s` to see the one-line JSON summaries.

  pytest tests/test_tmux_buffer_real.py                       # daily
  NETWORK_LAB_STRESS=1 pytest tests/test_tmux_buffer_real.py -m stress -s
"""

from __future__ import annotations

import json
import multiprocessing
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from network_lab_mcp import terminal

sys.path.insert(0, str(Path(__file__).parent))
import tmux_buffer_support as sup  # noqa: E402

stress = pytest.mark.stress
needs_stress = pytest.mark.skipif(not sup.stress_enabled(), reason="set NETWORK_LAB_STRESS=1 to run Stress tests")


@pytest.fixture
def server(monkeypatch, tmp_path):
    """A private tmux server with one idle session ("anchor") so it survives."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    name = f"nlm-bufreal-{os.getpid()}-{secrets.token_hex(4)}"
    monkeypatch.setattr(terminal, "TMUX_SOCKET_NAME", name)
    subprocess.run(["tmux", "-L", name, "-f", "/dev/null", "new-session", "-d", "-s", "anchor", "cat"], check=True)
    yield name
    subprocess.run(["tmux", "-L", name, "kill-server"], capture_output=True)


def _buffers() -> dict[str, bytes]:
    names = terminal._run(["list-buffers", "-F", "#{buffer_name}"], check=False).stdout.split()
    out = {}
    for n in names:
        out[n] = subprocess.run(
            terminal._tmux_base() + ["show-buffer", "-b", n], capture_output=True
        ).stdout  # dummy data only
    return out


def _load(name: str, data: bytes) -> None:
    subprocess.run(terminal._tmux_base() + ["load-buffer", "-b", name, "-"], input=data, check=True)


def _summary(test: str, **fields) -> None:
    base = {"test": test, "tmux_version": subprocess.run(["tmux", "-V"], capture_output=True, text=True).stdout.strip()}
    print("NLM_SUMMARY " + json.dumps({**base, **fields}, sort_keys=True))


# ---------------- conformance (C01-C08, C13-C15, K01, K02, K05, K07) ----------------


def test_c01_c02_paste_with_d_removes_its_own_buffer(server):
    _load("b1", b"x")
    _load("b2", b"y")
    terminal._run(["paste-buffer", "-r", "-d", "-b", "b1", "-t", "anchor"])
    assert set(_buffers()) == {"b2"}


def test_c03_c04_c05_observed_buffer_command_behaviour_on_missing_names(server):
    """Version-dependent: record, don't require, the legacy fallback; but
    save-buffer on a missing name must fail without touching others."""
    _load("keep-a", b"A")
    _load("keep-b", b"B")
    save = terminal._run(["save-buffer", "-b", "missing", "/dev/null"], check=False)
    assert save.returncode != 0 and set(_buffers()) == {"keep-a", "keep-b"}
    d = terminal._run(["delete-buffer", "-b", "missing"], check=False)
    _summary("c04_delete_buffer_missing_name", rc=d.returncode, buffers_after=sorted(_buffers()))


def test_k01_c07_c14_cleanup_of_missing_name_keeps_others_intact(server):
    _load("other-1", b"ONE")
    _load("other-2", b"TWO")
    assert terminal._cleanup_buffer("nlm-never-existed") == terminal.BUFFER_ALREADY_ABSENT
    assert _buffers() == {"other-1": b"ONE", "other-2": b"TWO"}


def test_k02_c06_cleanup_of_existing_name_removes_only_it(server):
    _load("other-1", b"ONE")
    _load("nlm-target", b"DUMMY_TARGET")
    _load("other-2", b"TWO")
    assert terminal._cleanup_buffer("nlm-target") == terminal.BUFFER_CLEANED
    assert _buffers() == {"other-1": b"ONE", "other-2": b"TWO"}


def test_c08_save_failure_skips_following_delete(server):
    _load("a", b"A")
    _load("b", b"B")
    r = terminal._run(["save-buffer", "-b", "gone", "/dev/null", ";", "delete-buffer", "-b", "gone"], check=False)
    assert r.returncode != 0 and set(_buffers()) == {"a", "b"}


def test_c15_error_text_matches_classifier(server):
    r = terminal._run(["save-buffer", "-b", "nlm-x", "/dev/null"], check=False)
    assert r.stderr.strip() == "no buffer nlm-x"
    p = terminal._run(["paste-buffer", "-b", "nlm-x", "-t", "anchor"], check=False)
    assert p.stderr.strip() == "no buffer nlm-x"


def test_k05_c10_vanished_pane_cleans_own_buffer_keeps_others_and_server(server, monkeypatch, tmp_path):
    _load("keep", b"KEEP")
    monkeypatch.setattr(terminal, "_send_target", lambda name: "%99999")
    with pytest.raises(terminal.TerminalError, match="target pane is unavailable") as ei:
        terminal._send_text_via_stdin("anchor", "DUMMY_SECRET_K05")
    assert "DUMMY_SECRET_K05" not in str(ei.value) and ei.value.__cause__ is None
    assert _buffers() == {"keep": b"KEEP"}  # no residual secret buffer
    # k07: later sends still work
    monkeypatch.undo()
    monkeypatch.setattr(terminal, "TMUX_SOCKET_NAME", server)
    terminal._send_text_via_stdin("anchor", "after")
    assert sup.residual_nlm_buffers(terminal) == []


def test_c13_k06_cleanup_without_a_server_does_not_start_one(monkeypatch):
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    name = f"nlm-noserver-{os.getpid()}-{secrets.token_hex(4)}"
    monkeypatch.setattr(terminal, "TMUX_SOCKET_NAME", name)
    assert terminal._cleanup_buffer("nlm-x") == terminal.BUFFER_CLEANUP_FAILED
    probe = subprocess.run(["tmux", "-L", name, "list-sessions"], capture_output=True)
    assert probe.returncode != 0  # still no server
    with pytest.raises(terminal.TerminalError):
        monkeypatch.setattr(terminal, "_send_target", lambda n: "%1")
        terminal._send_text_via_stdin("s", "x")
    assert subprocess.run(["tmux", "-L", name, "list-sessions"], capture_output=True).returncode != 0


# ---------------- data integrity (C12, Thread, Process, mixed, locale) ----------------


def _total_bytes(workers: list[str], count: int) -> int:
    return sum(len(sup.frame(w, s)) for w in workers for s in range(count))


def _check(out: Path, expected: set[tuple[str, int]]) -> dict:
    data = out.read_bytes() if out.exists() else b""
    seen, bad = sup.parse_frames(data)
    uniq = set(seen)
    return {
        "missing_payloads": len(expected - uniq),
        "duplicate_payloads": len(seen) - len(uniq),
        "cross_pane_payloads": len(uniq - expected),
        "corrupted_payloads": bad,
        "partial_payloads": 1 if len(data) < sum(len(sup.frame(w, s)) for w, s in expected) else 0,
    }


def _assert_clean(res: dict):
    assert all(v == 0 for v in res.values()), res


def test_c12_single_send_delivers_exact_bytes_and_leaves_no_buffer(server, tmp_path):
    payload = "line1\nタブ\t🚀 'q' $x;\\"
    out_session, out = sup.open_receiver(terminal, "c12", tmp_path, len(payload.encode()))
    terminal._send_text_via_stdin(out_session, payload)
    sup.wait_for(lambda: out.exists() and out.stat().st_size >= len(payload.encode()), "bytes")
    assert out.read_bytes() == payload.encode("utf-8")
    assert sup.residual_nlm_buffers(terminal) == []


def _run_threads(tmp_path, sessions: int, per: int, tag: str):
    workers = [f"{tag}{i}" for i in range(sessions)]
    outs, names = {}, {}
    for w in workers:
        names[w], outs[w] = sup.open_receiver(terminal, w, tmp_path, _total_bytes([w], per))
    results = {w: {} for w in workers}
    threads = [threading.Thread(target=sup.send_loop, args=(names[w], w, per, results[w])) for w in workers]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for w in workers:
        need = _total_bytes([w], per)
        sup.wait_for(lambda w=w, need=need: outs[w].exists() and outs[w].stat().st_size >= need, f"{w} bytes", 90)
    agg = {"missing_payloads": 0, "duplicate_payloads": 0, "cross_pane_payloads": 0, "corrupted_payloads": 0, "partial_payloads": 0}
    for w in workers:
        for k, v in _check(outs[w], {(w, s) for s in range(per)}).items():
            agg[k] += v
    fails = sum(r.get("failures", 0) for r in results.values())
    return agg, fails, time.monotonic() - t0


def test_thread_parallel_300_operations(server, tmp_path):
    agg, fails, secs = _run_threads(tmp_path, 3, 100, "t")
    _summary("thread_parallel", threads=3, sessions=3, operations=300, command_failures=fails,
             residual_buffers=len(sup.residual_nlm_buffers(terminal)), elapsed_seconds=round(secs, 2), **agg)
    assert fails == 0 and sup.residual_nlm_buffers(terminal) == []
    _assert_clean(agg)


def _spawn_procs(server, jobs_per_proc):
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=sup.process_worker, args=(server, jobs, q)) for jobs in jobs_per_proc]
    for p in procs:
        p.start()
    for p in procs:
        p.join(300)
    return sum(q.get(timeout=10) for _ in procs)


def _process_case(server, tmp_path, same_session: bool, per: int, tag: str):
    procs = 3
    if same_session:
        name, out = sup.open_receiver(terminal, f"{tag}s", tmp_path, _total_bytes([f"{tag}{i}" for i in range(procs)], per))
        jobs = [[(name, f"{tag}{i}", per)] for i in range(procs)]
        outs = {None: out}
        expected = {(f"{tag}{i}", s) for i in range(procs) for s in range(per)}
    else:
        jobs, outs, expected = [], {}, set()
        for i in range(procs):
            w = f"{tag}{i}"
            name, out = sup.open_receiver(terminal, w, tmp_path, _total_bytes([w], per))
            jobs.append([(name, w, per)])
            outs[w] = out
            expected |= {(w, s) for s in range(per)}
    t0 = time.monotonic()
    fails = _spawn_procs(server, jobs)
    for out in outs.values():
        sup.wait_for(lambda out=out: out.exists() and out.stat().st_size > 0, "output", 90)
    time.sleep(0.5)
    agg = {"missing_payloads": 0, "duplicate_payloads": 0, "cross_pane_payloads": 0, "corrupted_payloads": 0, "partial_payloads": 0}
    if same_session:
        # complete only when every frame arrived; otherwise _check reports missing/partial
        sup.wait_for(lambda: len(sup.parse_frames(outs[None].read_bytes())[0]) >= len(expected), "all frames", 90)
        res = _check(outs[None], expected)
        for k, v in res.items():
            agg[k] += v
    else:
        for w, out in outs.items():
            need = _total_bytes([w], per)
            sup.wait_for(lambda out=out, need=need: out.stat().st_size >= need, f"{w} bytes", 90)
            for k, v in _check(out, {(x, s) for x, s in expected if x == w}).items():
                agg[k] += v
    return agg, fails, time.monotonic() - t0


def test_process_parallel_light_diff_sessions(server, tmp_path):
    agg, fails, secs = _process_case(server, tmp_path, False, 20, "pl")
    _summary("process_parallel_light", processes=3, sessions=3, operations=60, command_failures=fails,
             residual_buffers=len(sup.residual_nlm_buffers(terminal)), elapsed_seconds=round(secs, 2), **agg)
    assert fails == 0 and sup.residual_nlm_buffers(terminal) == []
    _assert_clean(agg)


def test_process_parallel_light_same_session(server, tmp_path):
    agg, fails, secs = _process_case(server, tmp_path, True, 20, "ps")
    _summary("process_same_session_light", processes=3, sessions=1, operations=60, command_failures=fails,
             residual_buffers=len(sup.residual_nlm_buffers(terminal)), elapsed_seconds=round(secs, 2), **agg)
    assert fails == 0 and sup.residual_nlm_buffers(terminal) == []
    _assert_clean(agg)


@stress
@needs_stress
def test_stress_process_parallel_diff_sessions(server, tmp_path):
    per = 50 * sup.scale()
    agg, fails, secs = _process_case(server, tmp_path, False, per, "xd")
    _summary("stress_process_parallel", processes=3, sessions=3, operations=3 * per, command_failures=fails,
             residual_buffers=len(sup.residual_nlm_buffers(terminal)), elapsed_seconds=round(secs, 2), **agg)
    assert fails == 0 and sup.residual_nlm_buffers(terminal) == []
    _assert_clean(agg)


@stress
@needs_stress
def test_stress_process_parallel_same_session(server, tmp_path):
    per = 50 * sup.scale()
    agg, fails, secs = _process_case(server, tmp_path, True, per, "xs")
    _summary("stress_process_same_session", processes=3, sessions=1, operations=3 * per, command_failures=fails,
             residual_buffers=len(sup.residual_nlm_buffers(terminal)), elapsed_seconds=round(secs, 2), **agg)
    assert fails == 0 and sup.residual_nlm_buffers(terminal) == []
    _assert_clean(agg)  # any interleaving would surface as corrupted/missing frames


@stress
@needs_stress
def test_stress_mixed_load_2400_operations(server, tmp_path):
    runs, per, nproc, nthr, nsess = 2, 200 * sup.scale(), 3, 2, 6
    total = {"missing_payloads": 0, "duplicate_payloads": 0, "cross_pane_payloads": 0, "corrupted_payloads": 0, "partial_payloads": 0}
    fails_total, ops, t_all, load_total = 0, 0, 0.0, 0
    terminal._run(["new-session", "-d", "-s", "loadsess", "cat"])
    for run in range(runs):
        stop, counters = threading.Event(), {}
        loaders = [threading.Thread(target=sup.load_worker, args=(terminal, stop, counters, "loadsess", i)) for i in range(3)]
        for t in loaders:
            t.start()
        outs, jobs_by_proc, expected = {}, [[] for _ in range(nproc)], {}
        for s in range(nsess):
            sid = f"m{run}s{s}"
            workers = [f"{sid}w{k}" for k in range(1)]  # 6 sessions x 1 worker = 3 proc x 2 thr
            nm, out = sup.open_receiver(terminal, sid, tmp_path, _total_bytes(workers, per))
            outs[sid] = (out, workers)
            for k, w in enumerate(workers):
                jobs_by_proc[s % nproc].append((nm, w, per))
        t0 = time.monotonic()
        fails_total += _spawn_procs(server, jobs_by_proc)
        t_all += time.monotonic() - t0
        for sid, (out, workers) in outs.items():
            exp = {(w, s) for w in workers for s in range(per)}
            sup.wait_for(lambda out=out, exp=exp: len(sup.parse_frames(out.read_bytes())[0]) >= len(exp), f"{sid} frames", 120)
            for k, v in _check(out, exp).items():
                total[k] += v
            ops += len(exp)
        stop.set()
        for t in loaders:
            t.join()
        assert min(counters.values()) > 0, "load workers did not run"
        load_total += sum(counters.values())
    resid = len(sup.residual_nlm_buffers(terminal))
    _summary("stress_mixed_load", processes=nproc, threads_per_process=nthr, sessions=nsess, runs=runs,
             operations=ops, command_failures=fails_total, residual_buffers=resid, load_ops=load_total,
             elapsed_seconds=round(t_all, 2), **total)
    assert fails_total == 0 and resid == 0
    _assert_clean(total)


# ---------------- K03: cleanup under load ----------------


def _cleanup_load(tmp_path, absent_n: int, own_n: int, per: int):
    workers = ["k0", "k1", "k2"]
    outs, names = {}, {}
    for w in workers:
        names[w], outs[w] = sup.open_receiver(terminal, w, tmp_path, _total_bytes([w], per))
    _load("manual-keep", b"USER_BUFFER_DUMMY")
    results = {w: {} for w in workers}
    outcomes = {"absent": 0, "own_cleaned": 0, "unexpected": 0}
    lock = threading.Lock()

    def absent_cleaner():
        for _ in range(absent_n // 3):
            r = terminal._cleanup_buffer(terminal._new_buffer_name())
            with lock:
                outcomes["absent" if r == terminal.BUFFER_ALREADY_ABSENT else "unexpected"] += 1

    def own_cleaner():
        for _ in range(own_n // 3):
            n = terminal._new_buffer_name()
            _load(n, b"DUMMY_OWN")
            r = terminal._cleanup_buffer(n)
            with lock:
                outcomes["own_cleaned" if r == terminal.BUFFER_CLEANED else "unexpected"] += 1

    threads = [threading.Thread(target=sup.send_loop, args=(names[w], w, per, results[w])) for w in workers]
    threads += [threading.Thread(target=absent_cleaner) for _ in range(3)]
    threads += [threading.Thread(target=own_cleaner) for _ in range(3)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for w in workers:
        need = _total_bytes([w], per)
        sup.wait_for(lambda w=w, need=need: outs[w].exists() and outs[w].stat().st_size >= need, f"{w} bytes", 90)
    agg = {"missing_payloads": 0, "duplicate_payloads": 0, "cross_pane_payloads": 0, "corrupted_payloads": 0, "partial_payloads": 0}
    for w in workers:
        for k, v in _check(outs[w], {(w, s) for s in range(per)}).items():
            agg[k] += v
    sender_failures = sum(r.get("failures", 0) for r in results.values())
    bufs = _buffers()
    return agg, sender_failures, outcomes, bufs, time.monotonic() - t0


def test_k03_cleanup_under_load_light(server, tmp_path):
    agg, sf, oc, bufs, secs = _cleanup_load(tmp_path, 150, 60, 40)
    _summary("k03_cleanup_light", sender_failures=sf, expected_absent_cleanups=oc["absent"], own_cleaned=oc["own_cleaned"],
             unexpected_cleanup_results=oc["unexpected"], elapsed_seconds=round(secs, 2), **agg)
    assert sf == 0 and oc["unexpected"] == 0 and bufs == {"manual-keep": b"USER_BUFFER_DUMMY"}
    _assert_clean(agg)


@stress
@needs_stress
def test_stress_k03_cleanup_900_plus(server, tmp_path):
    n = 900 * sup.scale()
    agg, sf, oc, bufs, secs = _cleanup_load(tmp_path, n, n // 3, 100)
    _summary("stress_k03_cleanup", sender_failures=sf, expected_absent_cleanups=oc["absent"], own_cleaned=oc["own_cleaned"],
             unexpected_cleanup_results=oc["unexpected"], unrelated_buffers_lost=0 if "manual-keep" in bufs else 1,
             residual_buffers=len([b for b in bufs if b.startswith("nlm-")]), elapsed_seconds=round(secs, 2), **agg)
    assert sf == 0 and oc["unexpected"] == 0 and oc["absent"] == n
    assert bufs == {"manual-keep": b"USER_BUFFER_DUMMY"}
    _assert_clean(agg)


# ---------------- locale independence ----------------


def test_utf8_payload_is_locale_independent(server, tmp_path):
    """A child Python run with LC_ALL=C / PYTHONUTF8=0 still delivers UTF-8 bytes."""
    payload = "日本語🚀\nline2"
    session, out = sup.open_receiver(terminal, "loc", tmp_path, len(payload.encode()))
    env = {**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    code = (
        "import sys, locale;from network_lab_mcp import terminal;"
        f"terminal.TMUX_SOCKET_NAME={server!r};"
        "enc=locale.getpreferredencoding(False);"
        "terminal._send_text_via_stdin(sys.argv[1], sys.argv[2]);print('PYENC='+enc+' utf8mode='+str(sys.flags.utf8_mode))"
    )
    r = subprocess.run([sys.executable, "-c", code, session, payload.encode("utf-8").decode("utf-8", "surrogateescape")],
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr[-300:]
    sup.wait_for(lambda: out.exists() and out.stat().st_size >= len(payload.encode()), "bytes")
    assert out.read_bytes() == payload.encode("utf-8")
    _summary("locale_independence", child=r.stdout.strip())
