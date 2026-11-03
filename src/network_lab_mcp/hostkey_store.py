# SPDX-License-Identifier: Apache-2.0
"""known_hosts state classification and safe single-entry removal.

All OpenSSH interaction uses argument lists (never a shell) and the pinned
``ssh-keygen`` (the forced test wrapper when an isolated runtime is
installed, otherwise the system binary resolved to an absolute path).

Adding a key is `hostkey_tofu`'s job (TOFU of an unknown host, product file
only). This module classifies state, takes the product known_hosts lock,
backs up, and removes the entries of exactly one host identifier after a
human approved the replacement (human CLI only).
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from network_lab_mcp import ssh_isolation

UNKNOWN_CANDIDATE = "UNKNOWN_CANDIDATE"
REGISTERED_UNVERIFIED = "REGISTERED_UNVERIFIED"
UNDETERMINED = "UNDETERMINED"

_KEYGEN_TIMEOUT = 20
_FOUND_RE = re.compile(r"found: line (\d+)")


class HostKeyStoreError(Exception):
    """Fail-closed error. Messages never contain key material."""


@dataclass(frozen=True)
class RegisteredKey:
    key_type: str
    fingerprint: str  # SHA256:<43 chars>
    line_no: int
    hashed: bool


@dataclass(frozen=True)
class KnownHostsState:
    state: str
    keys: tuple[RegisteredKey, ...] = ()
    reason: str = ""


def host_identifier(host: str, port: int) -> str:
    """The identifier OpenSSH stores: bare host for port 22, [host]:port otherwise."""
    return host if int(port) == 22 else f"[{host}]:{int(port)}"


def sha256_fingerprint(blob_b64: str) -> str:
    raw = base64.b64decode(blob_b64.encode("ascii"), validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")


def keygen_path() -> str:
    runtime = ssh_isolation.active_runtime()
    if runtime is not None:
        return runtime.wrapper("ssh-keygen")
    found = shutil.which("ssh-keygen")
    if not found:
        raise HostKeyStoreError("ssh-keygen is not available")
    return os.path.realpath(found)


def run_keygen(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [keygen_path(), *args], capture_output=True, text=True, timeout=_KEYGEN_TIMEOUT, shell=False
    )


_KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
              "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")


def _parse_found(output: str) -> tuple[list[RegisteredKey], bool]:
    """Parse `ssh-keygen -F` output. Returns (keys, has_marker_or_pattern)."""
    keys: list[RegisteredKey] = []
    line_no = 0
    unsafe = False
    for raw in output.splitlines():
        if raw.startswith("# Host ") and " found: line " in raw:
            m = _FOUND_RE.search(raw)
            line_no = int(m.group(1)) if m else 0
            continue
        if not raw.strip() or raw.startswith("#"):
            continue
        fields = raw.split()
        if fields and fields[0].startswith("@"):
            unsafe = True  # @cert-authority / @revoked
            fields = fields[1:]
        if len(fields) < 3 or fields[1] not in _KEY_TYPES:
            unsafe = True
            continue
        try:
            fp = sha256_fingerprint(fields[2])
        except (ValueError, UnicodeError):
            unsafe = True
            continue
        keys.append(RegisteredKey(fields[1], fp, line_no, fields[0].startswith("|1|")))
    return keys, unsafe


def _file_state(path: str) -> str:
    """'missing' | 'ok' | 'unreadable'."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if not stat.S_ISREG(st.st_mode) or not os.access(path, os.R_OK):
        return "unreadable"
    return "ok"


def lookup(identifier: str, known_hosts: str) -> KnownHostsState:
    fs = _file_state(known_hosts)
    if fs == "missing":
        return KnownHostsState(UNKNOWN_CANDIDATE, (), "file missing")
    if fs == "unreadable":
        return KnownHostsState(UNDETERMINED, (), "file unreadable")
    result = run_keygen(["-F", identifier, "-f", known_hosts])
    if result.returncode == 0:
        keys, unsafe = _parse_found(result.stdout)
        if unsafe or not keys:
            return KnownHostsState(UNDETERMINED, tuple(keys), "unsupported entry (marker or key type)")
        return KnownHostsState(REGISTERED_UNVERIFIED, tuple(keys))
    if result.returncode == 1:
        return KnownHostsState(UNKNOWN_CANDIDATE, (), "no entry")
    return KnownHostsState(UNDETERMINED, (), "ssh-keygen failed")


