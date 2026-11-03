# SPDX-License-Identifier: Apache-2.0
"""Lab-oriented TOFU (trust on first use) for SSH host keys.

Method: *pre-register, then strict connect*.

1. The hop's host identifier is looked up in the product known_hosts, the
   user's known_hosts and the global known_hosts. Anything registered is left
   alone (never replaced, never extended).
2. An unknown hop gets exactly one **authentication-free probe**: OpenSSH's own
   client connects with the dummy user ``nlm-hostkey-probe`` and every
   authentication method except ``none`` disabled, no multiplexing, no
   session, and ``StrictHostKeyChecking=accept-new`` against a *temporary*
   known_hosts. OpenSSH therefore records the key it negotiated; no credential
   of the lab is ever sent on this connection.
3. The recorded key is validated (exactly one entry, expected host identifier,
   supported type, SHA256 fingerprint cross-checked with ssh-keygen) and
   appended to the **product-owned** known_hosts under a dedicated lock, then
   re-read and compared with the probed key. Only then may a credential-
   carrying connection start; it always runs with
   ``StrictHostKeyChecking=yes`` and ``UpdateHostKeys=no`` on every hop.

This module never removes, replaces or edits an existing entry, and never
asks a human anything: replacement of a registered key is the human CLI's job
(the human CLI flow module). It is the only host-key module the terminal layer imports,
which is why it must stay free of approval and removal code (a test checks
this).

TOFU limit: the first key is obtained over the network and cannot be proven
authentic. A man in the middle during the first connection would be trusted.
This is accepted for lab use.
"""

from __future__ import annotations

import base64
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from network_lab_mcp import hostkey_store as hs
from network_lab_mcp import ssh_isolation

PROBE_USER = "nlm-hostkey-probe"
PROBE_TIMEOUT_SECONDS = 30
PROBE_CONNECT_TIMEOUT_SECONDS = 15
KEY_GRACE_SECONDS = 5.0  # a key was recorded but ssh has not exited yet
MAX_JUMP_PASSWORD_SENDS = 40  # absolute cap for probe connections per run

# Error classes (the first element of every message so logs and tests can key on it).
HOST_KEY_UNKNOWN = "HOST_KEY_UNKNOWN"
HOST_KEY_CHANGED = "HOST_KEY_CHANGED"
KEY_TYPE_MISMATCH = "KEY_TYPE_MISMATCH"
HOST_KEY_ALGORITHM_MISMATCH = "HOST_KEY_ALGORITHM_MISMATCH"
SSH_KEX_MISMATCH = "SSH_KEX_MISMATCH"
TOFU_PROBE_ERROR = "TOFU_PROBE_ERROR"
TOFU_STORAGE_ERROR = "TOFU_STORAGE_ERROR"
TOFU_IDENTITY_ERROR = "TOFU_IDENTITY_ERROR"
JUMP_AUTH_ERROR = "JUMP_AUTH_ERROR"
SSH_AUTH_ERROR = "SSH_AUTH_ERROR"


class HostKeyState(Enum):
    PENDING = "PENDING"
    REGISTERED_UNVERIFIED = "REGISTERED_UNVERIFIED"
    VERIFIED = "VERIFIED"
    APPROVED = "APPROVED"
    TRUSTED = "TRUSTED"
    FAILED = "FAILED"


class AuthState(Enum):
    AUTH_NOT_STARTED = "AUTH_NOT_STARTED"
    JUMP_AUTH = "JUMP_AUTH"
    TARGET_AUTH = "TARGET_AUTH"
    AUTH_COMPLETED = "AUTH_COMPLETED"
    AUTH_FAILED = "AUTH_FAILED"


