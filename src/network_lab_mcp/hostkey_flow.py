# SPDX-License-Identifier: Apache-2.0
"""Host-key establishment and recovery for the human CLI `discover topology`.

Design (see docs/architecture.md, "SSH host-key management"):

* A key is **established before any credential-carrying connection**. A
  short *registration connection* (pty, `StrictHostKeyChecking=ask`, the
  approved fingerprint answered at most once per hop, target authentication
  disabled with `PreferredAuthentications=none`) lets OpenSSH itself write the
  approved key into known_hosts. No target credential is ever sent on it.
* The Discovery connections that carry credentials then always run with
  `StrictHostKeyChecking=yes` on **every hop**. ProxyJump hops are given
  explicit per-hop options through a structured `-o ProxyCommand=` (OpenSSH's
  `-J` child does not inherit command-line `-o` options; verified with
  OpenSSH 10.3).
* CHANGED keys stop the connection; recovery happens in the CLI: new key
  verified on a throw-away copy of known_hosts, backup, removal of exactly one
  host's entries, then a registration connection against the real file.

This module is imported by the human CLI and Discovery only. It is never
imported by the MCP server (a test enforces this).
"""

from __future__ import annotations

import os
import pty
import re
import select
import shlex
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum

from network_lab_mcp import hostkey_approval as ha
from network_lab_mcp import hostkey_store as hs
from network_lab_mcp import ssh_isolation, terminal

PROBE_TIMEOUT_SECONDS = 30
MAX_JUMP_PASSWORD_SENDS = 40  # absolute cap for registration/probe connections per Discovery run


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


class HostKeyChanged(HostKeyError):
    def __init__(self, device: str, hop: str, host: str, port: int):
        super().__init__(f"Device '{device}': the SSH host key of the {hop} ({host}:{port}) has changed.")
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


def hops_for(device_config: dict) -> list[Hop]:
    transport = device_config.get("transport")
    jump = device_config.get("jump_host_config")
    hops: list[Hop] = []
    if transport == "ssh":
        if jump:
            target, port = terminal._ssh_target(jump, 22)
            user, _, host = target.rpartition("@")
            hops.append(Hop("jump", host, port, user or None))
        target, port = terminal._ssh_target(device_config, 22)
        user, _, host = target.rpartition("@")
        hops.append(Hop("target", host, port, user or None))
    elif transport == "telnet" and jump:
        target, port = terminal._ssh_target(jump, 22)
        user, _, host = target.rpartition("@")
        hops.append(Hop("jump", host, port, user or None))
    return hops


# --------------------------------------------------------------------------
# OpenSSH output parsing (real OpenSSH 10.x wording; older wording tolerated)
# --------------------------------------------------------------------------

_PROMPT_RE = re.compile(
    r"The authenticity of host '(?P<host>[^']+)' can't be established\.\s*\n"
    r"(?P<alg>[A-Za-z0-9-]+) key fingerprint is:? (?P<fp>SHA256:[A-Za-z0-9+/]{43})\.?\s*\n"
    r"(?:.*\n)*?"
    r"Are you sure you want to continue connecting \(yes/no(?P<fpok>/\[fingerprint\])?\)\?\s*$"
)
_CHANGED_RE = re.compile(r"Host key for (?P<host>\S+) has changed and you have requested strict checking")
_CHANGED_BANNER_RE = re.compile(r"REMOTE HOST IDENTIFICATION HAS CHANGED")
_VERIFY_FAILED_RE = re.compile(r"Host key verification failed")
_NO_KEY_STRICT_RE = re.compile(r"No \S+ host key is known for (?P<host>\S+) and you have requested strict checking")


@dataclass(frozen=True)
class HostKeyPrompt:
    host: str
    port: int
    key_label: str
    fingerprint: str
    supports_fingerprint_answer: bool


def split_host_port(token: str) -> tuple[str, int]:
    token = token.strip()
    m = re.match(r"^\[(?P<h>[^\]]+)\]:(?P<p>\d+)$", token)
    if m:
        return m.group("h"), int(m.group("p"))
    return token.strip("[]"), 22


def parse_host_key_prompt(text: str) -> HostKeyPrompt | None:
    tail = "\n".join(text.rstrip().splitlines()[-14:])
    m = _PROMPT_RE.search(tail)
    if not m:
        return None
    name = m.group("host").split(" (")[0]
    host, port = split_host_port(name)
    alg = m.group("alg").upper()
    label = "ECDSA" if alg.startswith("ECDSA") else alg
    return HostKeyPrompt(host, port, label, m.group("fp"), bool(m.group("fpok")))