def classify(host: str, port: int, known_hosts: str, global_known_hosts: str | None = None) -> KnownHostsState:
    """UNKNOWN_CANDIDATE only when neither the user nor the global file
    holds an entry; any entry makes it REGISTERED_UNVERIFIED (verified by the
    real connection under StrictHostKeyChecking=yes, never assumed)."""
    ident = host_identifier(host, port)
    user = lookup(ident, known_hosts)
    if user.state == UNDETERMINED:
        return user
    if global_known_hosts:
        glob = lookup(ident, global_known_hosts)
        if glob.state == UNDETERMINED:
            return glob
        if glob.state == REGISTERED_UNVERIFIED:
            return KnownHostsState(REGISTERED_UNVERIFIED, user.keys + glob.keys, "global")
    return user


# --------------------------------------------------------------------------
# File safety, backup, lock
# --------------------------------------------------------------------------


def check_known_hosts_file(path: str) -> None:
    """Owner, type, permissions and symlink checks before any modification."""
    if os.path.islink(path):
        raise HostKeyStoreError("known_hosts is a symlink; refusing to modify it")
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise HostKeyStoreError("known_hosts is not a regular file")
    if st.st_uid != os.getuid():
        raise HostKeyStoreError("known_hosts is not owned by the current user")
    if stat.S_IMODE(st.st_mode) & 0o022:
        raise HostKeyStoreError("known_hosts is group/world writable")
    parent = os.stat(os.path.dirname(path) or ".")
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o022:
        raise HostKeyStoreError("known_hosts directory is not private to the current user")


def file_signature(path: str) -> tuple:
    st = os.stat(path)
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return (st.st_size, st.st_mtime_ns, st.st_ino, digest)


def default_backup_dir() -> str:
    home = os.path.expanduser("~")  # password-database based, not $HOME
    import pwd

    home = pwd.getpwuid(os.getuid()).pw_dir or home
    if os.uname().sysname == "Darwin":
        return os.path.join(home, "Library", "Application Support", "Network Lab MCP", "ssh-hostkey-backups")
    base = os.path.join(home, ".local", "state")
    return os.path.join(base, "network-lab-mcp", "ssh-hostkey-backups")