class HostKeyError(Exception):
    """Fail-closed; messages never contain credentials or key blobs."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message if code is None else f"[{code}] {message}")
        self.code = code


class HostKeyChanged(HostKeyError):
    def __init__(self, device: str, hop: str, host: str, port: int):
        super().__init__(
            f"Device '{device}': the SSH host key of the {hop} ({host}:{port}) has changed.", HOST_KEY_CHANGED
        )
        self.device, self.hop, self.host, self.port = device, hop, host, port


@dataclass
class Hop:
    role: str  # "jump" | "target"
    host: str
    port: int
    user: str | None
    host_state: HostKeyState = HostKeyState.PENDING
    auth_state: AuthState = AuthState.AUTH_NOT_STARTED

    def may_authenticate(self) -> bool:
        """Credentials may be sent for this hop only once its host key is trusted."""
        return self.host_state in (HostKeyState.TRUSTED, HostKeyState.REGISTERED_UNVERIFIED)

    def advance_auth(self, new: AuthState) -> None:
        if new in (AuthState.JUMP_AUTH, AuthState.TARGET_AUTH) and not self.may_authenticate():
            raise HostKeyError("authentication refused: the host key of this hop is not trusted")
        self.auth_state = new


def _terminal():
    from network_lab_mcp import terminal  # lazy: terminal imports this module

    return terminal


def hops_for(device_config: dict) -> list[Hop]:
    """The SSH hops of a device, derived only from its access-info (this is
    what limits TOFU to configured devices and jump servers)."""
    term = _terminal()
    transport = device_config.get("transport")
    jump = device_config.get("jump_host_config")
    hops: list[Hop] = []
    if transport == "ssh":
        if jump:
            target, port = term._ssh_target(jump, 22)
            user, _, host = target.rpartition("@")
            hops.append(Hop("jump", host, port, user or None))
        target, port = term._ssh_target(device_config, 22)
        user, _, host = target.rpartition("@")
        hops.append(Hop("target", host, port, user or None))
    elif transport == "telnet" and jump:
        target, port = term._ssh_target(jump, 22)
        user, _, host = target.rpartition("@")
        hops.append(Hop("jump", host, port, user or None))
    return hops


def hop_label(device_config: dict, role: str) -> str:
    return "direct" if role == "target" and not device_config.get("jump_host_config") else role


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def _home() -> str:
    import pwd

    return pwd.getpwuid(os.getuid()).pw_dir  # password database, not $HOME


def product_known_hosts_path() -> str:
    """The only file TOFU registration writes (0600, in a 0700 directory)."""
    runtime = ssh_isolation.active_runtime()
    if runtime is not None:
        return runtime.product_known_hosts
    home = _home()
    if os.uname().sysname == "Darwin":
        return os.path.join(home, "Library", "Application Support", "Network Lab MCP", "ssh", "known_hosts")
    return os.path.join(home, ".local", "state", "network-lab-mcp", "ssh", "known_hosts")


def user_known_hosts_path() -> str:
    """The user's own file: consulted, never modified by the product."""
    runtime = ssh_isolation.active_runtime()
    return runtime.known_hosts if runtime is not None else os.path.join(_home(), ".ssh", "known_hosts")


def global_known_hosts_path() -> str | None:
    runtime = ssh_isolation.active_runtime()
    return runtime.global_known_hosts if runtime is not None else "/etc/ssh/ssh_known_hosts"


def default_files() -> list[str]:
    return [product_known_hosts_path(), user_known_hosts_path()]


def known_hosts_option(files: list[str]) -> str:
    """`UserKnownHostsFile` value: one or more double-quoted absolute paths."""
    if not files:
        raise HostKeyError("no known_hosts file", TOFU_STORAGE_ERROR)
    for f in files:
        if not os.path.isabs(f) or re.search(r'[\x00-\x1f\x7f"%$`\\]', f):
            raise HostKeyError("known_hosts path is not a plain absolute path", TOFU_STORAGE_ERROR)
    return "UserKnownHostsFile=" + " ".join(f'"{f}"' for f in files)


def lock_dir_for(known_hosts: str) -> str:
    return os.path.join(os.path.dirname(known_hosts), "locks")


# --------------------------------------------------------------------------
# ssh argv construction
# --------------------------------------------------------------------------


def ssh_prefix(kh: str | None = None) -> list[str]:
    runtime = ssh_isolation.active_runtime()
    if runtime is not None:
        return [runtime.wrapper("ssh"), *ssh_isolation.required_ssh_args(runtime, kh)]
    found = shutil.which("ssh")
    if not found:
        raise HostKeyError("ssh is not available", TOFU_PROBE_ERROR)
    return [os.path.realpath(found)]


