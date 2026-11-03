# SPDX-License-Identifier: Apache-2.0
"""Human approval boundary for replacing a registered SSH host key (CLI `discover topology`).

Nothing in this module is reachable from MCP or from managed terminals: it
is imported only by the human CLI and by the Discovery host-key coordinator.
Approvals live in memory only, are bound to one (host, port, hop, key type,
fingerprint, operation, discovery run), expire, and are consumed exactly once.
"""

from __future__ import annotations

import base64
import os
import re
import select
import secrets
import stat
import sys
import termios
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

INPUT_TIMEOUT_SECONDS = 180
APPROVAL_TTL_SECONDS = 900

# CLI label -> (known_hosts key types, HostKeyAlgorithms for the confirmation connection)
KEY_TYPES: dict[str, tuple[tuple[str, ...], str]] = {
    "ED25519": (("ssh-ed25519",), "ssh-ed25519"),
    "ECDSA": (
        ("ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"),
        "ecdsa-sha2-nistp256,ecdsa-sha2-nistp384,ecdsa-sha2-nistp521",
    ),
    # RSA: stored as ssh-rsa, signed with SHA-2. SHA-1 `ssh-rsa` signatures are never enabled.
    "RSA": (("ssh-rsa",), "rsa-sha2-512,rsa-sha2-256"),
}
_FP_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class ApprovalError(Exception):
    pass


def sanitize_display(value: object) -> str:
    """Neutralise terminal control characters / escape sequences."""
    return _CONTROL_RE.sub("?", str(value))


def parse_key_type(label: str) -> str:
    key = (label or "").strip().upper()
    if key not in KEY_TYPES:
        raise ApprovalError("unsupported key type (use ED25519, ECDSA or RSA)")
    return key


def parse_fingerprint(text: str) -> str:
    """`SHA256:` + 43 unpadded base64 characters that decode to 32 bytes."""
    value = (text or "").strip()
    if not _FP_RE.match(value):
        raise ApprovalError("fingerprint must be SHA256:<43 base64 characters>")
    try:
        raw = base64.b64decode(value[7:] + "=", validate=True)
    except ValueError:
        raise ApprovalError("fingerprint is not valid base64") from None
    if len(raw) != 32:
        raise ApprovalError("fingerprint does not decode to 32 bytes")
    return value


def key_label_for_known_hosts_type(known_hosts_type: str) -> str | None:
    for label, (types, _algs) in KEY_TYPES.items():
        if known_hosts_type in types:
            return label
    return None


def host_key_algorithms(label: str) -> str:
    return KEY_TYPES[parse_key_type(label)][1]


@dataclass
class Approval:
    id: str
    discovery_id: str
    host: str
    port: int
    hop: str  # "jump" | "target"
    key_label: str
    fingerprint: str
    operation: str  # "register" | "replace"
    expires_at: float
    consumed: bool = False


class ApprovalRegistry:
    """In-memory, one-time, expiring approvals for one Discovery run."""

    def __init__(self, discovery_id: str | None = None, *, clock: Callable[[], float] = time.monotonic):
        self.discovery_id = discovery_id or secrets.token_hex(8)
        self._clock = clock
        self._items: dict[str, Approval] = {}

    def grant(self, *, host: str, port: int, hop: str, key_label: str, fingerprint: str, operation: str) -> Approval:
        if hop not in ("jump", "target") or operation not in ("register", "replace"):
            raise ApprovalError("invalid approval scope")
        a = Approval(
            secrets.token_hex(8), self.discovery_id, host, int(port), hop, parse_key_type(key_label),
            parse_fingerprint(fingerprint), operation, self._clock() + APPROVAL_TTL_SECONDS,
        )
        self._items[a.id] = a
        return a

    def find(self, *, host: str, port: int, hop: str, operation: str) -> Approval | None:
        for a in self._items.values():
            if (a.host, a.port, a.hop, a.operation) == (host, int(port), hop, operation) and not a.consumed:
                return a
        return None

    def consume(
        self, approval: Approval, *, host: str, port: int, hop: str, key_label: str, fingerprint: str,
        operation: str, discovery_id: str,
    ) -> None:
        """Consume exactly once; any mismatch or expiry fails closed. The
        approval is burned even when the match fails (no retry on the same id)."""
        ok = (
            not approval.consumed
            and self._clock() <= approval.expires_at
            and approval.discovery_id == discovery_id == self.discovery_id
            and (approval.host, approval.port, approval.hop, approval.operation)
            == (host, int(port), hop, operation)
            and approval.key_label == key_label
            and approval.fingerprint == fingerprint
        )
        approval.consumed = True
        if not ok:
            raise ApprovalError("approval is expired, already used, or does not match this connection")


# --------------------------------------------------------------------------
# Terminal channel
# --------------------------------------------------------------------------


class TtyChannel(Protocol):
    def flush_input(self) -> None: ...
    def write(self, text: str) -> None: ...
    def readline(self, timeout: float) -> str | None: ...  # None = timeout/EOF


class DevTtyChannel:
    """Reads only from the controlling terminal (/dev/tty), never stdin."""

    def __init__(self):
        for fd in (0, 1):
            if not os.isatty(fd):
                raise ApprovalError("approval needs an interactive terminal (stdin/stdout are not TTYs)")
        if os.fstat(0).st_rdev != os.fstat(1).st_rdev:
            raise ApprovalError("stdin and stdout are different terminals")
        try:
            self._fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        except OSError:
            raise ApprovalError("no controlling terminal is available") from None
        try:
            os.tcgetpgrp(self._fd)
        except OSError:
            os.close(self._fd)
            raise ApprovalError("controlling terminal is not usable") from None

    def flush_input(self) -> None:
        termios.tcflush(self._fd, termios.TCIFLUSH)

    def write(self, text: str) -> None:
        os.write(self._fd, text.encode("utf-8", "replace"))

    def readline(self, timeout: float) -> str | None:
        buf = b""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            r, _, _ = select.select([self._fd], [], [], left)
            if not r:
                return None
            chunk = os.read(self._fd, 1)
            if not chunk:
                return None
            if chunk == b"\n":
                return buf.decode("utf-8", "replace").rstrip("\r")
            buf += chunk
            if len(buf) > 512:
                return None

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass
