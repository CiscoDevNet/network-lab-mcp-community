# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Failure-injection: a temporary sshd started by a test must never outlive
the test, whatever way the test (or its fixture) fails. Each scenario runs an
inner pytest session in a subprocess and then checks the process table."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from hostkey_support import owned_sshd_alive_under, real_binaries_available

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


def run_inner(tmp_path, select: str, *, kill_after: float | None = None, sig=signal.SIGTERM):
    inner = tmp_path / "inner_tests"
    inner.mkdir()
    (inner / "test_inner.py").write_text(INNER.format(tests=str(TESTS_DIR)))
    base = tmp_path / "inner_base"
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
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


def test_reap_only_touches_processes_this_process_started(tmp_path):
    """reap_owned() must ignore pids it did not start and pids whose command line is not ours."""
    import hostkey_support as hsup

    victim = subprocess.Popen(["sleep", "30"])
    try:
        hsup._OWNED[victim.pid] = "/definitely/not/this/sandbox/sshd_x.conf"  # recorded pid, wrong command line
        hsup.reap_owned()
        assert victim.poll() is None  # still alive: command line did not match the recorded sandbox config
        assert victim.pid not in hsup._OWNED
    finally:
        victim.kill()
        victim.wait()
