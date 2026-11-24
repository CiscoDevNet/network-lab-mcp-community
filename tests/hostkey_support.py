# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Helpers for tests that use the REAL OpenSSH binaries, always through the
forced isolation wrapper and a per-test sandbox (never the user's ~/.ssh)."""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from network_lab_mcp import ssh_isolation as iso

SYSTEM_SSH = "/usr/bin/ssh"
SYSTEM_KEYGEN = "/usr/bin/ssh-keygen"
SYSTEM_SSHD = "/usr/sbin/sshd"


def real_binaries_available() -> bool:
    return all(os.access(p, os.X_OK) for p in (SYSTEM_SSH, SYSTEM_KEYGEN, SYSTEM_SSHD)) and bool(shutil.which("tmux"))


def make_real_runtime(base: Path, *, install: bool = True) -> iso.SshRuntime:
    rt = iso.create_sandbox(
        str(base), ssh=SYSTEM_SSH, ssh_keygen=SYSTEM_KEYGEN, sshd=SYSTEM_SSHD,
        tmux=os.path.realpath(shutil.which("tmux")),
    )
    Path(rt.ssh_config).write_text(iso.default_ssh_config(rt))
    keygen(rt, "-t", "ed25519", "-f", rt.identity, "-N", "", "-q", "-C", "nlm-test")
    if install:
        iso.install_for_tests(rt)
    return rt


def keygen(rt: iso.SshRuntime, *args: str) -> subprocess.CompletedProcess:
    r = subprocess.run([rt.wrapper("ssh-keygen"), *args], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r


def make_host_key(rt: iso.SshRuntime, name: str, key_type: str = "ed25519", bits: str | None = None) -> dict:
    d = Path(rt.sandbox) / "hostkeys"
    d.mkdir(exist_ok=True, mode=0o700)
    path = d / name
    extra = ["-b", bits] if bits else []
    keygen(rt, "-t", key_type, *extra, "-f", str(path), "-N", "", "-q", "-C", name)
    pub = Path(str(path) + ".pub").read_text().split()
    fp = keygen(rt, "-l", "-f", str(path) + ".pub").stdout.split()[1]
    return {"path": str(path), "type": pub[0], "blob": pub[1], "fingerprint": fp}


import signal
import socket
import time


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --------------------------------------------------------------------------
# Ownership and cleanup of the sshd servers THIS process started.
#
# Ownership evidence (strongest first): the live, UNREAPED Popen child we spawned
# with start_new_session=True. While that leader is unreaped (running, or exited
# but a zombie) its pid -- and therefore the process group id == pid -- cannot be
# recycled, so killpg(pgid) can only reach our own group. Once the leader has been
# reaped (by anyone) that guarantee is gone: no signal is ever sent to the stored
# pid/pgid again. `ps`/command lines are only diagnostics and the independent
# leftover detector; they are never the reason to signal a process.
#
# Order for a group: SIGTERM -> wait for the leader to exit WITHOUT reaping it
# (os.waitid WNOWAIT, bounded) -> SIGKILL the group (leader still unreaped, so the
# group id is still ours) -> reap the leader with wait(timeout=...). The record is
# released only after the leader was reaped; otherwise it stays for a later retry.
#
# Signal-handler safety: only Popen.wait(timeout=...) is used while cleaning
# (it never blocks on the Popen waitpid lock, unlike wait()/poll() from a handler
# that interrupted wait()). A second SIGTERM during cleanup is ignored (cleanup
# finishes, then the process dies of SIGTERM); a SIGTERM during spawn+register is
# deferred and re-delivered right after registration.
#
# Known limits: SIGKILL of the pytest process runs no handler/atexit (only the
# independent leftover detector in the parent can notice); grandchildren that left
# the process group are not signalled (never guessed); a small TOCTOU window
# remains between the state check and killpg (a nested cleanup may reap the leader
# in between). `ps` is called with -ww and LC_ALL=C but sshd may rewrite its own
# process title, so command lines are never treated as a complete argv.
# --------------------------------------------------------------------------


@dataclass
class _Owned:
    proc: subprocess.Popen
    cfg: str
    pgid: int
    state: str = "running"  # running | unconfirmed (termination not confirmed; retried later)


_OWNED: dict[int, _Owned] = {}  # pid -> record for every sshd THIS process started
DISOWNED: list[str] = []  # records dropped WITHOUT signalling (ownership not established); diagnostics only
_cleaning = False
_term_started = False
_spawning = False
_pending_term = False
TERM_GRACE = 8.0
KILL_GRACE = 5.0


class ProcessTableError(RuntimeError):
    """`ps` failed or printed something unparseable: never interpreted as 'no processes'."""


_PS_ENV = {**os.environ, "LC_ALL": "C"}  # only handed to ps; the test process itself is untouched


def ps_table() -> list[tuple[int, int, int, str, str]]:
    """(pid, ppid, pgid, stat, command) of every process, full width (-ww)."""
    try:
        r = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,pgid=,stat=,command="], capture_output=True, text=True,
                           timeout=30, env=_PS_ENV)
    except (OSError, subprocess.SubprocessError) as e:
        raise ProcessTableError(f"ps could not run: {e!r}") from e
    if r.returncode != 0 or not r.stdout.strip():
        raise ProcessTableError(f"ps failed rc={r.returncode} stderr={r.stderr.strip()[:200]!r}")
    rows = []
    for ln in r.stdout.splitlines():
        if not ln.strip():
            continue
        f = ln.split(None, 4)
        try:
            rows.append((int(f[0]), int(f[1]), int(f[2]), f[3], f[4] if len(f) > 4 else ""))
        except (ValueError, IndexError) as e:
            raise ProcessTableError(f"unparseable ps line: {ln[:80]!r}") from e
    return rows


def _command_of(pid: int) -> str | None:
    """Diagnostic only (never an ownership decision). None if unavailable."""
    try:
        r = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, timeout=10,
                           env=_PS_ENV)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _leader_state(proc: subprocess.Popen) -> str:
    """'running' | 'exited' (zombie, still unreaped) | 'reaped' | 'unknown'. Never reaps."""
    if proc.returncode is not None:
        return "reaped"
    if not hasattr(os, "waitid"):
        return "unknown"
    try:
        r = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return "reaped"  # someone else reaped it
    except OSError:
        return "unknown"
    return "running" if r is None else "exited"