def make_backup(known_hosts: str, backup_dir: str) -> str:
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    name = f"known_hosts.{time.strftime('%Y%m%dT%H%M%S')}.{secrets.token_hex(4)}.bak"
    dest = os.path.join(backup_dir, name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(dest, flags, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(Path(known_hosts).read_bytes())
    return dest


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class KnownHostsLock:
    """In-process + flock exclusion for one known_hosts file (lock file lives
    in `lock_dir`, never next to the user's known_hosts)."""

    wait_seconds = 90.0  # >= one probe (30 s) plus registration of the process that holds it

    def __init__(self, known_hosts: str, lock_dir: str):
        self._key = os.path.realpath(known_hosts)
        self._lock_dir = lock_dir
        self._fd = None

    def __enter__(self):
        with _locks_guard:
            lock = _locks.setdefault(self._key, threading.Lock())
        if not lock.acquire(timeout=self.wait_seconds):
            raise HostKeyStoreError("another host-key operation is running for this known_hosts")
        self._thread_lock = lock
        try:
            os.makedirs(self._lock_dir, mode=0o700, exist_ok=True)
            name = hashlib.sha256(self._key.encode()).hexdigest()[:16] + ".lock"
            self._fd = os.open(os.path.join(self._lock_dir, name), os.O_CREAT | os.O_RDWR, 0o600)
            deadline = time.monotonic() + self.wait_seconds
            while True:  # another process may hold it for one probe; wait, but never forever
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise HostKeyStoreError("known_hosts is locked by another process") from None
                    time.sleep(0.05)
        except BaseException:
            self._release()
            raise
        return self

    def _release(self):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        self._thread_lock.release()

    def __exit__(self, *exc):
        self._release()
        return False


# --------------------------------------------------------------------------
# Single-host removal
# --------------------------------------------------------------------------


def _matched_lines(identifier: str, known_hosts: str) -> set[int]:
    result = run_keygen(["-F", identifier, "-f", known_hosts])
    if result.returncode != 0:
        raise HostKeyStoreError("host entry not found")
    lines = set()
    for raw in result.stdout.splitlines():
        if raw.startswith("# Host ") and " found: line " in raw:
            m = _FOUND_RE.search(raw)
            if m:
                lines.add(int(m.group(1)))
    if not lines:
        raise HostKeyStoreError("host entry location could not be determined")
    return lines


def _count(lines: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for ln in lines:
        out[ln] = out.get(ln, 0) + 1
    return out


def _atomic_write(path: str, data: bytes) -> None:
    tmp = path + f".nlm-tmp-{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def remove_host(identifier: str, known_hosts: str, backup_dir: str, *, expect_signature: tuple | None = None) -> str:
    """Remove every entry of exactly `identifier`; returns the backup path.

    Refuses (nothing modified) if a matched line is shared with other host
    patterns, uses wildcards/negation/markers, or if the file changed since
    `expect_signature` or between our read and the removal. The backup is
    **never written back wholesale**: another process may have added trust
    entries meanwhile. After `ssh-keygen -R` every original line that was not
    matched must still be present (other processes' additions are kept);
    anything else stops with the backup path for manual recovery."""
    check_known_hosts_file(known_hosts)
    if expect_signature is not None and file_signature(known_hosts) != expect_signature:
        raise HostKeyStoreError("known_hosts changed since it was inspected")
    sig_at_read = file_signature(known_hosts)
    original = Path(known_hosts).read_bytes()
    old_lines = original.decode("utf-8", "surrogateescape").split("\n")
    matched = _matched_lines(identifier, known_hosts)
    for n in matched:
        if n < 1 or n > len(old_lines):
            raise HostKeyStoreError("host entry location is inconsistent")
        fields = old_lines[n - 1].split()
        if not fields or fields[0].startswith("@"):
            raise HostKeyStoreError("matched entry uses a marker; refusing automatic removal")
        hostfield = fields[0]
        if not hostfield.startswith("|1|") and ("," in hostfield or any(c in hostfield for c in "*?!")):
            raise HostKeyStoreError("matched entry is shared or a pattern; refusing automatic removal")
    try:
        backup = make_backup(known_hosts, backup_dir)
    except OSError:
        raise HostKeyStoreError("backup could not be created; known_hosts was not modified") from None
    # Last check before the destructive step: no other process changed or swapped the file.
    check_known_hosts_file(known_hosts)
    if file_signature(known_hosts) != sig_at_read:
        raise HostKeyStoreError(f"known_hosts changed before removal; nothing was modified (backup: {backup})")
    result = run_keygen(["-R", identifier, "-f", known_hosts])
    current = Path(known_hosts).read_bytes()
    if result.returncode != 0:
        if current == original:
            raise HostKeyStoreError(f"ssh-keygen -R failed; known_hosts is unchanged (backup: {backup})")
        raise HostKeyStoreError(
            "ssh-keygen -R failed and known_hosts changed; it was NOT restored automatically so that other "
            f"changes are not lost. Review it manually (backup: {backup})."
        )
    cur_lines = current.decode("utf-8", "surrogateescape").split("\n")
    expected = [ln for i, ln in enumerate(old_lines, 1) if i not in matched]
    have, want = _count(cur_lines), _count(expected)
    missing = [ln for ln, n in want.items() if ln and have.get(ln, 0) < n]
    if missing:
        # Never "repair" by writing lines back: whoever removed them may have meant to. Stop for a human.
        raise HostKeyStoreError(
            "unrelated known_hosts entries are missing after removal; nothing was written back automatically "
            f"so other changes are not lost. Review it manually (backup: {backup})."
        )
    if lookup(identifier, known_hosts).state != UNKNOWN_CANDIDATE:
        raise HostKeyStoreError(f"entries of the host are still present after removal (backup: {backup})")
    return backup
