# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Host-key handling of the human CLI `discover topology`.

* **First use (TOFU) needs no human.** `Coordinator.prepare` registers every
  unregistered hop of the devices in the access-info through
  `hostkey_tofu.register_unknown` (authentication-free probe, key persisted in
  the product known_hosts *before* any credential is sent). The credential-
  carrying Discovery connections then always run with
  `StrictHostKeyChecking=yes` on **every hop**.
* **Replacing a registered key is human-only.** A changed key (or one of
  another type than the registered one, which OpenSSH reports as changed)
  stops the connection. `Coordinator.recover_changed` probes the new key into
  a temporary file, shows old and new fingerprints, asks on the controlling
  terminal (`/dev/tty`, default No), then backs up, removes exactly that
  host's entries from the product known_hosts and registers the probed key.
  Entries in the user's or the global known_hosts are never edited.

This module is imported by the human CLI and Discovery only. It is never
imported by the MCP server or the terminal layer (a test enforces this).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable

from network_lab_mcp import hostkey_approval as ha
from network_lab_mcp import hostkey_store as hs
from network_lab_mcp import hostkey_tofu as tofu
from network_lab_mcp.hostkey_tofu import (  # noqa: F401  (re-exported for Discovery/CLI/tests)
    MAX_JUMP_PASSWORD_SENDS, PROBE_TIMEOUT_SECONDS, AuthState, Hop, HostKeyChanged, HostKeyError, HostKeyState,
    build_ssh_command, hops_for,
)

# --------------------------------------------------------------------------
# OpenSSH output parsing (strict-connection failures)
# --------------------------------------------------------------------------

_CHANGED_RE = re.compile(r"Host key for (?P<host>\S+) has changed and you have requested strict checking")
_CHANGED_BANNER_RE = re.compile(r"REMOTE HOST IDENTIFICATION HAS CHANGED")
_VERIFY_FAILED_RE = re.compile(r"Host key verification failed")
_NO_KEY_STRICT_RE = re.compile(r"No \S+ host key is known for (?P<host>\S+) and you have requested strict checking")


def split_host_port(token: str) -> tuple[str, int]:
    token = token.strip()
    m = re.match(r"^\[(?P<h>[^\]]+)\]:(?P<p>\d+)$", token)
    if m:
        return m.group("h"), int(m.group("p"))
    return token.strip("[]"), 22


def detect_changed(text: str) -> tuple[str, int] | None:
    m = _CHANGED_RE.search(text)
    if m:
        return split_host_port(m.group("host"))
    if _CHANGED_BANNER_RE.search(text):
        return ("", 0)
    return None


def detect_verification_failure(text: str) -> bool:
    return bool(_VERIFY_FAILED_RE.search(text) or _NO_KEY_STRICT_RE.search(text))


def known_hosts_path() -> str:
    """The product known_hosts (the only file the product writes)."""
    return tofu.product_known_hosts_path()


def default_known_hosts() -> str:
    return tofu.user_known_hosts_path()


def global_known_hosts_path() -> str | None:
    return tofu.global_known_hosts_path()


# --------------------------------------------------------------------------
# Coordinator (human CLI)
# --------------------------------------------------------------------------

_REPLACE_WARNING = (
    "\nWARNING:\nThe SSH host key has changed.\n\n"
    "This may be caused by device reinstallation,\nconfiguration changes, or a man-in-the-middle attack.\n\n"
    "The new key was obtained over the network and\nhas not been independently verified.\n\n"
)


def confirm_replace(channel: ha.TtyChannel, timeout: float = ha.INPUT_TIMEOUT_SECONDS) -> bool:
    """The only place a registered host key can be approved for replacement.
    Input already buffered on the terminal (pasted or piped text, an early
    "yes") is discarded first; only a fresh, exact `yes` approves. Empty or
    any other input, EOF, Ctrl-C-induced EOF and timeout all cancel."""
    channel.flush_input()
    channel.write("Replace the trusted host key? [yes/no] (default: no): ")
    return (channel.readline(timeout) or "").strip() == "yes"