def _signal_group(rec: _Owned, sig: int) -> bool:
    """killpg only while the leader is provably unreaped and the group is the one we created."""
    pgid = rec.pgid
    if pgid <= 1 or pgid == os.getpgrp() or pgid != rec.proc.pid:
        return False
    st = _leader_state(rec.proc)
    if st == "reaped" or (st == "unknown" and rec.proc.returncode is not None):
        return False
    try:
        actual = os.getpgid(rec.proc.pid)
    except ProcessLookupError:
        actual = pgid  # zombie leader on some platforms
    if actual != pgid:
        return False
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    return True


def _terminate(rec: _Owned) -> str:
    """Stop the owned group and reap its leader.
    'reaped' (record may be released) | 'unconfirmed' (retry later) | 'disowned' (never signalled)."""
    proc = rec.proc
    if _leader_state(proc) == "reaped":
        proc.wait(timeout=0)  # sync returncode if someone else reaped; never signal a stale pid/pgid
        return "reaped"
    if not _signal_group(rec, signal.SIGTERM):
        DISOWNED.append(f"pid={proc.pid} cfg={rec.cfg}: ownership not established, nothing signalled")
        return "disowned"
    deadline = time.monotonic() + TERM_GRACE
    if hasattr(os, "waitid"):
        while _leader_state(proc) == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
    else:
        time.sleep(1.0)
    _signal_group(rec, signal.SIGKILL)  # leader still unreaped here, so the group id is still ours
    if hasattr(os, "waitid"):
        end = time.monotonic() + 1.0
        while _leader_state(proc) == "running" and time.monotonic() < end:
            time.sleep(0.02)
    _signal_group(rec, signal.SIGKILL)  # sweep members forked while the first kill was being delivered
    try:
        proc.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        return "unconfirmed"
    return "reaped"


def reap_owned() -> int:
    """Terminate every sshd this process started (owned Popen leader + its own session group).
    Returns how many records were terminated. Idempotent and re-entrancy safe."""
    global _cleaning
    if _cleaning:
        return 0
    _cleaning = True
    done = 0
    try:
        for pid, rec in list(_OWNED.items()):
            try:
                result = _terminate(rec)
            except Exception as e:  # keep going: other servers still need cleanup
                rec.state = "unconfirmed"
                DISOWNED.append(f"pid={pid}: cleanup error {e!r}")
                continue
            if result == "unconfirmed":
                rec.state = "unconfirmed"  # kept: a later cleanup re-evaluates (leader still unreaped)
                continue
            _OWNED.pop(pid, None)  # reaped, or quarantined as not ours (reported in DISOWNED)
            done += result == "reaped"
    finally:
        _cleaning = False
    return done


def owned_sshd_alive_under(root: str) -> list[str]:
    """Independent leftover detector (does NOT consult _OWNED): any live sshd (anyone's) whose
    config lives under `root`. Raises ProcessTableError if the process table is unavailable."""
    return [
        f"pid={pid} ppid={ppid} pgid={pgid} stat={stat} {cmd}"
        for pid, ppid, pgid, stat, cmd in ps_table()
        if not stat.startswith("Z") and "sshd" in cmd and "sshiso/etc/sshd_" in cmd and root in cmd
    ]