def detect_changed(text: str) -> tuple[str, int] | None:
    m = _CHANGED_RE.search(text)
    if m:
        return split_host_port(m.group("host"))
    if _CHANGED_BANNER_RE.search(text):
        return ("", 0)
    return None


def detect_verification_failure(text: str) -> bool:
    return bool(_VERIFY_FAILED_RE.search(text) or _NO_KEY_STRICT_RE.search(text))


# --------------------------------------------------------------------------
# ssh argv construction
# --------------------------------------------------------------------------


def ssh_prefix(kh: str | None = None) -> list[str]:
    runtime = ssh_isolation.active_runtime()
    if runtime is not None:
        return [runtime.wrapper("ssh"), *ssh_isolation.required_ssh_args(runtime, kh)]
    found = shutil.which("ssh")
    if not found:
        raise HostKeyError("ssh is not available")
    return [os.path.realpath(found)]


def default_known_hosts() -> str:
    import pwd

    return os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".ssh", "known_hosts")


def known_hosts_path() -> str:
    runtime = ssh_isolation.active_runtime()
    return runtime.known_hosts if runtime is not None else default_known_hosts()


def global_known_hosts_path() -> str | None:
    runtime = ssh_isolation.active_runtime()
    return runtime.global_known_hosts if runtime is not None else "/etc/ssh/ssh_known_hosts"


def _hop_opts(kh: str, policy: str, key_label: str | None = None) -> list[str]:
    opts = [
        "-o", f"StrictHostKeyChecking={policy}", "-o", f"UserKnownHostsFile={kh}", "-o", "UpdateHostKeys=no",
        "-o", "CheckHostIP=no", "-o", "VerifyHostKeyDNS=no", "-o", "BatchMode=no",
    ]
    if key_label:
        opts += ["-o", f"HostKeyAlgorithms={ha.host_key_algorithms(key_label)}"]
    return opts


_NO_AUTH_OPTS = [
    "-o", "PreferredAuthentications=none", "-o", "PubkeyAuthentication=no", "-o", "GSSAPIAuthentication=no",
    "-o", "KbdInteractiveAuthentication=no", "-o", "PasswordAuthentication=no", "-o", "NumberOfPasswordPrompts=0",
]


def build_ssh_command(
    device_config: dict, *, kh: str, target_policy: str = "yes", jump_policy: str = "yes",
    target_label: str | None = None, jump_label: str | None = None, no_auth_hop: str | None = None,
) -> list[str]:
    """argv for an ssh connection with explicit per-hop host-key policy.
    `no_auth_hop` ('target'|'jump') disables every authentication method on
    that hop (registration connections)."""
    target, port = terminal._ssh_target(device_config, 22)
    base = ssh_prefix(kh)
    jump = device_config.get("jump_host_config")
    cmd = [*base]
    if jump:
        jtarget, jport = terminal._ssh_target(jump, 22)
        child = [*base, *_hop_opts(kh, jump_policy, jump_label)]
        if no_auth_hop == "jump":
            child += _NO_AUTH_OPTS
        child += ["-W", "%h:%p", "-p", str(jport), jtarget]
        cmd += ["-o", "ProxyCommand=" + " ".join(shlex.quote(x) for x in child)]
    cmd += _hop_opts(kh, target_policy, target_label)
    if no_auth_hop == "target":
        cmd += _NO_AUTH_OPTS
    cmd += ["-p", str(port), target]
    return cmd


# --------------------------------------------------------------------------
# Registration / verification connection (pty, no credential to the hop)
# --------------------------------------------------------------------------


@dataclass
class ProbeResult:
    ok: bool
    reason: str = ""


def _spawn_pty(argv: list[str]):
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