def strict_opts(files: list[str] | None = None) -> list[str]:
    """Options of every credential-carrying hop: strict verification against
    the product + user known_hosts, no key additions, no multiplexing (a
    master connection cannot stand in for verification of this one)."""
    return [
        "-o", "StrictHostKeyChecking=yes", "-o", known_hosts_option(files or default_files()),
        "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no", "-o", "VerifyHostKeyDNS=no",
        "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ControlPersist=no",
    ]


def probe_opts(tmp_kh: str) -> list[str]:
    return [
        "-o", "StrictHostKeyChecking=accept-new", "-o", known_hosts_option([tmp_kh]),
        "-o", "PreferredAuthentications=none", "-o", "NumberOfPasswordPrompts=0",
        "-o", "PubkeyAuthentication=no", "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no", "-o", "HostbasedAuthentication=no",
        "-o", "GSSAPIAuthentication=no", "-o", "IdentityAgent=none", "-o", "BatchMode=yes",
        "-o", "UpdateHostKeys=no", "-o", "HashKnownHosts=no", "-o", "CheckHostIP=no",
        "-o", "VerifyHostKeyDNS=no", "-o", "ControlMaster=no", "-o", "ControlPath=none",
        "-o", "ControlPersist=no", "-o", "SessionType=none", "-o", "PermitLocalCommand=no",
        "-o", f"ConnectTimeout={PROBE_CONNECT_TIMEOUT_SECONDS}", "-o", "LogLevel=VERBOSE",
    ]


def build_ssh_command(device_config: dict, *, files: list[str] | None = None) -> list[str]:
    """argv of a credential-carrying connection: strict on every hop. With a
    jump host the jump hop is a structured `-o ProxyCommand=` child (OpenSSH's
    `-J` child does not inherit command-line `-o`; verified with OpenSSH 10.3)."""
    term = _terminal()
    target, port = term._ssh_target(device_config, 22)
    cmd = [*ssh_prefix()]
    jump = device_config.get("jump_host_config")
    if jump:
        jtarget, jport = term._ssh_target(jump, 22)
        child = [*ssh_prefix(), *strict_opts(files), "-W", "%h:%p", "-p", str(jport), jtarget]
        cmd += ["-o", "ProxyCommand=" + " ".join(shlex.quote(x) for x in child)]
    cmd += strict_opts(files) + ["-o", "BatchMode=no", "-p", str(port), target]
    return cmd


def build_probe_command(device_config: dict, hop_role: str, tmp_kh: str, files: list[str] | None = None) -> list[str]:
    """argv of the authentication-free probe of `hop_role`. For a target behind
    a jump host the jump hop is the real, strictly verified, authenticating
    child; only the probed hop gets the dummy user and no credential."""
    term = _terminal()
    hop = {h.role: h for h in hops_for(device_config)}[hop_role]
    cmd = [*ssh_prefix(tmp_kh), "-N", "-T"]
    jump = device_config.get("jump_host_config")
    if hop_role == "target" and jump:
        jtarget, jport = term._ssh_target(jump, 22)
        child = [*ssh_prefix(), *strict_opts(files), "-o", "BatchMode=no", "-W", "%h:%p", "-p", str(jport), jtarget]
        cmd += ["-o", "ProxyCommand=" + " ".join(shlex.quote(x) for x in child)]
    cmd += probe_opts(tmp_kh) + ["-p", str(hop.port), f"{PROBE_USER}@{hop.host}"]
    return cmd


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


def classify(host: str, port: int, files: list[str], global_kh: str | None) -> hs.KnownHostsState:
    """UNKNOWN_CANDIDATE only if no consulted file has any entry for the
    identifier. Any unreadable/odd file makes the answer UNDETERMINED (fail
    closed, never TOFU into an unclear situation)."""
    ident = hs.host_identifier(host, port)
    keys: list[hs.RegisteredKey] = []
    for path in [*files, *([global_kh] if global_kh else [])]:
        if os.path.islink(path):
            return hs.KnownHostsState(hs.UNDETERMINED, (), "a known_hosts file is a symlink")
        state = hs.lookup(ident, path)
        if state.state == hs.UNDETERMINED:
            return state
        if state.state == hs.REGISTERED_UNVERIFIED:
            keys.extend(state.keys)
    if keys:
        return hs.KnownHostsState(hs.REGISTERED_UNVERIFIED, tuple(keys))
    return hs.KnownHostsState(hs.UNKNOWN_CANDIDATE, (), "no entry")