atexit.register(reap_owned)
_prev_term = signal.getsignal(signal.SIGTERM)


def _on_term(signum, frame):
    global _term_started, _pending_term
    if _spawning:
        _pending_term = True  # re-delivered by start() once the new child is registered
        return
    if _term_started:
        return  # second SIGTERM while cleaning: let the running cleanup finish
    _term_started = True
    try:
        reap_owned()
    except BaseException as e:
        try:
            os.write(2, f"hostkey_support: cleanup failed: {type(e).__name__}\n".encode())
        except OSError:
            pass
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)  # same exit semantics as before: killed by SIGTERM (-15)


try:
    if _prev_term in (signal.SIG_DFL, None):
        signal.signal(signal.SIGTERM, _on_term)
except ValueError:  # not the main thread
    pass


class Sshd:
    """Loopback-only, user-privilege, pubkey-only sshd inside the sandbox,
    started through the forced wrapper."""

    def __init__(self, rt: iso.SshRuntime, name: str, host_key: dict, port: int | None = None, algorithms: str | None = None):
        self.rt, self.name, self.port = rt, name, port or free_port()
        self.host_key = host_key
        d = Path(rt.sandbox) / "etc"
        self.cfg = d / f"sshd_{name}.conf"
        auth = Path(rt.sandbox) / f"authorized_{name}"
        auth.write_text(Path(rt.identity + ".pub").read_text())
        os.chmod(auth, 0o600)
        lines = [
            f"Port {self.port}", "ListenAddress 127.0.0.1", f"HostKey {host_key['path']}",
            f"AuthorizedKeysFile {auth}", f"PidFile {Path(rt.sandbox) / ('sshd_' + name + '.pid')}",
            "PasswordAuthentication no", "PubkeyAuthentication yes", "UsePAM no", "StrictModes no",
            "KbdInteractiveAuthentication no", "LogLevel VERBOSE",
        ]
        if algorithms:
            lines.append(f"HostKeyAlgorithms {algorithms}")
        self.cfg.write_text("\n".join(lines) + "\n")
        self.proc = None

    def start(self):
        global _spawning, _pending_term
        self.log = open(Path(self.rt.sandbox) / f"sshd_{self.name}.log", "wb")
        _spawning = True
        try:
            try:
                self.proc = subprocess.Popen(
                    [self.rt.wrapper("sshd"), "-D", "-e", "-f", str(self.cfg)], stdout=self.log, stderr=self.log,
                    start_new_session=True,
                )
                _OWNED[self.proc.pid] = _Owned(self.proc, str(self.cfg), self.proc.pid)
            except BaseException:
                self.log.close()
                raise
        finally:
            _spawning = False
            if _pending_term:
                _pending_term = False
                os.kill(os.getpid(), signal.SIGTERM)  # handler now sees the registered child
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    socket.create_connection(("127.0.0.1", self.port), timeout=0.3).close()
                    return self
                except OSError:
                    if _leader_state(self.proc) != "running":
                        raise AssertionError("sshd exited: " + Path(self.log.name).read_text())
                    time.sleep(0.1)
            raise AssertionError("sshd did not start")
        except BaseException:
            self.stop()  # never leave a half-started server behind
            raise

    def stop(self):
        rec = self.proc and _OWNED.get(self.proc.pid)
        confirmed = True
        if rec is not None:
            confirmed = _terminate(rec) != "unconfirmed"
        # wait until the port is really closed
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                time.sleep(0.1)
            except OSError:
                break
        try:
            self.log.close()
        except Exception:
            pass
        if rec is not None and confirmed:
            _OWNED.pop(self.proc.pid, None)
        if not confirmed:
            raise AssertionError(f"could not confirm termination of sshd pid={self.proc.pid}")

    def stopped(self) -> bool:
        return self.proc is None or self.proc.poll() is not None


def run_ssh_pty(rt: iso.SshRuntime, args: list[str], answers: list[tuple[str, str]] | None = None, timeout: float = 20):
    """Run the wrapped ssh on a real pty; answer prompts by substring. Returns (output, rc)."""
    import pty
    import select

    argv = [rt.wrapper("ssh"), *iso.required_ssh_args(rt), *args]
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(argv[0], argv)
    out = b""
    answers = list(answers or [])
    deadline = time.monotonic() + timeout
    rc = None
    while time.monotonic() < deadline:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                data = os.read(fd, 4096)
            except OSError:
                data = b""
            if not data:
                break
            out += data
            text = out.decode("utf-8", "replace")
            if answers and answers[0][0] in text:
                os.write(fd, (answers.pop(0)[1] + "\n").encode())
    try:
        _, status = os.waitpid(pid, os.WNOHANG)
        if _ == 0:
            os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
        rc = os.waitstatus_to_exitcode(status)
    except ChildProcessError:
        rc = None
    return out.decode("utf-8", "replace"), rc