def run_registration_connection(
    device_name: str, device_config: dict, hop_role: str, kh: str, expected: tuple[str, str],
    *, counters: dict | None = None, timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Connect to `hop_role` and let OpenSSH register `expected` (key label,
    fingerprint) into `kh` -- only if the presented key is exactly that.

    * The fingerprint is answered at most once per hop, and only when the
      prompt's host/port/algorithm/fingerprint all match.
    * The hop under test never gets a credential (all auth methods off).
    * For a target behind a jump host the jump hop must already be trusted
      (strict); its password is sent only through the jump-prompt attribution
      (`terminal._classify_proxyjump_prompt`), at most once.
    """
    label, fp = expected
    hops = {h.role: h for h in hops_for(device_config)}
    if hop_role not in hops:
        return ProbeResult(False, "no such hop")
    hop = hops[hop_role]
    jump_cfg = device_config.get("jump_host_config")
    if hop_role == "target":
        argv = build_ssh_command(device_config, kh=kh, target_policy="ask", jump_policy="yes",
                                 target_label=label, no_auth_hop="target")
    else:
        sub = {"transport": "ssh", "address": hop.host, "port": hop.port, "username": hop.user}
        argv = build_ssh_command(sub, kh=kh, target_policy="ask", target_label=label, no_auth_hop="target")
    argv += ["true"]
    if hop_role == "target" and jump_cfg and counters is not None:
        if (jump_cfg.get("address"), jump_cfg.get("port", 22)) in counters.get("jump_failed", set()):
            return ProbeResult(False, "jump host authentication already failed in this run")
    pid, fd = _spawn_pty(argv)
    out = ""
    answered = False
    jump_sent = False
    jump_failed: set = counters.setdefault("jump_failed", set()) if counters is not None else set()
    jump_key = (jump_cfg or {}).get("address"), (jump_cfg or {}).get("port", 22)
    deadline = time.monotonic() + timeout
    reason = "timeout"
    try:
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r:
                continue
            try:
                data = os.read(fd, 4096)
            except OSError:
                data = b""
            if not data:
                reason = "connection ended"
                break
            out += data.decode("utf-8", "replace")
            prompt = parse_host_key_prompt(out)
            if prompt is not None:
                if answered:
                    reason = "host key prompt repeated"
                    break
                if (prompt.host, prompt.port) != (hop.host, hop.port):
                    reason = "host key prompt for an unexpected host"
                    break
                if not prompt.supports_fingerprint_answer:
                    reason = "this OpenSSH cannot answer with a fingerprint"
                    break
                if (prompt.key_label, prompt.fingerprint) != (label, fp):
                    reason = "presented host key does not match the approved key"
                    break
                os.write(fd, (fp + "\n").encode())
                answered = True
                out = ""  # prompt consumed; only look at what follows
                continue
            last = out.rstrip().splitlines()[-1] if out.strip() else ""
            if terminal.PASSWORD_PROMPT_RE.search(last):
                kind = terminal._classify_proxyjump_prompt(device_config, last) if jump_cfg else "other"
                password = (jump_cfg or {}).get("password")
                if kind == "jump" and jump_sent:
                    jump_failed.add(jump_key)  # re-prompt after our single send: authentication failed
                    reason = "jump host authentication failed"
                    break
                if kind == "jump" and password and hop_role == "target":
                    if jump_key in jump_failed:
                        reason = "jump host authentication already failed in this run"
                        break
                    if counters is not None:
                        used = counters.get("jump_sends", 0)
                        if used >= min(counters.get("limit", MAX_JUMP_PASSWORD_SENDS), MAX_JUMP_PASSWORD_SENDS):
                            reason = "jump authentication budget exhausted"
                            break
                        counters["jump_sends"] = used + 1
                    os.write(fd, (password + "\n").encode())
                    jump_sent = True
                    out = ""
                    continue
                reason = "unexpected password prompt (nothing was sent)"
                break
            if jump_sent and not answered and re.search(r"Permission denied", out):
                jump_failed.add(jump_key)
                reason = "jump host authentication failed"
                break
        else:
            reason = "timeout"
    finally:
        _kill(pid)
        try:
            os.close(fd)
        except OSError:
            pass
    state = hs.lookup(hs.host_identifier(hop.host, hop.port), kh)
    match = state.state == hs.REGISTERED_UNVERIFIED and any(
        k.fingerprint == fp and ha.key_label_for_known_hosts_type(k.key_type) == label for k in state.keys
    )
    if answered and match:
        return ProbeResult(True)
    return ProbeResult(False, reason if not answered else "registered key does not match the approved key")


# --------------------------------------------------------------------------
# Coordinator (human CLI)
# --------------------------------------------------------------------------


@dataclass
class Coordinator:
    """Created by the interactive CLI for one `discover topology` run."""

    channel: ha.TtyChannel
    known_hosts: str = field(default_factory=known_hosts_path)
    global_known_hosts: str | None = field(default_factory=global_known_hosts_path)
    backup_dir: str = field(default_factory=hs.default_backup_dir)
    lock_dir: str | None = None
    registry: ha.ApprovalRegistry = field(default_factory=ha.ApprovalRegistry)
    counters: dict = field(default_factory=dict)
    trusted: set = field(default_factory=set)  # (host, port) established/registered in this run
    device_hops: dict = field(default_factory=dict)  # device -> list[Hop] (state tracked per hop)
    messages: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.lock_dir is None:
            self.lock_dir = os.path.join(os.path.dirname(self.backup_dir), "locks")

    # -- helpers
    def _set_state(self, host: str, port: int, state: HostKeyState) -> None:
        for hops in self.device_hops.values():
            for h in hops:
                if (h.host, h.port) == (host, port):
                    h.host_state = state

    def require_trusted(self, device: str) -> None:
        """Called by Discovery before any credential is sent for `device`.
        Every hop must be REGISTERED_UNVERIFIED (verified by the strict
        connection itself) or TRUSTED; anything else stops, nothing is sent."""
        hops = self.device_hops.get(device)
        if hops is None or not all(h.may_authenticate() for h in hops):
            raise HostKeyError(f"Device '{device}': a host key is not trusted; no credential was sent.")

    def _say(self, text: str) -> None:
        self.channel.write(text + "\n")

    def _work_dir(self) -> str:
        runtime = ssh_isolation.active_runtime()
        return runtime.tmp_dir if runtime is not None else tempfile.gettempdir()

    def _lock(self):
        return hs.KnownHostsLock(self.known_hosts, self.lock_dir)

    # -- before workers start
    def prepare(self, targets: dict[str, dict]) -> None:
        """Serial, main-thread. Registers approved UNKNOWN keys (jump hops
        first). Raises HostKeyError on anything unresolved (fail closed)."""
        seen: set[tuple[str, int]] = set()
        # Deterministic jump-password budget for registration/probe connections:
        #   limit = min(40, 2 x (devices behind a password-authenticated jump host) + 6)
        # (<= 1 registration per such device, plus 2 per CHANGED recovery, at most 3 recoveries).
        pw_jump_devices = sum(1 for c in targets.values() if (c.get("jump_host_config") or {}).get("password"))
        self.counters["limit"] = min(MAX_JUMP_PASSWORD_SENDS, 2 * pw_jump_devices + 6)
        for device, cfg in targets.items():
            hops = hops_for(cfg)
            self.device_hops[device] = hops
            for hop in hops:
                key = (hop.host, hop.port)
                if key in seen:
                    hop.host_state = HostKeyState.TRUSTED if key in self.trusted else HostKeyState.REGISTERED_UNVERIFIED
                    continue
                seen.add(key)
                state = hs.classify(hop.host, hop.port, self.known_hosts, self.global_known_hosts)
                if state.state == hs.UNDETERMINED:
                    raise HostKeyError(
                        f"Device '{device}': the SSH host-key state of the {hop.role} cannot be determined "
                        f"({state.reason}). Resolve it manually; nothing was sent."
                    )
                if state.state == hs.REGISTERED_UNVERIFIED:
                    hop.host_state = HostKeyState.REGISTERED_UNVERIFIED
                    continue
                if cfg.get("transport") == "telnet":
                    raise HostKeyError(
                        f"Device '{device}': the jump host key is not registered and host-key registration is not "
                        "supported for Telnet-over-SSH tunnels. Register it once with a manual ssh connection."
                    )
                self._register_unknown(device, cfg, hop)

    def _ask(self, device: str, hop: Hop, operation: str) -> tuple[str, str] | None:
        ctx = ha.PromptContext(device, hop.role, hop.host, hop.port, operation)
        return ha.collect_expected_key(self.channel, ctx)

    def _register_unknown(self, device: str, cfg: dict, hop: Hop) -> None:
        expected = self._ask(device, hop, "register")
        if expected is None:
            raise HostKeyError(f"Device '{device}': host-key approval cancelled for the {hop.role}; nothing was sent.")
        approval = self.registry.grant(host=hop.host, port=hop.port, hop=hop.role, key_label=expected[0],
                                       fingerprint=expected[1], operation="register")
        with self._lock():
            result = run_registration_connection(device, cfg, hop.role, self.known_hosts, expected, counters=self.counters)
            if not result.ok:
                hop.host_state = HostKeyState.FAILED
                raise HostKeyError(
                    f"Device '{device}': the {hop.role} host key could not be verified ({result.reason}); "
                    "nothing was registered or sent. Re-run `discover topology` to retry."
                )
            self.registry.consume(approval, host=hop.host, port=hop.port, hop=hop.role, key_label=expected[0],
                                  fingerprint=expected[1], operation="register", discovery_id=self.registry.discovery_id)
        hop.host_state = HostKeyState.TRUSTED
        self.trusted.add((hop.host, hop.port))
        self._set_state(hop.host, hop.port, HostKeyState.TRUSTED)
        self._say(f"Host key registered for {ha.sanitize_display(hop.host)}:{hop.port}.")

    # -- CHANGED recovery
    def recover_changed(self, err: HostKeyChanged, device_config: dict) -> bool:
        """Interactive recovery. Returns True when a verified replacement
        key is registered (the caller re-runs Discovery)."""
        hops = {h.role: h for h in hops_for(device_config)}
        hop = hops.get(err.hop)
        if hop is None:
            return False
        ident = hs.host_identifier(hop.host, hop.port)
        old = hs.lookup(ident, self.known_hosts)
        self._say(
            f"\nDevice '{ha.sanitize_display(err.device)}':\nSSH host key has changed.\n\n"
            "Possible causes:\n- Device reinstallation\n- SSH key regeneration\n- Device replacement\n"
            "- Unexpected device or security issue\n"
        )
        self.channel.flush_input()
        self.channel.write("Select an action:\n  1. Cancel (default)\n  2. Show existing host key information\n"
                           "  3. Review replacement of obsolete host key\n\nChoice [1]: ")
        while True:
            choice = self.channel.readline(ha.INPUT_TIMEOUT_SECONDS)
            choice = (choice or "").strip()
            if choice == "2":
                for k in old.keys:
                    self._say(f"Existing: {k.key_type} {k.fingerprint}")
                self.channel.write("Choice [1]: ")
                continue
            if choice == "3":
                break
            return False
        expected = self._ask(err.device, hop, "replace")
        if expected is None:
            return False
        approval = self.registry.grant(host=hop.host, port=hop.port, hop=hop.role, key_label=expected[0],
                                       fingerprint=expected[1], operation="replace")
        with self._lock():
            sig = hs.file_signature(self.known_hosts)
            tmpdir = tempfile.mkdtemp(prefix="nlm-probe-", dir=self._work_dir())
            os.chmod(tmpdir, 0o700)
            try:
                tmp_kh = os.path.join(tmpdir, "known_hosts")
                with open(tmp_kh, "wb") as out:
                    out.write(open(self.known_hosts, "rb").read())
                os.chmod(tmp_kh, 0o600)
                hs.remove_host(ident, tmp_kh, os.path.join(tmpdir, "bk"))
                probe = run_registration_connection(err.device, device_config, hop.role, tmp_kh, expected,
                                                    counters=self.counters)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
            if not probe.ok:
                self._say(f"% The presented host key could not be verified ({probe.reason}). Known hosts unchanged.")
                return False
            self.channel.flush_input()
            self.channel.write(
                f"Verified: the device presented the expected {expected[0]} key.\n"
                f"Remove the old entries for {ha.sanitize_display(hop.host)}:{hop.port} and register the new key?\n"
                "Final confirmation [yes/no]: "
            )
            if (self.channel.readline(ha.INPUT_TIMEOUT_SECONDS) or "").strip() != "yes":
                return False
            if hs.file_signature(self.known_hosts) != sig:
                raise HostKeyError("known_hosts changed during recovery; nothing was modified. Re-run the command.")
            backup = hs.remove_host(ident, self.known_hosts, self.backup_dir, expect_signature=sig)
            self._say(f"Backup saved: {backup}")
            result = run_registration_connection(err.device, device_config, hop.role, self.known_hosts, expected,
                                                 counters=self.counters)
            if not result.ok:
                raise HostKeyError(
                    f"Device '{err.device}': the replacement key could not be re-verified on the real connection "
                    f"({result.reason}). The old entry was removed (backup: {backup}); nothing was sent."
                )
            self.registry.consume(approval, host=hop.host, port=hop.port, hop=hop.role, key_label=expected[0],
                                  fingerprint=expected[1], operation="replace", discovery_id=self.registry.discovery_id)
        self.trusted.add((hop.host, hop.port))
        self._set_state(hop.host, hop.port, HostKeyState.TRUSTED)
        self._say("Replacement host key registered.")
        return True