@dataclass
class Coordinator:
    """Created by the human CLI for one `discover topology` run. `channel` is
    the controlling-terminal channel used only to confirm the replacement of a
    registered key; without one (no TTY) first-use TOFU still works and any
    changed key simply stops the run."""

    channel: ha.TtyChannel | None = None
    known_hosts: str = field(default_factory=tofu.product_known_hosts_path)  # product file: the only one written
    global_known_hosts: str | None = field(default_factory=tofu.global_known_hosts_path)
    backup_dir: str = field(default_factory=hs.default_backup_dir)
    lock_dir: str | None = None
    user_known_hosts: str | None = field(default_factory=tofu.user_known_hosts_path)  # read-only for the product
    registry: ha.ApprovalRegistry = field(default_factory=ha.ApprovalRegistry)
    counters: dict = field(default_factory=dict)
    trusted: set = field(default_factory=set)  # (host, port) established/registered in this run
    device_hops: dict = field(default_factory=dict)  # device -> list[Hop] (state tracked per hop)
    messages: list[str] = field(default_factory=list)
    emit: Callable[[str], None] = print

    def __post_init__(self):
        if self.lock_dir is None:
            self.lock_dir = tofu.lock_dir_for(self.known_hosts)

    # -- helpers
    def strict_files(self) -> list[str]:
        return [self.known_hosts, *([self.user_known_hosts] if self.user_known_hosts else [])]

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
        if self.channel is not None:
            self.channel.write(text + "\n")

    def _note(self, text: str) -> None:
        self.messages.append(text)
        self.emit(text)

    def _lock(self):
        return hs.KnownHostsLock(self.known_hosts, self.lock_dir)

    # -- before workers start
    def prepare(self, targets: dict[str, dict]) -> None:
        """Serial, main-thread. TOFU-registers every unknown hop (jump hops
        first; at most one probe per unknown host). Raises HostKeyError on
        anything unresolved (fail closed)."""
        seen: set[tuple[str, int]] = set()
        # Deterministic jump-password budget for probe connections:
        #   limit = min(40, 2 x (devices behind a password-authenticated jump host) + 6)
        # (<= 1 probe per such device -- only for unregistered targets --, plus 1 per CHANGED review,
        # at most 3 reviews, plus slack).
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
                state = tofu.classify(hop.host, hop.port, self.strict_files(), self.global_known_hosts)
                if state.state == hs.UNDETERMINED:
                    raise HostKeyError(
                        f"Device '{device}': the SSH host-key state of the {hop.role} cannot be determined "
                        f"({state.reason}). Resolve it manually; nothing was sent.", tofu.TOFU_IDENTITY_ERROR,
                    )
                if state.state == hs.REGISTERED_UNVERIFIED:
                    hop.host_state = HostKeyState.REGISTERED_UNVERIFIED
                    continue
                self._tofu(device, cfg, hop)

    def _tofu(self, device: str, cfg: dict, hop: Hop) -> None:
        try:
            event = tofu.register_unknown(
                device, cfg, hop, product_kh=self.known_hosts, user_kh=self.user_known_hosts,
                global_kh=self.global_known_hosts, counters=self.counters, lock_dir=self.lock_dir,
            )
        except HostKeyError as exc:
            hop.host_state = HostKeyState.FAILED
            raise HostKeyError(
                f"Device '{device}': the {hop.role} host key could not be registered: {exc} "
                "Nothing was sent. If the device cannot be probed safely, register its key once with a manual "
                "ssh connection."
            ) from None
        hop.host_state = HostKeyState.TRUSTED
        self.trusted.add((hop.host, hop.port))
        self._set_state(hop.host, hop.port, HostKeyState.TRUSTED)
        if event is not None:
            self._note(event.message())

    # -- CHANGED / KEY_TYPE_MISMATCH recovery (human only)
    def recover_changed(self, err: HostKeyChanged, device_config: dict) -> bool:
        """Interactive replacement review. Returns True when the probed key
        replaced the registered one (the caller re-runs Discovery)."""
        if self.channel is None:
            return False
        hops = {h.role: h for h in hops_for(device_config)}
        hop = hops.get(err.hop)
        if hop is None:
            return False
        ident = hs.host_identifier(hop.host, hop.port)
        host_disp = f"{ha.sanitize_display(hop.host)}:{hop.port}"
        old = hs.lookup(ident, self.known_hosts)
        if old.state != hs.REGISTERED_UNVERIFIED:
            self._say(
                f"\nDevice '{ha.sanitize_display(err.device)}': the SSH host key of {host_disp} has changed, but the "
                "trusted key is not in the product known_hosts, which is the only file this tool edits.\n"
                f"If it is in your own known_hosts, remove the old entry yourself (for a non-default port use the "
                f"[host]:port form), e.g.:  ssh-keygen -R '{ident}' -f ~/.ssh/known_hosts\n"
                "A system-wide known_hosts must be changed by its administrator."
            )
            return False
        sig = hs.file_signature(self.known_hosts)
        try:
            new = tofu.probe_host_key(err.device, device_config, hop.role, files=self.strict_files(), counters=self.counters)
        except HostKeyError as exc:
            self._say(f"% The presented host key could not be obtained ({exc}). Known hosts unchanged.")
            return False
        label = ha.key_label_for_known_hosts_type(new.key_type)
        if label is None:
            return False
        kind = tofu.KEY_TYPE_MISMATCH if new.key_type not in {k.key_type for k in old.keys} else tofu.HOST_KEY_CHANGED
        self._say(_REPLACE_WARNING + f"Device: {ha.sanitize_display(err.device)} ({tofu.hop_label(device_config, hop.role)})\n"
                  f"Host: {host_disp}\nReason: {kind}\n")
        for k in old.keys:
            self._say(f"Trusted key: {k.key_type} {k.fingerprint}")
        self._say(f"New key:     {new.key_type} {new.fingerprint}\n")
        if not confirm_replace(self.channel):
            self._say("% Cancelled. Known hosts unchanged.")
            return False
        approval = self.registry.grant(host=hop.host, port=hop.port, hop=hop.role, key_label=label,
                                       fingerprint=new.fingerprint, operation="replace")
        with self._lock():
            if hs.file_signature(self.known_hosts) != sig:
                raise HostKeyError("known_hosts changed during review; nothing was modified. Re-run the command.")
            backup = hs.remove_host(ident, self.known_hosts, self.backup_dir, expect_signature=sig)
            self._say(f"Backup saved: {backup}")
            self._tidy_after_removal()
            try:
                tofu.append_entry(self.known_hosts, ident, new)
                tofu.verify_registered(self.known_hosts, ident, new)
            except HostKeyError as exc:
                restored = self._restore_old_entries(ident, backup)
                raise HostKeyError(
                    f"Device '{err.device}': the replacement key could not be registered ({exc}). "
                    + ("The previous entries were restored." if restored
                       else f"The previous entries could not be restored automatically (backup: {backup}).")
                    + " Nothing was sent."
                ) from None
            self.registry.consume(approval, host=hop.host, port=hop.port, hop=hop.role, key_label=label,
                                  fingerprint=new.fingerprint, operation="replace",
                                  discovery_id=self.registry.discovery_id)
        tofu.log_event(device=err.device, hop=tofu.hop_label(device_config, hop.role), host=ident, port=hop.port,
                       key_type=new.key_type, fingerprint=new.fingerprint, result="replaced", reason=kind,
                       previous=[k.fingerprint for k in old.keys])
        self.trusted.add((hop.host, hop.port))
        self._set_state(hop.host, hop.port, HostKeyState.TRUSTED)
        self._say("Replacement host key registered.")
        return True

    def _tidy_after_removal(self) -> None:
        """`ssh-keygen -R` leaves `<file>.old` (the backup in the private backup
        directory is the record). Make sure the file keeps its owner/mode."""
        old = self.known_hosts + ".old"
        try:
            if os.path.lexists(old) and not os.path.islink(old):
                os.unlink(old)
            os.chmod(self.known_hosts, 0o600)
            hs.check_known_hosts_file(self.known_hosts)
        except (OSError, hs.HostKeyStoreError) as exc:
            raise HostKeyError(f"known_hosts permissions could not be confirmed after removal ({type(exc).__name__})",
                               tofu.TOFU_STORAGE_ERROR) from None

    def _restore_old_entries(self, ident: str, backup: str) -> bool:
        """Put the removed host's lines back (append only: other processes'
        additions are never overwritten)."""
        try:
            found = hs.run_keygen(["-F", ident, "-f", backup])
            lines = [ln for ln in found.stdout.splitlines() if ln.strip() and not ln.startswith("#")]
            if found.returncode != 0 or not lines:
                return False
            with open(self.known_hosts, "ab") as fh:
                fh.write(("\n".join(lines) + "\n").encode("utf-8", "surrogateescape"))
                fh.flush()
                os.fsync(fh.fileno())
            return hs.lookup(ident, self.known_hosts).state == hs.REGISTERED_UNVERIFIED
        except (OSError, hs.HostKeyStoreError):
            return False