# --------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------

_ALLOWED_KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521")
_KEY_LABELS = {"ssh-ed25519": "ED25519", "ssh-rsa": "RSA"}


def key_label(key_type: str) -> str:
    return _KEY_LABELS.get(key_type, "ECDSA" if key_type.startswith("ecdsa-") else key_type)


@dataclass(frozen=True)
class ProbedKey:
    key_type: str
    blob: str
    fingerprint: str

    @property
    def label(self) -> str:
        return key_label(self.key_type)


def _blob_type(blob_b64: str) -> str | None:
    try:
        raw = base64.b64decode(blob_b64.encode("ascii"), validate=True)
    except (ValueError, UnicodeError):
        return None
    if len(raw) < 4:
        return None
    n = int.from_bytes(raw[:4], "big")
    return raw[4:4 + n].decode("ascii", "replace") if 4 + n <= len(raw) else None


def read_probe_file(path: str, identifier: str) -> ProbedKey | None:
    """The single key OpenSSH recorded in the probe's temporary known_hosts.
    None when nothing complete is there yet. Anything unexpected (several
    entries, another host identifier, hashed entry, unsupported type, bad
    blob) is an error, never a guess."""
    try:
        data = open(path, "rb").read().decode("utf-8", "replace")
    except FileNotFoundError:
        return None
    if not data.strip():
        return None
    if not data.endswith("\n"):
        return None  # still being written
    lines = [ln for ln in data.splitlines() if ln.strip() and not ln.startswith("#")]
    if len(lines) != 1:
        raise HostKeyError("the probe recorded an unexpected number of host keys; nothing was registered", TOFU_PROBE_ERROR)
    fields = lines[0].split()
    if len(fields) < 3 or fields[0].startswith("@"):
        raise HostKeyError("the probe recorded an unsupported entry; nothing was registered", TOFU_PROBE_ERROR)
    if fields[0] != identifier:
        raise HostKeyError(
            "the probe recorded the key under a different host identifier (HostKeyAlias, HostName or hashing in the "
            "ssh configuration?); nothing was registered", TOFU_IDENTITY_ERROR)
    ktype, blob = fields[1], fields[2]
    if ktype not in _ALLOWED_KEY_TYPES or _blob_type(blob) != ktype:
        raise HostKeyError("the host key type is not supported; nothing was registered", TOFU_PROBE_ERROR)
    try:
        fp = hs.sha256_fingerprint(blob)
    except (ValueError, UnicodeError):
        raise HostKeyError("the recorded host key is malformed; nothing was registered", TOFU_PROBE_ERROR) from None
    check = hs.run_keygen(["-l", "-f", path])
    if check.returncode != 0 or fp not in check.stdout:
        raise HostKeyError("the recorded host key could not be cross-checked; nothing was registered", TOFU_PROBE_ERROR)
    return ProbedKey(ktype, blob, fp)


def _spawn_pty(argv: list[str]):
    import warnings

    with warnings.catch_warnings():  # the child only execs; the multi-threaded-fork warning does not apply
        warnings.simplefilter("ignore", DeprecationWarning)
        pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child
        try:
            os.execv(argv[0], argv)
        finally:
            os._exit(127)
    return pid, fd


