# SPDX-License-Identifier: Apache-2.0
"""Helpers for tests that use the REAL OpenSSH binaries, always through the
forced isolation wrapper and a per-test sandbox (never the user's ~/.ssh)."""

from __future__ import annotations

import os
import shutil
import subprocess
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


_OWNED: dict[int, str] = {}  # pid -> sandbox cfg path of every sshd THIS process started


def _command_of(pid: int) -> str:
    r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
    return r.stdout.strip()


def reap_owned() -> int:
    """Kill only sshd processes this process started (pid recorded at spawn AND
    still running our exact sandbox config). Returns how many were killed."""
    killed = 0
    for pid, cfg in list(_OWNED.items()):
        cmd = _command_of(pid)
        if cfg in cmd and "sshd" in cmd:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pid, sig)
                except (ProcessLookupError, PermissionError):
                    try:
                        os.kill(pid, sig)
                    except (ProcessLookupError, PermissionError):
                        pass
                time.sleep(0.2)
                if cfg not in _command_of(pid):
                    break
            killed += 1
        _OWNED.pop(pid, None)
    return killed


def owned_sshd_alive_under(root: str) -> list[str]:
    """Any sshd (anyone's) whose config lives under `root` -- used to prove cleanup."""
    r = subprocess.run(["ps", "-axo", "pid,command"], capture_output=True, text=True)
    return [ln for ln in r.stdout.splitlines() if "sshd" in ln and "sshiso/etc/sshd_" in ln and root in ln]


import atexit

atexit.register(reap_owned)
_prev_term = signal.getsignal(signal.SIGTERM)


def _on_term(signum, frame):
    reap_owned()
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)


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
        self.log = open(Path(self.rt.sandbox) / f"sshd_{self.name}.log", "wb")
        try:
            self.proc = subprocess.Popen(
                [self.rt.wrapper("sshd"), "-D", "-e", "-f", str(self.cfg)], stdout=self.log, stderr=self.log,
                start_new_session=True,
            )
        except BaseException:
            self.log.close()
            raise
        _OWNED[self.proc.pid] = str(self.cfg)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    socket.create_connection(("127.0.0.1", self.port), timeout=0.3).close()
                    return self
                except OSError:
                    if self.proc.poll() is not None:
                        raise AssertionError("sshd exited: " + Path(self.log.name).read_text())
                    time.sleep(0.1)
            raise AssertionError("sshd did not start")
        except BaseException:
            self.stop()  # never leave a half-started server behind
            raise

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=5)
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
        if self.proc is not None:
            _OWNED.pop(self.proc.pid, None)

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