def _kill(pid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        for _ in range(20):
            try:
                done, _s = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
            if done:
                return
            time.sleep(0.05)


def _reap(pid: int) -> None:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


_SSH_ERRORS = (
    (re.compile(r"no matching host key type found"), HOST_KEY_ALGORITHM_MISMATCH,
     "no host key algorithm is common to the client and the device. If the device only offers a legacy "
     "algorithm, enable it for this host in your ssh_config (HostKeyAlgorithms); it is never enabled automatically"),
    (re.compile(r"no matching key exchange method found"), SSH_KEX_MISMATCH,
     "no key-exchange algorithm is common to the client and the device (KexAlgorithms in your ssh_config)"),
    (re.compile(r"Connection refused"), TOFU_PROBE_ERROR, "the connection was refused"),
    (re.compile(r"Connection timed out|Operation timed out|timed out"), TOFU_PROBE_ERROR, "the connection timed out"),
    (re.compile(r"Connection reset|Connection closed by"), TOFU_PROBE_ERROR,
     "the connection was closed or reset before a host key was recorded (this can be a login restriction, AAA "
     "or rate limit on the device, but the cause is not known)"),
    (re.compile(r"Could not resolve hostname|Name or service not known"), TOFU_PROBE_ERROR, "the name could not be resolved"),
    (re.compile(r"No route to host|Network is unreachable"), TOFU_PROBE_ERROR, "the host is not reachable"),
)


def classify_ssh_failure(text: str) -> tuple[str, str] | None:
    """(code, short description) for a recognised OpenSSH failure message."""
    for rx, code, desc in _SSH_ERRORS:
        if rx.search(text):
            return code, desc
    return None


def probe_host_key(
    device_name: str, device_config: dict, hop_role: str, *, files: list[str] | None = None,
    counters: dict | None = None, timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbedKey:
    """Run the authentication-free probe of one hop and return the key OpenSSH
    recorded. At most one probe per call; there is no retry. A jump password
    (jump hop of a target probe only) is sent at most once, attributed through
    the same prompt classification as the managed login and counted in
    `counters`. The temporary directory, files and the ssh process are always
    cleaned up."""
    term = _terminal()
    hops = {h.role: h for h in hops_for(device_config)}
    if hop_role not in hops:
        raise HostKeyError("no such hop", TOFU_IDENTITY_ERROR)
    hop = hops[hop_role]
    ident = hs.host_identifier(hop.host, hop.port)
    jump_cfg = device_config.get("jump_host_config")
    jump_key = ((jump_cfg or {}).get("address"), (jump_cfg or {}).get("port", 22))
    jump_failed: set = counters.setdefault("jump_failed", set()) if counters is not None else set()
    needs_jump_auth = hop_role == "target" and bool(jump_cfg)
    if needs_jump_auth and jump_key in jump_failed:
        raise HostKeyError("jump host authentication already failed in this run; no further attempt", JUMP_AUTH_ERROR)

    runtime = ssh_isolation.active_runtime()
    tmpdir = tempfile.mkdtemp(prefix="nlm-probe-", dir=runtime.tmp_dir if runtime is not None else tempfile.gettempdir())
    pid = None
    fd = None
    try:
        os.chmod(tmpdir, 0o700)
        tmp_kh = os.path.join(tmpdir, "known_hosts")
        argv = build_probe_command(device_config, hop_role, tmp_kh, files)
        pid, fd = _spawn_pty(argv)
        out = ""
        jump_sent = False
        key: ProbedKey | None = None
        key_seen_at: float | None = None
        deadline = time.monotonic() + timeout
        failure: tuple[str, str] | None = None
        ended = False
        auth_ok = re.compile(r"Authenticated to " + re.escape(hop.host) + r" \(")
        # A refusal of the *jump* hop names the jump user and host; the probed hop's own refusal
        # ("nlm-hostkey-probe@target: Permission denied") is the normal end of a probe.
        jump_denied = re.compile(
            re.escape(str((jump_cfg or {}).get("username") or "")) + "@" + re.escape(str((jump_cfg or {}).get("address")))
            + r": Permission denied")
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    data = b""
                if not data:
                    ended = True
                    break
                out += data.decode("utf-8", "replace")
                if auth_ok.search(out):
                    failure = (TOFU_PROBE_ERROR, "authentication unexpectedly succeeded on the probe connection; "
                               "the connection was closed, nothing was run and nothing was registered")
                    break
                if re.search(r"\(yes/no(/\[fingerprint\])?\)\?\s*$", out):
                    failure = (TOFU_PROBE_ERROR, "an unexpected confirmation prompt appeared; nothing was sent")
                    break
                last = out.rstrip().splitlines()[-1] if out.strip() else ""
                if term.PASSWORD_PROMPT_RE.search(last):
                    kind = term._classify_proxyjump_prompt(device_config, last) if needs_jump_auth else "other"
                    password = (jump_cfg or {}).get("password")
                    if kind == "jump" and jump_sent:
                        jump_failed.add(jump_key)
                        failure = (JUMP_AUTH_ERROR, "jump host authentication failed")
                        break
                    if kind == "jump" and password:
                        if counters is not None:
                            used = counters.get("jump_sends", 0)
                            if used >= min(counters.get("limit", MAX_JUMP_PASSWORD_SENDS), MAX_JUMP_PASSWORD_SENDS):
                                failure = (JUMP_AUTH_ERROR, "jump authentication budget exhausted")
                                break
                            counters["jump_sends"] = used + 1
                        os.write(fd, (password + "\n").encode())
                        jump_sent = True
                        out = ""
                        continue
                    failure = (TOFU_PROBE_ERROR, "unexpected password prompt (nothing was sent)")
                    break
                if jump_sent and jump_denied.search(out):
                    jump_failed.add(jump_key)
                    failure = (JUMP_AUTH_ERROR, "jump host authentication failed")
                    break
            if key is None:
                key = read_probe_file(tmp_kh, ident)
                if key is not None and key_seen_at is None:
                    key_seen_at = time.monotonic()
            elif time.monotonic() - key_seen_at > KEY_GRACE_SECONDS:
                break  # key recorded; do not wait for the authentication outcome
        timed_out = not ended and failure is None and key is None
        # Always end the probe before judging the file: a half-written entry is never accepted.
        _kill(pid)
        pid = None
        if failure is not None:
            raise HostKeyError(failure[1], failure[0])
        key = read_probe_file(tmp_kh, ident)
        if key is None:
            known = classify_ssh_failure(out)
            if known:
                raise HostKeyError(known[1], known[0])
            raise HostKeyError(
                "the connection ended without a host key being recorded" + (" (timeout)" if timed_out else ""),
                TOFU_PROBE_ERROR)
        return key
    finally:
        if pid is not None:
            _kill(pid)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Persistence (product known_hosts only)
# --------------------------------------------------------------------------


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare_product_file(path: str) -> None:
    """Private directory (0700) and a safe regular file (0600, owned by the
    user, not a symlink). Raises TOFU_STORAGE_ERROR otherwise."""
    d = os.path.dirname(path)
    try:
        if not os.path.isdir(d):
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)
        if os.path.lexists(path):
            hs.check_known_hosts_file(path)
        else:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            except FileExistsError:
                pass  # created by another process just now; the checks below decide
            hs.check_known_hosts_file(path)
    except (OSError, hs.HostKeyStoreError) as exc:
        raise HostKeyError(f"the product known_hosts cannot be used safely ({type(exc).__name__}); "
                           "nothing was registered", TOFU_STORAGE_ERROR) from None


def append_entry(path: str, identifier: str, key: ProbedKey) -> None:
    """Append exactly one line without touching existing content (O_APPEND,
    single write, fsync of file and directory)."""
    prepare_product_file(path)
    line = f"{identifier} {key.key_type} {key.blob}\n".encode("ascii")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise HostKeyError("the product known_hosts is not a regular file of this user", TOFU_STORAGE_ERROR)
            if st.st_size:
                with open(path, "rb") as rf:
                    rf.seek(-1, os.SEEK_END)
                    if rf.read(1) != b"\n":
                        line = b"\n" + line
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_dir(os.path.dirname(path))
    except OSError as exc:
        raise HostKeyError(f"the host key could not be written ({type(exc).__name__}); nothing was sent",
                           TOFU_STORAGE_ERROR) from None


def verify_registered(path: str, identifier: str, key: ProbedKey) -> None:
    """Re-read the product file and require the probed key (and only it)."""
    state = hs.lookup(identifier, path)
    ok = (
        state.state == hs.REGISTERED_UNVERIFIED
        and len(state.keys) == 1
        and state.keys[0].fingerprint == key.fingerprint
        and state.keys[0].key_type == key.key_type
    )
    if not ok:
        raise HostKeyError("the registered host key does not match the probed key; nothing was sent", TOFU_STORAGE_ERROR)


# --------------------------------------------------------------------------
# Event log
# --------------------------------------------------------------------------


def log_path() -> str:
    runtime = ssh_isolation.active_runtime()
    if runtime is not None:
        return os.path.join(runtime.sandbox, "tofu.log")
    return os.path.join(str(_terminal().LOGS_ROOT.parent), "hostkey-tofu.log")


def log_event(**fields) -> None:
    """One JSON line (timestamp, device, hop, host, port, key type,
    fingerprint, result). Never a credential, never key material beyond the
    public SHA256 fingerprint. Logging problems never affect trust decisions."""
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}
    try:
        path = log_path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=True) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# TOFU
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TofuEvent:
    device: str
    hop: str  # direct | jump | target
    host: str
    port: int
    key_type: str
    fingerprint: str

    def message(self) -> str:
        return ("SSH host key automatically accepted (TOFU):\n"
                f"{self.device} ({self.hop}) {key_label(self.key_type)} {self.fingerprint}")


def register_unknown(
    device_name: str, device_config: dict, hop: Hop, *, product_kh: str | None = None,
    user_kh: str | None = None, global_kh: str | None = None, counters: dict | None = None,
    lock_dir: str | None = None,
) -> TofuEvent | None:
    """TOFU for one hop. Under the product lock: re-check, probe once, append,
    re-read and compare. Returns None when another process registered the
    host meanwhile. Raises HostKeyError (nothing registered, nothing sent)."""
    product_kh = product_kh or product_known_hosts_path()
    user_kh = user_kh if user_kh is not None else user_known_hosts_path()
    files = [product_kh, user_kh] if user_kh else [product_kh]
    global_kh = global_kh if global_kh is not None else global_known_hosts_path()
    ident = hs.host_identifier(hop.host, hop.port)
    label = hop_label(device_config, hop.role)
    prepare_product_file(product_kh)
    try:
        lock = hs.KnownHostsLock(product_kh, lock_dir or lock_dir_for(product_kh))
        lock.__enter__()
    except hs.HostKeyStoreError as exc:
        raise HostKeyError(str(exc), TOFU_STORAGE_ERROR) from None
    try:
        state = classify(hop.host, hop.port, files, global_kh)
        if state.state == hs.UNDETERMINED:
            raise HostKeyError(f"the known_hosts state of the {hop.role} cannot be determined ({state.reason}); "
                               "nothing was sent", TOFU_IDENTITY_ERROR)
        if state.state == hs.REGISTERED_UNVERIFIED:
            return None
        try:
            key = probe_host_key(device_name, device_config, hop.role, files=files, counters=counters)
        except HostKeyError as exc:
            log_event(device=device_name, hop=label, host=ident, port=hop.port, result="probe_failed",
                      error=exc.code or "ERROR")
            raise
        try:
            append_entry(product_kh, ident, key)
            verify_registered(product_kh, ident, key)
        except HostKeyError as exc:
            log_event(device=device_name, hop=label, host=ident, port=hop.port, key_type=key.key_type,
                      fingerprint=key.fingerprint, result="registration_failed", error=exc.code or "ERROR")
            raise
        log_event(device=device_name, hop=label, host=ident, port=hop.port, key_type=key.key_type,
                  fingerprint=key.fingerprint, result="registered")
        return TofuEvent(device_name, label, hop.host, hop.port, key.key_type, key.fingerprint)
    finally:
        lock.__exit__(None, None, None)


def ensure_trusted(device_name: str, device_config: dict, *, counters: dict | None = None) -> list[TofuEvent]:
    """TOFU every unregistered SSH hop of one access-info-defined device (jump
    first, then target). Registered hops are never probed. Used by managed
    terminals; the Discovery coordinator uses `register_unknown` directly."""
    events: list[TofuEvent] = []
    counters = counters if counters is not None else {"limit": 2}
    for hop in hops_for(device_config):
        ev = register_unknown(device_name, device_config, hop, counters=counters)
        if ev is not None:
            events.append(ev)
    return events
