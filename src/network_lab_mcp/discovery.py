# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""IOS XR + IOS XE + classic IOS topology discovery over LLDP and CDP,
enriched with stable Layer-3 interface context (IPv4 address + VRF).

    committed active_access_info
        -> private bootstrap connection (terminal.open_bootstrap_terminal)
        -> per-type login + read-only collection (this module's minimal
           command runner): IOS XR gets LLDP + CDP + `show ipv4 interface
           brief`; IOS XE gets LLDP + CDP + `show vrf`/`show ip interface
           brief`; classic IOS gets CDP + `show vrf`/`show ip interface
           brief` (classic IOS has no LLDP support)
        -> normalized neighbor observations (parse_lldp_neighbors /
           parse_cdp_neighbors), each tagged with its own `source`
        -> identity resolution (resolve_remote_identity, protocol-agnostic)
        -> multi-protocol link reconciliation (reconcile_links): the same
           physical link seen via both protocols (or reciprocally from both
           ends) becomes exactly one link; a local interface where LLDP and
           CDP disagree about the neighbor is a conflict, which (like any
           managed physical interface in more than one link) fails
           Discovery instead of silently picking one
        -> per-device Layer-3 interface enrichment (only for `discover
           topology l3`; see "L3 topology enrichment" below): ipv4_address + vrf only,
           never a prefix length, never operational (up/down) state
        -> DiscoveryResult (in-memory only)

`discover_topology()` is the only entry point cli/main.py's `discover
topology` handler calls; everything else here is an implementation detail.
It never writes to disk and never mutates running-config or any committed
file -- the caller (cli/config.py) is responsible for turning a
DiscoveryResult into a topology *candidate*, exactly like any other
topology edit, and nothing here special-cases commit/clear/root/exit/end.

IOS XE / classic IOS login uses whatever transport the device's
access-info specifies (ssh or telnet). Telnet is unauthenticated-in-transit
and unencrypted -- suitable only for isolated lab environments, never
presented as a secure transport (see README.md/docs/architecture.md).

L3 topology enrichment is opt-in (`discover topology l3`): the default
L2 run sends no L3-only command and never carries L3 data into the candidate.
When requested it fails closed -- any device's L3 command, transport or
parse failure fails the whole Discovery (no partial candidate). Neighbor
discovery (LLDP/CDP) remains the only source of links.
Only a stable, directly observed IPv4 address + VRF are stored -- never a
prefix length, never inferred subnets, and never a link inferred from
address similarity (links come from LLDP/CDP or an explicit topology edit
only). A management-network address (the same address the selected
access-info uses to *reach* that device) is deliberately excluded from
topology L3 data -- see _build_device_interface_fields().

Explicitly out of scope: NX-OS discovery, multi-hop jump hosts (telnet
devices cannot have a jump_host at all -- see lab.validate_device_jump_
host_references()), SNMP/NETCONF/RESTCONF, automatic topology activation/
commit, IPv4 prefix-length/subnet inference, operational-state persistence,
and a generic discovery/plugin framework."""

from __future__ import annotations

import concurrent.futures
import copy
import re
import threading
from dataclasses import dataclass, field

from network_lab_mcp import hostkey_flow, lab, terminal

LOGIN_TIMEOUT_SECONDS = 30
COMMAND_TIMEOUT_SECONDS = 25

# Bounds how many devices' bootstrap collection runs concurrently.
# Small and internal, not a public CLI/config knob --
# 8 comfortably covers real lab scale (a handful to a dozen managed
# devices) while keeping concurrent SSH/tmux session creation bounded
# rather than launching one thread per arbitrary target count.
DISCOVERY_MAX_WORKERS = 8

# Matches an IOS XR exec prompt, e.g. "RP/0/RP0/CPU0:LAB_R1#", and
# captures the hostname -- this doubles as both "the prompt has returned"
# detection and the smallest reliable IOS XR hostname source (see
# "Local hostname collection" in docs/architecture.md): no separate
# `show running-config | include hostname` query is needed.
_IOSXR_PROMPT_RE = re.compile(r"RP/\S+/CPU\d+:(?P<hostname>[^#\s]+)#\s*$", re.MULTILINE)
# The password-prompt regex itself is shared SSOT: see
# terminal.PASSWORD_PROMPT_RE's own docstring -- OpenSSH's client-side
# prompt text is identical regardless of caller, so there is exactly one
# place that recognizes it.
# re.MULTILINE is required here: without it, the `$` in
# each combined sub-pattern only anchors to the true end of the whole
# captured string, not the end of each line -- meaning a match on a line
# that is *not* the very last line of the captured pane text (e.g. once
# something else has already been appended after it) silently fails. Both
# constituent patterns already carry their own re.MULTILINE individually,
# but that flag is lost when their `.pattern` text is combined into a new
# re.compile() call -- it must be re-applied on the combined pattern too.
_LOGIN_WAIT_RE = re.compile(
    f"(?:{terminal.PASSWORD_PROMPT_RE.pattern})|(?:{_IOSXR_PROMPT_RE.pattern})", re.MULTILINE
)

# Classic-IOS-style exec prompt, e.g. "LegacyRouter#" or "LegacyRouter>" -- shared by
# IOS XE *and* classic IOS (the same login/prompt shape applies
# to both, so this is deliberately not named "_IOSXE_..."). Unlike IOS XR's
# "RP/.../CPU0:hostname#" shape, this prompt *is* just the hostname, so the
# whole line must be exactly that (never matched against a mid-table CDP
# row, which always has other fields after the device ID on the same line
# -- see parse_cdp_neighbors()). A telnet/console login may also show a
# "Username:" prompt before "Password:"; OpenSSH's
# own password prompt (terminal.PASSWORD_PROMPT_RE) is reused unchanged
# since it is transport-agnostic text matching, not an SSH-specific
# mechanism.
#
# Both now live in terminal.py: managed terminal_open()'s own
# Telnet authentication needed the exact same two patterns, so they moved
# to the one shared lower-level module rather than being duplicated --
# these names are kept as aliases so nothing else in this file (or its
# tests) needs to change.
_IOS_STYLE_PROMPT_RE = terminal.IOS_STYLE_PROMPT_RE
_USERNAME_PROMPT_RE = terminal.USERNAME_PROMPT_RE
# re.MULTILINE re-applied on the combined pattern -- see _LOGIN_WAIT_RE's
# own comment above for why.
_IOS_STYLE_LOGIN_WAIT_RE = re.compile(
    f"(?:{_USERNAME_PROMPT_RE.pattern})|(?:{terminal.PASSWORD_PROMPT_RE.pattern})|(?:{_IOS_STYLE_PROMPT_RE.pattern})",
    re.MULTILINE,
)

# Real, documented Cisco text for "the command ran, but LLDP is
# administratively disabled" -- this is NOT a
# transport/command failure (the prompt returns normally), and it is NOT a
# parser failure either; it must mean "zero LLDP observations", the same
# way parse_cdp_neighbors() already treats CDP-unavailable leniently.
_LLDP_UNAVAILABLE_RE = re.compile(r"LLDP is not enabled", re.IGNORECASE)


class DiscoveryError(Exception):
    """Fail-closed error for the whole `discover topology` operation."""


class ProtocolOutputError(DiscoveryError):
    """A neighbor-protocol output carrying a "not enabled" message that is not
    exactly the expected one -- an unverifiable state. Raised by the pure
    classifier; Discovery turns it into an unusable protocol (state
    "unknown", a warning, no observations), never into a guess."""


_DEVICE_PREFIX_RE = re.compile(r"^Device '[^']*': ")
_SESSION_EXITED_MARKER = "exited while waiting for expected output"


class DeviceCollectionError(DiscoveryError):
    """A *continuable*, single-device collection failure: connection, login,
    authentication, timeout, or the session ending mid-collection. Only an
    explicitly classified failure of this kind becomes a per-device Warning
    (the device is still registered); every other DiscoveryError --
    host-key problems, an ambiguous credential target, unexpected worker
    crashes -- stays fatal for the whole run. `collected` holds the output
    of every command that *completed* before the failure (never the
    interrupted command's partial output) and `hostname` its login prompt
    identity when login succeeded."""

    def __init__(self, message: str, collected: dict | None = None, session_ended: bool = False):
        super().__init__(message)
        self.collected: dict = collected if collected is not None else {}
        self.session_ended = session_ended

    @property
    def reason(self) -> str:
        return _DEVICE_PREFIX_RE.sub("", str(self), count=1)


# --------------------------------------------------------------------------
# Private bootstrap connectivity + minimal command runner
# --------------------------------------------------------------------------


def _last_nonblank_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line
    return ""


def _resolve_login_password(device_id: str, device_config: dict, prompt_line: str) -> str:
    """Which password answers the current prompt.

    A thin, Discovery-specific wrapper around the shared
    terminal.resolve_target_password_prompt(): the actual
    target-vs-jump-host attribution logic lives there once, reused
    identically by managed terminal_open()'s own private authentication,
    so a prompt that cannot be confidently attributed to either hop fails
    closed (DiscoveryError) the exact same way for both callers -- this
    function only supplies Discovery's own wording for that failure (see
    "Bounded ProxyJump limitation" in docs/architecture.md)."""
    outcome = terminal.resolve_target_password_prompt(device_config, prompt_line)
    if outcome.matched_target:
        return outcome.password
    if outcome.reason == "jump-host password prompt":
        raise DiscoveryError(
            f"Device '{device_id}': the jump host prompted for an interactive password, which "
            "Discovery's automated bootstrap login does not support. Configure key/agent-based "
            "(non-interactive) SSH authentication for the jump host, or use direct SSH for Discovery."
        )
    raise DiscoveryError(
        f"Device '{device_id}': could not safely determine whether the password prompt belongs to the "
        "jump host or the target device; refusing to guess which credential to send. Configure "
        "key/agent-based (non-interactive) SSH authentication for the jump host."
    )


class _RunCancelled(DiscoveryError):
    """Raised inside a worker that was refused permission to start a
    credential send (or to start at all) because the run was cancelled."""


class _RunControl:
    """Per-run cancellation with an admission gate. `cancel()` and
    `admit()` take the same small lock, so once cancel() has returned no
    later admit() can succeed: after cancellation is decided, the number of
    newly started credential sends is exactly zero. Sends admitted before
    that point are already in flight and finish through their own bounded
    paths (nothing is retroactively un-sent). Never held across any I/O,
    so devices are never serialized by it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self.admitted = 0
        self.refused = 0
        self.admitted_after_cancel = 0

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def admit(self) -> None:
        with self._lock:
            if self._cancelled:
                self.refused += 1
                raise _RunCancelled("Discovery was cancelled; no further credential is sent.")
            self.admitted += 1


_ACTIVE_RUN: "_RunControl | None" = None


def _admit_credential_send(device_id: str) -> None:
    run = _ACTIVE_RUN
    if run is not None:
        run.admit()


# Set only by discover_topology(hostkeys=...) -- i.e. only by the interactive human CLI.
_ACTIVE_HOSTKEYS: "hostkey_flow.Coordinator | None" = None


def _open_bootstrap(device_id: str, device_config: dict) -> None:
    """Open the bootstrap session; with a host-key coordinator active every
    SSH hop runs under StrictHostKeyChecking=yes (keys were established
    beforehand by the coordinator; nothing is ever auto-accepted)."""
    co = _ACTIVE_HOSTKEYS
    if co is not None and device_config.get("transport") == "ssh":
        command = hostkey_flow.build_ssh_command(device_config, files=co.strict_files())
        terminal.open_bootstrap_terminal(device_id, device_config, ssh_command=command)
    else:
        terminal.open_bootstrap_terminal(device_id, device_config)


def _wait_first(device_id: str, device_config: dict, wait_re: re.Pattern) -> str:
    try:
        return terminal.wait_for_bootstrap_pattern(device_id, wait_re, LOGIN_TIMEOUT_SECONDS)
    except terminal.TerminalError:
        _raise_if_host_key_failure(device_id, device_config)
        raise


def _raise_if_host_key_failure(device_id: str, device_config: dict) -> None:
    try:
        text = terminal.read_bootstrap(device_id)
    except terminal.TerminalError:
        return
    changed = hostkey_flow.detect_changed(text)
    if changed is not None:
        hops = {(h.host, h.port): h for h in hostkey_flow.hops_for(device_config)}
        hop = hops.get(changed) if changed != ("", 0) else next(
            (h for h in hostkey_flow.hops_for(device_config) if h.role == "target"), None)
        if hop is None:
            raise DiscoveryError(f"Device '{device_id}': an SSH host key changed on an unexpected host; nothing was sent.")
        err = hostkey_flow.HostKeyChanged(device_id, hop.role, hop.host, hop.port)
        err.device_config = device_config
        raise err
    known = hostkey_flow.tofu.classify_ssh_failure(text)
    if known is not None and known[0] in (hostkey_flow.tofu.HOST_KEY_ALGORITHM_MISMATCH, hostkey_flow.tofu.SSH_KEX_MISMATCH):
        # Algorithm negotiation failed before any credential was sent: an
        # ordinary connection failure of this one device, not a trust problem.
        raise DeviceCollectionError(
            f"Device '{device_id}': [{known[0]}] {known[1]}. The host key was registered (if it was new) but the "
            "connection failed during algorithm negotiation; no credentials were sent."
        )
    if hostkey_flow.detect_verification_failure(text):
        raise DiscoveryError(
            f"Device '{device_id}': SSH host key verification failed (the key is not registered or not usable); "
            "no credentials were sent. Re-run `discover topology`, or register the key manually with ssh."
        )


def _require_trusted_hops(device_id: str) -> None:
    """With the host-key coordinator active, no credential is sent unless
    every hop of the device is trusted (also guaranteed structurally by
    StrictHostKeyChecking=yes on every hop)."""
    _admit_credential_send(device_id)  # cancellation gate: before every credential send
    if _ACTIVE_HOSTKEYS is None:
        return
    try:
        _ACTIVE_HOSTKEYS.require_trusted(device_id)
    except hostkey_flow.HostKeyError as exc:
        raise DiscoveryError(str(exc)) from None


def _answer_jump_stage(device_id: str, device_config: dict, text: str, prompt_re: re.Pattern) -> str:
    """For a ProxyJump device, answer the jump-server password prompt (once,
    via terminal's shared stage logic) and return the pane text to continue
    the target stage with. No-op for direct connections."""
    if not device_config.get("jump_host_config"):
        return text
    last_line = _last_nonblank_line(text)
    if not terminal.PASSWORD_PROMPT_RE.search(last_line):
        return text
    _require_trusted_hops(device_id)
    try:
        text, last_line = terminal.authenticate_bootstrap_jump_stage(device_id, device_config, text, last_line)
    except terminal.TerminalError as exc:
        raise DiscoveryError(str(exc)) from None
    if not last_line:  # no further prompt: e.g. key-based target authentication
        text = terminal.wait_for_bootstrap_pattern(device_id, prompt_re, LOGIN_TIMEOUT_SECONDS)
    return text


def _login(device_id: str, device_config: dict) -> str:
    """Open the bootstrap session, answer at most one password prompt, and
    return the resolved hostname once the IOS XR prompt is seen. Does not
    automate a host-key confirmation prompt: every SSH hop runs with
    StrictHostKeyChecking=yes (terminal._build_transport_command()), so an
    unknown or changed key stops Discovery before any credential is sent."""
    _open_bootstrap(device_id, device_config)
    text = _wait_first(device_id, device_config, _LOGIN_WAIT_RE)
    text = _answer_jump_stage(device_id, device_config, text, _IOSXR_PROMPT_RE)
    last_line = _last_nonblank_line(text)
    if terminal.PASSWORD_PROMPT_RE.search(last_line):
        password = _resolve_login_password(device_id, device_config, last_line)
        _require_trusted_hops(device_id)
        terminal.send_secret_to_bootstrap(device_id, password)
        text = terminal.wait_for_bootstrap_pattern(device_id, _IOSXR_PROMPT_RE, LOGIN_TIMEOUT_SECONDS)
    match = _IOSXR_PROMPT_RE.search(_last_nonblank_line(text))
    if not match:
        raise DeviceCollectionError(f"Device '{device_id}': did not reach an IOS XR prompt after login.")
    return match.group("hostname")


def _login_ios_style(device_id: str, device_config: dict) -> str:
    """Shared classic-IOS-style login for both IOS XE *and* classic IOS
    (named generically rather than `_login_iosxe()` since the same login
    sequence genuinely applies to both). The same bounded, at-most-one-
    password-send flow as _login(), but for classic-IOS-style login
    instead of IOS XR's. Works over either transport the device's
    access-info specifies (ssh or telnet, both already handled uniformly
    by terminal.open_bootstrap_terminal() -- see
    _build_transport_command()). A telnet device structurally cannot have
    a jump_host_config (lab.py's schema validation requires transport
    'ssh' for that), so resolve_target_password_prompt()'s "no
    jump_host_config -> unambiguous" short-circuit already answers a
    telnet password prompt correctly with no telnet-specific attribution
    logic needed.

    Also answers at most one optional "Username:" prompt, which only some
    IOS/IOS XE login configurations show before "Password:"."""
    _open_bootstrap(device_id, device_config)
    text = _wait_first(device_id, device_config, _IOS_STYLE_LOGIN_WAIT_RE)
    text = _answer_jump_stage(device_id, device_config, text, _IOS_STYLE_PROMPT_RE)
    last_line = _last_nonblank_line(text)
    if _USERNAME_PROMPT_RE.search(last_line):
        username = device_config.get("username")
        if not username:
            raise DeviceCollectionError(
                f"Device '{device_id}': a username prompt appeared but no username is configured in "
                "the active access-info definition."
            )
        _require_trusted_hops(device_id)
        terminal.send_secret_to_bootstrap(device_id, str(username))
        text = terminal.wait_for_bootstrap_pattern(
            device_id, _IOS_STYLE_LOGIN_WAIT_RE, LOGIN_TIMEOUT_SECONDS, baseline_text=text
        )
        last_line = _last_nonblank_line(text)
    if terminal.PASSWORD_PROMPT_RE.search(last_line):
        password = _resolve_login_password(device_id, device_config, last_line)
        _require_trusted_hops(device_id)
        terminal.send_secret_to_bootstrap(device_id, password)
        text = terminal.wait_for_bootstrap_pattern(device_id, _IOS_STYLE_PROMPT_RE, LOGIN_TIMEOUT_SECONDS)
    last_prompt_line = _last_nonblank_line(text)
    match = _IOS_STYLE_PROMPT_RE.search(last_prompt_line)
    if not match:
        raise DeviceCollectionError(f"Device '{device_id}': did not reach an exec prompt after login.")
    hostname = match.group("hostname")
    if last_prompt_line.rstrip().endswith(">") and terminal.enable_applicable(device_config):
        _enable_privileged_exec(device_id, device_config, hostname)
    return hostname


def _enable_privileged_exec(device_id: str, device_config: dict, hostname: str) -> None:
    """Privileged EXEC authentication for an ios/iosxe Discovery session at
    the user EXEC prompt, only because an enable_password is configured.
    Ordinary failures become a continuable per-device warning (the device is
    still registered as connected, status partial); a credential echo or a
    lost pane ownership is fatal for the whole run. Fixed messages only."""
    outcome = None
    try:
        terminal.authenticate_bootstrap_enable(
            device_id, hostname, device_config["enable_password"], admit=lambda: _admit_credential_send(device_id)
        )
    except terminal.CredentialEchoError:
        outcome = "echo"
    except terminal.PaneOwnershipError:
        outcome = "ownership"
    except terminal.EnableAuthenticationError as exc:
        outcome = exc.reason
    if outcome is None:
        return
    if outcome == "echo":
        raise DiscoveryError(f"Device '{device_id}': Unexpected credential echo during authentication.")
    if outcome == "ownership":
        raise DiscoveryError(f"Device '{device_id}': {terminal.ENABLE_OWNERSHIP_MESSAGE}")
    if outcome == "timeout":
        message = "Timed out while waiting for privileged exec prompt."
    else:
        message = "Privileged EXEC authentication failed."
    raise DeviceCollectionError(
        f"Device '{device_id}': {message}",
        collected={"hostname": hostname},
        session_ended=outcome == "session_ended",
    )


def _extract_command_output(full_text: str, command_text: str, prompt_re: re.Pattern) -> str:
    """Slice out one command's own output from the full pane transcript:
    everything after the line that echoes the command, up to (excluding)
    the trailing prompt line(s). `prompt_re` is the device-type-specific
    "end of output" prompt (IOS XR's or IOS XE's)."""
    lines = full_text.splitlines()
    needle = command_text.strip()
    start = 0
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].rstrip().endswith(needle):
            start = i + 1
            break
    end = len(lines)
    while end > start and (not lines[end - 1].strip() or prompt_re.search(lines[end - 1])):
        end -= 1
    return "\n".join(lines[start:end])


def _run_command(
    device_id: str,
    command_text: str,
    prompt_re: re.Pattern = _IOSXR_PROMPT_RE,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> str:
    """Send one command line and wait for the device's own prompt to
    return. `prompt_re` defaults to IOS XR's prompt (unchanged call sites);
    IOS XE collection passes _IOSXE_PROMPT_RE instead.

    Captures the pane *before* sending so wait_for_bootstrap_pattern() can
    require the pane to have actually changed before accepting a prompt
    match -- otherwise a prompt already sitting in the pane from the
    *previous* command could satisfy the wait immediately, before this
    command produced any output at all (a stale-prompt race)."""
    baseline = terminal.read_bootstrap(device_id)
    terminal.send_to_bootstrap(device_id, command_text, None, True)
    full_text = terminal.wait_for_bootstrap_pattern(device_id, prompt_re, timeout, baseline_text=baseline)
    return _extract_command_output(full_text, command_text, prompt_re)


def _disable_terminal_paging(device_id: str, prompt_re: re.Pattern) -> None:
    """Send `terminal length 0` immediately after successful login and
    confirm the device prompt has returned before any Discovery show
    command is sent.

    This is the fix for a real observed failure: a C9200L (IOS XE) ran
    `show version` before pagination was disabled, its output stopped at
    the device's own `--More--` pager prompt (which never printed the
    expected exec prompt back), and the whole device's collection timed
    out and failed. Disabling the pager up front -- rather than teaching
    Discovery to detect and answer `--More--` -- keeps this a single,
    narrow, one-time step reusing the exact same send/wait-for-prompt
    primitive (`_run_command()`) every other command already uses, with
    no separate pager state machine.

    `terminal length 0` is sent exactly once per session (each collector
    calls this exactly once, right after login) and is a session-level
    EXEC setting only -- never persisted, never entered via configuration
    mode. Fails closed: if the prompt does not return, this raises
    (converted to DiscoveryError by the caller, exactly like any other
    login/command failure) with a message that identifies *this* phase
    specifically, and no Discovery show command is ever attempted while
    the terminal's page-length state is unknown."""
    try:
        _run_command(device_id, "terminal length 0", prompt_re)
    except terminal.TerminalError as exc:
        raise terminal.TerminalError(f"Timed out disabling terminal paging: {exc}") from exc


def _collect_device(
    device_id: str,
    device_config: dict,
    login,
    paging_prompt_re: re.Pattern,
    commands: list[tuple[str, str]],
    prompt_re: re.Pattern | None,
) -> dict:
    """Shared collection body: log in, disable paging, then run each read-only
    `(key, command)` in order, recording each command's output only once the
    device prompt has returned. A continuable failure (TerminalError, or a
    DeviceCollectionError from login) is re-raised as a DeviceCollectionError
    that carries everything collected *before* it, so one late command
    failing never discards the earlier, completed results; the interrupted
    command's own partial output is never recorded. Security-relevant
    DiscoveryErrors (host keys, ambiguous credential target) propagate
    unchanged. The caller closes the bootstrap session either way."""
    collected: dict = {}
    try:
        collected["hostname"] = login(device_id, device_config)
        # Discovery sessions are temporary and closed right after
        # collection, so there is no need to restore terminal length
        # afterwards (see docs/architecture.md).
        _disable_terminal_paging(device_id, paging_prompt_re)
        for key, command in commands:
            if prompt_re is None:
                collected[key] = _run_command(device_id, command)
            else:
                collected[key] = _run_command(device_id, command, prompt_re)
    except DeviceCollectionError as exc:
        # keep anything the raising step itself attached (e.g. the login
        # hostname when only privileged-EXEC authentication failed)
        exc.collected = {**exc.collected, **collected}
        raise
    except terminal.TerminalError as exc:
        raise DeviceCollectionError(
            f"Device '{device_id}': {exc}",
            collected=collected,
            session_ended=_SESSION_EXITED_MARKER in str(exc),
        ) from exc
    return collected


def _bootstrap_collect(device_id: str, device_config: dict, include_l3: bool = False) -> dict:
    """IOS XR collection: `show version` (identity/diagnostic parity), both
    neighbor-discovery protocols (IOS XR gets LLDP and CDP), and `show ipv4
    interface brief` (L3 enrichment, only when `include_l3`). `show
    running-config` is deliberately not collected: nothing in Discovery ever
    read it, and a device's complete configuration would only persist
    unnecessary detail into the terminal log. A continuable failure raises
    DeviceCollectionError carrying the results completed so far (see
    _collect_device())."""
    commands = [
        ("show_version", "show version"),
        ("show_lldp_neighbors", "show lldp neighbors"),
        ("show_cdp_neighbors", "show cdp neighbors"),
    ]
    if include_l3:
        commands.append(("show_ipv4_interface_brief", "show ipv4 interface brief"))
    return _collect_device(device_id, device_config, _login, _IOSXR_PROMPT_RE, commands, None)


def _bootstrap_collect_iosxe(device_id: str, device_config: dict, include_l3: bool = False) -> dict:
    """IOS XE collection: `show version`, LLDP and CDP, and the L3 commands
    `show vrf` + `show ip interface brief` (only when `include_l3`).
    Pagination is disabled first (see _disable_terminal_paging()'s docstring
    for the real C9200L failure this fixes). `% LLDP is not enabled` is
    classified by the caller, not here."""
    commands = [
        ("show_version", "show version"),
        ("show_lldp_neighbors", "show lldp neighbors"),
        ("show_cdp_neighbors", "show cdp neighbors"),
    ]
    if include_l3:
        commands += [("show_vrf", "show vrf"), ("show_ip_interface_brief", "show ip interface brief")]
    return _collect_device(device_id, device_config, _login_ios_style, _IOS_STYLE_PROMPT_RE, commands, _IOS_STYLE_PROMPT_RE)


def _bootstrap_collect_ios(device_id: str, device_config: dict, include_l3: bool = False) -> dict:
    """Classic IOS collection (deliberately minimal): `show version`, `show
    cdp neighbors` (classic IOS has no LLDP support), and the L3 commands
    `show vrf` + `show ip interface brief` (only when `include_l3`)."""
    commands = [("show_version", "show version"), ("show_cdp_neighbors", "show cdp neighbors")]
    if include_l3:
        commands += [("show_vrf", "show vrf"), ("show_ip_interface_brief", "show ip interface brief")]
    return _collect_device(device_id, device_config, _login_ios_style, _IOS_STYLE_PROMPT_RE, commands, _IOS_STYLE_PROMPT_RE)


# --------------------------------------------------------------------------
# LLDP + CDP parsing -- normalized observations only, no identity resolution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LldpObservation:
    """A single normalized neighbor observation from either protocol
    (`source` distinguishes them: "lldp" or "cdp") -- despite the name
    (kept to avoid an unnecessary rename of an already-public, widely
    tested type), this is the shared observation shape CDP support reuses
    as-is rather than inventing a second, parallel type;
    see the `NeighborObservation` alias below for new code."""

    local_device_id: str
    local_interface: str
    remote_device_id_raw: str
    remote_port_id: str
    capabilities: tuple[str, ...] = ()
    source: str = "lldp"


# New CDP-facing code should spell it this way; both names are the exact
# same class.
NeighborObservation = LldpObservation

_CAPABILITY_CODES = {
    "R": "router",
    "B": "bridge",
    "T": "telephone",
    "C": "docsis_cable_device",
    "W": "wlan_access_point",
    "P": "repeater",
    "S": "station",
    "O": "other",
}

# CDP's own "Capability Codes" legend uses different letters/meanings than
# LLDP's (e.g. CDP's "T" is "Trans Bridge", not "telephone"; CDP's repeater
# code is lowercase "r", not "P") -- a separate mapping, not a reuse of
# LLDP's, to avoid mislabeling. The letter set here is a safe superset of
# what real Cisco CDP output uses; an unmapped letter is kept as-is rather
# than dropped (see _normalize_capability_chars()).
_CDP_CAPABILITY_CODES = {
    "R": "router",
    "T": "trans_bridge",
    "B": "source_route_bridge",
    "S": "switch",
    "H": "host",
    "I": "igmp",
    "r": "repeater",
    "P": "phone",
    "D": "remote",
    "C": "cvta",
    "M": "two_port_mac_relay",
}
# Every single-character CDP capability code, used only to distinguish a
# capability token (e.g. "S", "I") from the start of the Platform field
# during row parsing (see parse_cdp_neighbors()) -- not used for the
# mapping itself.
_CDP_CAPABILITY_LETTERS = frozenset(_CDP_CAPABILITY_CODES)

_TOTAL_ENTRIES_RE = re.compile(r"total entries displayed:\s*(\d+)", re.IGNORECASE)


def _normalize_capability_chars(raw: str, codes: dict[str, str]) -> tuple[str, ...]:
    return tuple(codes.get(ch, ch) for ch in raw.strip() if ch.strip())


def _normalize_capabilities(raw: str) -> tuple[str, ...]:
    return _normalize_capability_chars(raw, _CAPABILITY_CODES)


class LldpParseError(Exception):
    """Raised when `show lldp neighbors` output cannot be recognized as
    valid IOS XR LLDP structure, or when its own declared entry count
    disagrees with what was actually parsed -- this is the fail-closed
    signal that keeps a failed/unrecognized command (e.g. "% Invalid input
    detected...") from ever being silently treated as a legitimate
    zero-neighbor response."""


# IOS XR's two confirmed "protocol not enabled" responses. Deliberately an
# exact, per-protocol allowlist (no substring search, no other wording): the
# only thing recognized as "disabled" is output whose *entire* meaningful
# content is the one expected line for that protocol. Anything else that
# merely looks like a disabled message (wrong protocol, extra lines, mixed
# with a table) is a DiscoveryError, never silently zero observations.
_IOSXR_DISABLED_MESSAGES = {
    "lldp": "% LLDP is not enabled",
    "cdp": "% CDP is not enabled",
}
_DISABLED_LIKE_RE = re.compile(r"\b(?:lldp|cdp)\b.*\bnot enabled\b", re.IGNORECASE)
_CLI_TIMESTAMP_RE = re.compile(r"^[A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2}(?:\.\d+)? \S+$")
_IOSXR_PROMPT_OR_ECHO_RE = re.compile(r"^RP/\S+/CPU\d+:[^#\s]+#\s*(?:show (?:lldp|cdp) neighbors)?\s*$")


def iosxr_protocol_disabled(raw_text: str, protocol: str, local_device_id: str) -> bool:
    """True only if IOS XR's output for `protocol` ("lldp" or "cdp") is
    exactly that protocol's known "not enabled" response (ignoring blank
    lines, the CLI timestamp line, and a bare prompt/command-echo line).
    False if no disabled-like text is present at all (caller parses
    normally). Raises DiscoveryError if disabled-like text is present but
    is not solely the exact expected message -- the wrong protocol's
    message, additional content, or a mixed table -- since that is not a
    state this code has evidence for."""
    meaningful = [
        line
        for line in (raw.strip() for raw in raw_text.splitlines())
        if line and not _CLI_TIMESTAMP_RE.match(line) and not _IOSXR_PROMPT_OR_ECHO_RE.match(line)
    ]
    if not any(_DISABLED_LIKE_RE.search(line) for line in meaningful):
        return False
    if meaningful == [_IOSXR_DISABLED_MESSAGES[protocol]]:
        return True
    raise ProtocolOutputError(
        f"Device '{local_device_id}': 'show {protocol} neighbors' output contains a protocol-disabled "
        "message that is not exactly the expected one for this command; refusing to interpret it."
    )


def parse_lldp_neighbors(raw_text: str, local_device_id: str) -> list[LldpObservation]:
    """Parse `show lldp neighbors` output into normalized observations.

    Deliberately tolerant of real IOS XR output variation: the timestamp
    line and capability-codes legend are optional/ignored, column spacing
    is not depended on (each data row is split on whitespace, not fixed
    columns), and a trailing device prompt or a blank line ends the table
    just as reliably as "Total entries displayed: N". A row that doesn't
    split into exactly the 5 expected fields is skipped rather than raising
    -- this parser only ever produces raw observations, never resolves a
    remote Device ID to a logical managed device (see
    resolve_remote_identity() for that separate stage).

    Raises LldpParseError (never returns a misleading empty list) when:
    - the "Device ID ... Local Intf ..." table header is never recognized
      at all (e.g. invalid/unrecognized command output such as
      "% Invalid input detected..." -- structurally indistinguishable from
      a legitimate zero-neighbor response unless this is checked); or
    - a "Total entries displayed: N" line is present and disagrees with
      the number of rows actually parsed.

    A legitimate zero-neighbor response (header present, no data rows, and
    either no "Total entries displayed" line or one that says 0) still
    returns an empty list successfully."""
    observations: list[LldpObservation] = []
    in_table = False
    declared_total: int | None = None
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not in_table:
            if line.startswith("Device ID") and "Local Intf" in line:
                in_table = True
            continue
        if not line:
            break
        total_match = _TOTAL_ENTRIES_RE.match(line)
        if total_match:
            declared_total = int(total_match.group(1))
            break
        if _IOSXR_PROMPT_RE.search(line):
            break
        parts = line.split()
        if len(parts) != 5:
            continue
        device_id_raw, local_intf, _hold_time, capability_raw, port_id = parts
        observations.append(
            LldpObservation(
                local_device_id=local_device_id,
                local_interface=local_intf,
                remote_device_id_raw=device_id_raw,
                remote_port_id=port_id,
                capabilities=_normalize_capabilities(capability_raw),
            )
        )
    if not in_table:
        raise LldpParseError(
            f"Device '{local_device_id}': 'show lldp neighbors' output was not recognized as valid "
            "IOS XR LLDP structure (no 'Device ID ... Local Intf' table header found)."
        )
    if declared_total is not None and declared_total != len(observations):
        raise LldpParseError(
            f"Device '{local_device_id}': 'Total entries displayed: {declared_total}' does not match "
            f"{len(observations)} parsed neighbor row(s)."
        )
    return observations


_CDP_HEADER_MARKER = "Local Intrfce"


def _cdp_is_capability_token(token: str) -> bool:
    return bool(token) and all(ch in _CDP_CAPABILITY_LETTERS for ch in token)


def parse_cdp_neighbors(raw_text: str, local_device_id: str) -> list[NeighborObservation]:
    """Parse `show cdp neighbors` output into normalized observations.

    Handles both real-world row shapes:
      - IOS XR-style, entirely on one line:
        "LegacyRouter    Gi0/0/0/10       144     R          Cisco 720 Gi0/0"
      - IOS/IOS XE-style, where a long/FQDN Device ID wraps onto its own
        line and the remaining fields follow on the *next* physical line:
        "external-switch.example.com"
        "                 Fas 0/0            159             S I   WS-C2960X Gig 1/0/16"

    Never assumes fixed column byte offsets. Since both Local Interface,
    Platform, and Port ID can each be multiple whitespace-separated tokens
    (e.g. "Fas 0/0", "ASR9K Ser", "Gig 1/0/16"), plain column-count
    splitting (as LLDP's parser uses) is not reliable here; instead, a
    field row is recognized by containing a bare-integer Holdtime token,
    which reliably splits "Device ID + Local Interface" (before it) from
    "Capability + Platform + Port ID" (after it), and only the Port ID
    field's own column start (read once from the header line) is needed to
    unambiguously split Platform from Port ID.

    Deliberately more lenient than parse_lldp_neighbors(): CDP is commonly
    disabled/unsupported on a given device, so a missing/unrecognized
    table header returns an empty list of observations rather than raising
    (CDP being unavailable must never fail the
    whole device's collection; contrast with LLDP, which is expected to
    always be available and so still fails closed on an unrecognized
    header). A malformed individual row is skipped, never fatal."""
    observations: list[NeighborObservation] = []
    in_table = False
    port_id_col: int | None = None
    pending_device_id: str | None = None

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not in_table:
            if line.startswith("Device ID") and _CDP_HEADER_MARKER in line:
                idx = raw_line.find("Port ID")
                if idx != -1:
                    port_id_col = idx
                    in_table = True
            continue
        if not line:
            break

        tokens = list(re.finditer(r"\S+", raw_line))
        if not tokens:
            continue
        has_holdtime = any(m.group().isdigit() for m in tokens)
        if not has_holdtime:
            # A real wrapped Device-ID-only line (the row wrapped because
            # the Device ID was too long to fit before the Local Interface
            # column) is always exactly one token -- a real Device ID never
            # contains whitespace. A multi-token line with no Holdtime
            # anywhere is unrecognized/malformed input, not a wrapped
            # Device ID -- skip it rather than risk misattributing the
            # *next* row's fields to it.
            if len(tokens) == 1:
                pending_device_id = line
            continue

        if pending_device_id is not None:
            device_id = pending_device_id
            pending_device_id = None
            field_tokens = tokens
        else:
            device_id = tokens[0].group()
            field_tokens = tokens[1:]

        holdtime_idx = next((i for i, m in enumerate(field_tokens) if m.group().isdigit()), None)
        if holdtime_idx is None or holdtime_idx == 0:
            continue  # malformed row (no interface text before Holdtime) -- skip
        local_intf = " ".join(m.group() for m in field_tokens[:holdtime_idx])

        cap_tokens: list[str] = []
        for m in field_tokens[holdtime_idx + 1 :]:
            if not _cdp_is_capability_token(m.group()):
                break
            cap_tokens.append(m.group())
        capability_raw = " ".join(cap_tokens)

        port_id = raw_line[port_id_col:].strip() if port_id_col is not None else ""
        if not port_id:
            continue  # malformed row (no Port ID text) -- skip

        observations.append(
            NeighborObservation(
                local_device_id=local_device_id,
                local_interface=local_intf,
                remote_device_id_raw=device_id,
                remote_port_id=port_id,
                capabilities=_normalize_capability_chars(capability_raw, _CDP_CAPABILITY_CODES),
                source="cdp",
            )
        )

    if not in_table:
        return []
    return observations


# --------------------------------------------------------------------------
# L3 interface enrichment: ipv4_address + vrf only, per
# interface -- topology context only (collected on `discover topology l3`),
# never a link/connectivity source. See the module docstring's "L3 topology
# enrichment" paragraph for the overall design.
# --------------------------------------------------------------------------


class L3ParseError(Exception):
    """Raised when an L3 enrichment command's output cannot be recognized
    at all (its own table header is missing) -- the fail-closed signal
    that keeps a failed/unsupported command from being silently treated as
    "zero interfaces". Caught by discover_topology() and turned into a
    per-device skip + warning, never a whole-Discovery
    failure -- unlike LldpParseError, which still fails the whole
    operation."""


# A conservative, deliberately small set of known Cisco interface-name
# abbreviations, each mapped to its one canonical full name -- used only
# to let `show vrf`'s (possibly abbreviated)
# Interfaces column match `show ip interface brief`'s (full-name) Interface
# column for the same physical interface. An unrecognized prefix is left
# completely unchanged (never guessed) -- this is intentionally not a
# general-purpose interface-alias framework, just the smallest lookup
# needed to reconcile these two specific commands' output.
_INTERFACE_TYPE_ALIASES: dict[str, str] = {
    "gi": "GigabitEthernet",
    "gig": "GigabitEthernet",
    "gigabitethernet": "GigabitEthernet",
    "fa": "FastEthernet",
    "fas": "FastEthernet",
    "fastethernet": "FastEthernet",
    "te": "TenGigabitEthernet",
    "tengigabitethernet": "TenGigabitEthernet",
    "fo": "FortyGigabitEthernet",
    "fortygigabitethernet": "FortyGigabitEthernet",
    "hu": "HundredGigE",
    "hundredgige": "HundredGigE",
    "tw": "TwoGigabitEthernet",
    "twogigabitethernet": "TwoGigabitEthernet",
    "et": "Ethernet",
    "eth": "Ethernet",
    "ethernet": "Ethernet",
    "vl": "Vlan",
    "vlan": "Vlan",
    "lo": "Loopback",
    "loopback": "Loopback",
    "po": "Port-channel",
    "port-channel": "Port-channel",
    "se": "Serial",
    "serial": "Serial",
    "bv": "BVI",
    "bvi": "BVI",
    "tu": "Tunnel",
    "tunnel": "Tunnel",
}
_INTERFACE_PREFIX_RE = re.compile(r"^([A-Za-z-]+)(.*)$")

_DEFAULT_VRF = "default"


def _canonicalize_interface_name(raw: str) -> str:
    """Expand a KNOWN interface-type abbreviation (e.g. "Gi0/0",
    "Fas 0/0") to its one canonical full name ("GigabitEthernet0/0",
    "FastEthernet0/0"), preserving the slot/port/sub-interface suffix
    (including a dotted sub-interface, e.g. ".2000") unchanged. An
    embedded space (as CDP sometimes renders, e.g. "Fas 0/0") is removed
    only as part of a *recognized* rewrite. An unrecognized prefix is
    returned completely unchanged (including any embedded space) -- never
    over-normalized."""
    cleaned = raw.strip().replace(" ", "")
    match = _INTERFACE_PREFIX_RE.match(cleaned)
    if not match:
        return raw.strip()
    prefix, rest = match.group(1), match.group(2)
    canonical = _INTERFACE_TYPE_ALIASES.get(prefix.lower())
    if canonical is None:
        return raw.strip()
    return f"{canonical}{rest}"


def parse_ipv4_interface_brief(raw_text: str) -> dict[str, tuple[str | None, str]]:
    """Parse IOS XR's `show ipv4 interface brief` into
    {interface: (ipv4_address_or_None, vrf_name)}. Only Interface/
    IP-Address/Vrf-Name are read; Status/Protocol are ignored entirely
    regardless of how many words they take (e.g.
    "Shutdown" vs. a multi-word status) -- Vrf-Name is always the *last*
    whitespace token and IP-Address is always the second, which is robust
    to that variation without depending on a fixed token count.

    "unassigned" becomes None -- never the literal string,
    never a fabricated address. Raises L3ParseError only when the
    "Interface ... IP-Address ... Vrf-Name" header itself is never found
    (e.g. an unsupported/mistyped command) -- a malformed individual row is
    skipped, never fatal, and a header-found-but-zero-rows response
    returns an empty dict successfully."""
    interfaces: dict[str, tuple[str | None, str]] = {}
    in_table = False
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not in_table:
            if "Interface" in line and "IP-Address" in line and "Vrf-Name" in line:
                in_table = True
            continue
        if not line:
            break
        tokens = line.split()
        if len(tokens) < 4:
            continue  # malformed row -- skip
        interface_name, ip_address = tokens[0], tokens[1]
        vrf_name = tokens[-1]
        interfaces[interface_name] = (None if ip_address.lower() == "unassigned" else ip_address, vrf_name)
    if not in_table:
        raise L3ParseError(
            "'show ipv4 interface brief' output was not recognized as valid IOS XR structure "
            "(no 'Interface ... IP-Address ... Vrf-Name' table header found)."
        )
    return interfaces


def parse_ip_interface_brief(raw_text: str) -> dict[str, str | None]:
    """Parse classic IOS / IOS XE's `show ip interface brief` into
    {canonical_interface_name: ipv4_address_or_None}. Only Interface/
    IP-Address are read (the first two whitespace tokens); OK?/Method/
    Status/Protocol are ignored entirely regardless of their own word
    count (Status can legitimately be the two-word "administratively
    down", which would otherwise make a fixed-token-count split brittle --
    never needing to parse it at all sidesteps that entirely).

    "unassigned" becomes None. Raises L3ParseError only when
    the "Interface ... IP-Address" header itself is never found."""
    interfaces: dict[str, str | None] = {}
    in_table = False
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not in_table:
            if "Interface" in line and "IP-Address" in line:
                in_table = True
            continue
        if not line:
            break
        tokens = line.split()
        if len(tokens) < 2:
            continue  # malformed row -- skip
        interface_name = _canonicalize_interface_name(tokens[0])
        ip_address = tokens[1]
        interfaces[interface_name] = None if ip_address.lower() == "unassigned" else ip_address
    if not in_table:
        raise L3ParseError(
            "'show ip interface brief' output was not recognized as valid structure "
            "(no 'Interface ... IP-Address' table header found)."
        )
    return interfaces


def parse_show_vrf(raw_text: str) -> dict[str, str]:
    """Parse classic IOS / IOS XE's `show vrf` into
    {canonical_interface_name: vrf_name} -- only non-default VRF
    membership; an interface never listed here is assumed `default` by the
    caller, never guessed here.

    The Name/Default-RD/Protocols/Interfaces columns are not fixed-width
    (e.g. "<not set>" is itself two whitespace tokens), so each row is read
    as: VRF name = the *first* token, interface = the *last* token,
    tolerating any number of tokens in between. Some IOS versions wrap a
    VRF's additional interfaces onto their own continuation line with the
    VRF-name column blank -- recognized here as a line containing *only*
    an interface token, associated with the immediately preceding VRF
    name.

    Raises L3ParseError only when the "Name ... Interfaces" header itself
    is never found; a header-found-but-zero-non-default-VRF response
    returns an empty dict successfully (this is the common case: most
    interfaces belong to the default VRF, which never appears here)."""
    vrf_by_interface: dict[str, str] = {}
    in_table = False
    pending_vrf_name: str | None = None
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not in_table:
            if "Name" in line and "Interfaces" in line:
                in_table = True
            continue
        if not line:
            break
        tokens = line.split()
        if len(tokens) == 1:
            if pending_vrf_name is not None:
                vrf_by_interface[_canonicalize_interface_name(tokens[0])] = pending_vrf_name
            continue
        vrf_name = tokens[0]
        interface_name = tokens[-1]
        vrf_by_interface[_canonicalize_interface_name(interface_name)] = vrf_name
        pending_vrf_name = vrf_name
    if not in_table:
        raise L3ParseError(
            "'show vrf' output was not recognized as valid structure (no 'Name ... Interfaces' "
            "table header found)."
        )
    return vrf_by_interface


def _combine_ios_style_l3(
    ip_brief: dict[str, str | None], vrf_by_interface: dict[str, str]
) -> dict[str, tuple[str | None, str]]:
    """Combine classic IOS/IOS XE's two required L3 sources:
    every interface `show ip interface brief` reports, paired with its VRF
    from `show vrf` if listed there, else the literal `default` -- never
    guessed as default when `show vrf` itself failed to parse (the caller
    only reaches this once *both* sources parsed successfully; see
    discover_topology())."""
    return {name: (ipv4, vrf_by_interface.get(name, _DEFAULT_VRF)) for name, ipv4 in ip_brief.items()}


def _build_device_interface_fields(
    raw_l3: dict[str, tuple[str | None, str]], management_address: str | None
) -> dict[str, dict | None]:
    """Turn one device's raw (interface -> (ipv4_or_None, vrf)) observation
    into the topology candidate's own {interface: {ipv4_address, vrf} |
    None} shape: a real, non-management-address
    observation becomes a dict to set; an explicitly-observed `unassigned`
    interface, or one whose address matches the access-info connection
    address for this same device (never persist the
    management address into topology L3 data), becomes an explicit `None`
    removal signal (build_discovery_candidate() leaves it out of the
    rebuilt candidate)."""
    fields: dict[str, dict | None] = {}
    for raw_name, (ipv4_address, vrf) in raw_l3.items():
        canonical = _canonicalize_interface_name(raw_name)
        if ipv4_address is None or ipv4_address == management_address:
            fields[canonical] = None
        else:
            fields[canonical] = {"ipv4_address": ipv4_address, "vrf": vrf}
    return fields


# --------------------------------------------------------------------------
# Identity resolution + link reconciliation
# --------------------------------------------------------------------------


def resolve_remote_identity(remote_device_id_raw: str, identity_map: dict[str, str]) -> str | None:
    """Resolve an LLDP remote Device ID to a logical managed device ID, or
    None if unresolved/ambiguous (fail closed -- never a guess).

    `identity_map` maps logical_device_id -> observed_hostname. Matching
    order, each step requiring a *unique* match:
      1. exact observed-hostname match
      2. case-normalized exact match
      3. short-name/FQDN-style alias match (the raw ID's segment before its
         first '.', compared case-insensitively against each hostname)

    No substring search, no fuzzy matching, and no inference from the
    logical device ID itself (e.g. never "R2" in device_id)."""

    def _unique(matches: list[str]) -> str | None:
        return matches[0] if len(matches) == 1 else None

    exact = [logical for logical, hostname in identity_map.items() if hostname == remote_device_id_raw]
    result = _unique(exact)
    if result is not None or len(exact) > 1:
        return result

    lowered = remote_device_id_raw.lower()
    case_insensitive = [logical for logical, hostname in identity_map.items() if hostname.lower() == lowered]
    result = _unique(case_insensitive)
    if result is not None or len(case_insensitive) > 1:
        return result

    short_name = remote_device_id_raw.split(".", 1)[0].lower()
    alias = [logical for logical, hostname in identity_map.items() if hostname.lower() == short_name]
    return _unique(alias)


@dataclass(frozen=True)
class ManagedLink:
    a_device: str
    a_interface: str
    b_device: str
    b_interface: str


@dataclass(frozen=True)
class LinkConflict:
    endpoint_a: tuple[str, str]
    endpoint_b: tuple[str, str]
    observation_a: LldpObservation
    observation_b: LldpObservation


@dataclass(frozen=True)
class UnresolvedNeighbor:
    remote_device_id_raw: str
    local_device_id: str
    local_interface: str
    remote_port_id: str
    capabilities: tuple[str, ...]


def reconcile_links(
    resolved_observations: list[tuple[NeighborObservation, str]],
) -> tuple[list[ManagedLink], list[LinkConflict]]:
    """Deduplicate reciprocal, multi-protocol observations into physical
    links.

    `resolved_observations` is a list of (observation, resolved_remote_id)
    pairs from either protocol, already filtered to observations whose
    remote resolved uniquely to a managed device (see
    resolve_remote_identity()) -- the protocol that produced each
    observation is irrelevant to this function beyond its own `source`
    field, which callers may use for diagnostics. A physical link is keyed
    by its unordered pair of (device, interface) endpoints, so two parallel
    links between the same router pair on different interfaces stay
    distinct.

    Two conflict cases are both reported (never silently resolved by
    picking one side), and both fail closed only for the specific local
    interface(s) involved -- an unrelated link elsewhere still reconciles
    normally:
      1. A reciprocal pair that disagrees about the interface mapping.
      2. *One* local interface has more than one
         observation -- whether from different protocols (LLDP says one
         neighbor, CDP says a different one) or the same protocol
         producing incompatible rows -- that do not all agree on the same
         (remote device, remote interface). Corroborating observations
         (same protocol or not, same remote endpoint) are not a conflict;
         they collapse into the same single candidate link."""
    # Endpoint identity is (device, canonical interface name), so one port
    # spelled "Gi0/0/0/10" (LLDP) and "Gig 0/0/0/10" (CDP) is one endpoint.
    # Canonicalization is the existing known-abbreviation table only; an
    # unrecognized spelling stays distinct (never guessed). Raw spellings
    # are kept for display: a device's own spelling of its interface (as a
    # local interface of one of its observations) beats a neighbor's
    # spelling of it, then the lexicographically smallest raw name wins, so
    # the result never depends on observation order.
    def ident(device: str, interface: str) -> tuple[str, str]:
        return (device, _canonicalize_interface_name(interface))

    spellings: dict[tuple[str, str], set[tuple[int, str]]] = {}
    for obs, remote_id in resolved_observations:
        spellings.setdefault(ident(obs.local_device_id, obs.local_interface), set()).add((0, obs.local_interface))
        spellings.setdefault(ident(remote_id, obs.remote_port_id), set()).add((1, obs.remote_port_id))

    def shown(endpoint: tuple[str, str]) -> tuple[str, str]:
        return (endpoint[0], min(spellings[endpoint])[1])

    by_local_endpoint: dict[tuple[str, str], list[tuple[NeighborObservation, str]]] = {}
    for obs, remote_id in resolved_observations:
        by_local_endpoint.setdefault(ident(obs.local_device_id, obs.local_interface), []).append((obs, remote_id))

    links: list[ManagedLink] = []
    conflicts: list[LinkConflict] = []
    # Endpoints are consumed by their own dict key (local_dev, local_intf),
    # never by a "canonical pair" derived from a claimed remote port --
    # in a conflict the two sides claim *different* remote ports, so only
    # marking each side's own key reliably prevents processing the same
    # physical endpoint twice.
    seen_endpoints: set[tuple[str, str]] = set()

    resolved_by_endpoint: dict[tuple[str, str], tuple[NeighborObservation, str]] = {}
    for endpoint, obs_list in by_local_endpoint.items():
        first_obs, first_remote_id = obs_list[0]
        first_remote = ident(first_remote_id, first_obs.remote_port_id)
        agree = all(ident(remote_id, obs.remote_port_id) == first_remote for obs, remote_id in obs_list)
        if not agree:
            second_obs, _second_remote_id = next(
                (o, r) for o, r in obs_list if ident(r, o.remote_port_id) != first_remote
            )
            conflicts.append(LinkConflict(shown(endpoint), shown(endpoint), first_obs, second_obs))
            seen_endpoints.add(endpoint)
            continue
        resolved_by_endpoint[endpoint] = (first_obs, first_remote_id)

    for endpoint_a, (obs, remote_id) in resolved_by_endpoint.items():
        if endpoint_a in seen_endpoints:
            continue

        endpoint_b = ident(remote_id, obs.remote_port_id)

        reverse = resolved_by_endpoint.get(endpoint_b)
        if reverse is None:
            # One-sided observation -- still a valid managed link.
            links.append(ManagedLink(*shown(endpoint_a), *shown(endpoint_b)))
            seen_endpoints.add(endpoint_a)
            continue

        reverse_obs, reverse_remote_id = reverse
        if ident(reverse_remote_id, reverse_obs.remote_port_id) == endpoint_a:
            links.append(ManagedLink(*shown(endpoint_a), *shown(endpoint_b)))
        else:
            conflicts.append(LinkConflict(shown(endpoint_a), shown(endpoint_b), obs, reverse_obs))
        seen_endpoints.add(endpoint_a)
        seen_endpoints.add(endpoint_b)

    return links, conflicts


# --------------------------------------------------------------------------
# Top-level orchestration -- builds an in-memory DiscoveryResult
# --------------------------------------------------------------------------


@dataclass
class DeviceOutcome:
    """Everything one run learned about one registered device -- the single
    per-device record behind the Device Discovery Summary and the Warnings.
    Internal and display-only: none of it is persisted into the topology.

    connection: "connected" (login prompt identity confirmed), "failed" (an
    attempt that did not get that far) or "not_attempted". A session that
    ends *after* login stays "connected" with a warning.
    lldp / cdp: "enabled", "disabled", "unsupported", "unknown" (could not be
    confirmed) or "na" (not applicable). `*_ok` means a recognized, fully
    parsed neighbor table was obtained (zero rows included), i.e. its
    observations are usable. l3_state: "not_requested", "ok", "failed" or
    "unknown" (never collected)."""

    device_id: str
    device_type: str | None
    supported: bool
    attempted: bool = False
    connection: str = "not_attempted"
    hostname: str | None = None
    session_ended: bool = False
    lldp: str = "unknown"
    cdp: str = "unknown"
    lldp_ok: bool = False
    cdp_ok: bool = False
    observations: list = field(default_factory=list)
    l3_state: str = "not_requested"
    l3_interfaces: dict | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def protocol_usable(self) -> bool:
        return self.lldp_ok or self.cdp_ok

    @property
    def status(self) -> str:
        """"Complete": connected, identity confirmed, at least one neighbor
        protocol observed (an empty table counts) and no limitation of any
        kind (no warning). Everything else that is still registered is
        "Partial"."""
        if self.connection == "connected" and self.protocol_usable and not self.warnings:
            return "Complete"
        return "Partial"


@dataclass(frozen=True)
class DiscoveryResult:
    access_info_name: str
    default_topology_name: str
    iosxr_target_count: int
    connected_count: int
    observation_count: int
    iosxe_target_count: int = 0
    cdp_observation_count: int = 0
    ios_target_count: int = 0
    l3_enriched_device_count: int = 0
    l3_interface_count: int = 0
    devices: dict[str, dict] = field(default_factory=dict)
    managed_links: list[ManagedLink] = field(default_factory=list)
    unresolved: list[UnresolvedNeighbor] = field(default_factory=list)
    conflicts: list[LinkConflict] = field(default_factory=list)
    identity_map: dict[str, str] = field(default_factory=dict)
    include_l3: bool = False
    # Per registered device, in access-info order, and the flattened,
    # deterministic "<device>: <message>" warning lines.
    outcomes: list[DeviceOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _select_targets_by_type(access_data: dict, wanted_type: str) -> dict[str, dict]:
    """Discovery targets of one normalized device `type`, with the device's
    jump host definition resolved into `jump_host_config`. Devices of any
    other type (including an unrecognized/missing one) are not targets of
    this type; they are still *registered* (see discover_topology())."""
    targets: dict[str, dict] = {}
    devices = access_data.get("devices") or {}
    jump_hosts = access_data.get("jump_hosts") or {}
    for device_id, device_cfg in devices.items():
        device_cfg = device_cfg or {}
        raw_type = device_cfg.get("type")
        if not raw_type:
            continue
        try:
            normalized = lab.normalize_device_type(str(raw_type))
        except lab.LabConfigError:
            continue
        if normalized != wanted_type:
            continue
        resolved = dict(device_cfg)
        jump_ref = device_cfg.get("jump_host")
        if jump_ref and jump_ref in jump_hosts:
            resolved["jump_host_config"] = dict(jump_hosts[jump_ref])
        targets[device_id] = resolved
    return targets


def _select_iosxr_targets(access_data: dict) -> dict[str, dict]:
    return _select_targets_by_type(access_data, "iosxr")


def _select_iosxe_targets(access_data: dict) -> dict[str, dict]:
    return _select_targets_by_type(access_data, "iosxe")


def _select_ios_targets(access_data: dict) -> dict[str, dict]:
    return _select_targets_by_type(access_data, "ios")


def _plan_registration(access_data: dict) -> dict[str, DeviceOutcome]:
    """Device Registration plan: every device defined in the access-info, in
    access-info order, regardless of type, reachability or protocol support.
    Only the device ID and its (already validated) type are ever used; no
    connection detail and no jump host is registered."""
    plan: dict[str, DeviceOutcome] = {}
    for device_id, device_cfg in (access_data.get("devices") or {}).items():
        raw_type = (device_cfg or {}).get("type")
        device_type = None
        if raw_type:
            try:
                device_type = lab.normalize_device_type(str(raw_type))
            except lab.LabConfigError as exc:
                raise DiscoveryError(f"Device '{device_id}': {exc}") from exc
        plan[device_id] = DeviceOutcome(device_id, device_type, supported=device_type in _SUPPORTED_TYPES)
    return plan


_SUPPORTED_TYPES = ("iosxr", "iosxe", "ios")


def _connection_info_missing(device_cfg: dict) -> bool:
    return not device_cfg.get("address") or device_cfg.get("transport") not in ("ssh", "telnet")


def _strip_device_prefix(text: str) -> str:
    return _DEVICE_PREFIX_RE.sub("", text, count=1)


def _apply_neighbor_state(outcome: DeviceOutcome, info: dict) -> None:
    """Classify LLDP and CDP independently from the commands that actually
    completed. Neither protocol's failure affects the other, and a failure
    never raises: it leaves that protocol unusable (its partial or
    unrecognized observations are dropped) and records a warning. "No
    neighbors" (a recognized empty table) is a normal, usable observation."""
    device_id = outcome.device_id
    device_type = outcome.device_type
    if device_type in ("iosxr", "iosxe"):
        lldp_text = info.get("show_lldp_neighbors")
        if lldp_text is None:
            outcome.lldp = "unknown"
        else:
            try:
                disabled = (
                    iosxr_protocol_disabled(lldp_text, "lldp", device_id)
                    if device_type == "iosxr"
                    else bool(_LLDP_UNAVAILABLE_RE.search(lldp_text))
                )
                lldp_observations = [] if disabled else parse_lldp_neighbors(lldp_text, device_id)
            except (LldpParseError, ProtocolOutputError) as exc:
                outcome.lldp = "unknown"
                outcome.warnings.append(f"LLDP observations were not used: {_strip_device_prefix(str(exc))}")
            else:
                if disabled:
                    outcome.lldp = "disabled"
                else:
                    outcome.lldp = "enabled"
                    outcome.lldp_ok = True
                    outcome.observations.extend(lldp_observations)
    else:
        outcome.lldp = "na"

    cdp_text = info.get("show_cdp_neighbors")
    cdp_disabled = False
    cdp_ambiguous: ProtocolOutputError | None = None
    if cdp_text is not None and not _cdp_table_recognized(cdp_text):
        try:
            cdp_disabled = iosxr_protocol_disabled(cdp_text, "cdp", device_id)
        except ProtocolOutputError as exc:
            cdp_ambiguous = exc
    if cdp_text is None:
        outcome.cdp = "unknown"
    elif cdp_ambiguous is not None:
        outcome.cdp = "unknown"
        outcome.warnings.append(f"CDP observations were not used: {_strip_device_prefix(str(cdp_ambiguous))}")
    elif _cdp_table_recognized(cdp_text):
        outcome.cdp = "enabled"
        cdp_observations = parse_cdp_neighbors(cdp_text, device_id)
        if not cdp_observations and _cdp_table_has_body(cdp_text):
            outcome.warnings.append(
                "CDP observations were not used: 'show cdp neighbors' table could not be parsed "
                "(rows present but none recognized)."
            )
        else:
            outcome.cdp_ok = True
            outcome.observations.extend(cdp_observations)
    elif cdp_disabled:
        outcome.cdp = "disabled"
    elif device_type == "iosxr" and _iosxr_lnt_cdp_unsupported(info.get("show_version", ""), cdp_text):
        outcome.cdp = "unsupported"
    else:
        outcome.cdp = "unknown"
        outcome.warnings.append(
            "CDP observations were not used: 'show cdp neighbors' output was not recognized as valid "
            "CDP structure (no 'Device ID ... Local Intrfce ... Port ID' table header found)."
        )


def _apply_l3_state(outcome: DeviceOutcome, info: dict, management_address: str | None) -> None:
    """L3 enrichment is independent of LLDP/CDP: it is classified from its own
    completed commands only. A parse failure drops that device's L3
    observation (with a warning); a command that never completed leaves it
    "unknown" (the collection failure's own warning covers it)."""
    device_type = outcome.device_type
    keys = ("show_ipv4_interface_brief",) if device_type == "iosxr" else ("show_ip_interface_brief", "show_vrf")
    if any(key not in info for key in keys):
        outcome.l3_state = "unknown"
        return
    try:
        if device_type == "iosxr":
            raw_l3 = parse_ipv4_interface_brief(info["show_ipv4_interface_brief"])
        else:
            raw_l3 = _combine_ios_style_l3(
                parse_ip_interface_brief(info["show_ip_interface_brief"]), parse_show_vrf(info["show_vrf"])
            )
    except L3ParseError as exc:
        outcome.l3_state = "failed"
        outcome.warnings.append(f"L3 interface observations were not used: {exc}")
        return
    outcome.l3_state = "ok"
    outcome.l3_interfaces = _build_device_interface_fields(raw_l3, management_address)


def _record_collection(
    outcome: DeviceOutcome,
    info: dict,
    failure: DeviceCollectionError | None,
    include_l3: bool,
    management_address: str | None,
) -> None:
    outcome.attempted = True
    outcome.hostname = info.get("hostname")
    outcome.connection = "connected" if outcome.hostname else "failed"
    if failure is not None:
        outcome.session_ended = failure.session_ended
        outcome.warnings.append(failure.reason)
    if outcome.connection != "connected":
        # Identity was never confirmed: nothing the device printed (if
        # anything) is trusted, so every protocol stays "unknown".
        if include_l3:
            outcome.l3_state = "unknown"
        return
    _apply_neighbor_state(outcome, info)
    if include_l3:
        _apply_l3_state(outcome, info, management_address)
    unavailable = ("disabled", "unsupported", "na")
    if not outcome.protocol_usable and outcome.lldp in unavailable and outcome.cdp in unavailable:
        outcome.warnings.append("No usable neighbor discovery protocol.")


def discover_topology(lab_root=None, include_l3: bool = False, hostkeys=None) -> DiscoveryResult:
    """Run the full Discovery flow against committed running-config's
    selected access-info and return an in-memory DiscoveryResult.

    Never touches the committed candidate/topology/settings -- merging the
    result into a topology candidate (see build_discovery_candidate()) is
    the caller's job (cli/config.py).

    Device Registration, Device Discovery and Link Discovery are separate
    responsibilities. Every device the access-info defines is registered
    (ID and type only), whatever its type, reachability or protocol support.
    Supported devices (IOS XR, IOS XE, classic IOS) are then collected in
    parallel, and each device's outcome is recorded independently: a
    connection/login/timeout failure, a session ending mid-collection, a
    disabled/unsupported/unparsable LLDP or CDP, or a failed L3 collection
    is a per-device (or per-protocol) *warning*, never a failure of the run,
    and results completed before a failure are kept. Types without a
    Discovery implementation (and `type`-less devices) are registered with
    discovery not attempted.

    Still fatal (DiscoveryError, nothing returned): an invalid access-info
    definition, no device at all, anything security relevant (changed or
    unverifiable host key, ambiguous credential target), an unexpected worker
    crash (a bug), and reconciliation conflicts or physical-interface reuse
    in the observed links. Ctrl-C is not a partial success: it propagates."""
    lab_root = lab_root or lab.find_lab_root()
    access_info_name = resolve_default_topology_name(lab_root)
    if not lab.access_info_exists(access_info_name, lab_root):
        raise DiscoveryError(f"Selected access-info '{access_info_name}' does not exist.")
    access_data = lab.load_access_info(access_info_name, lab_root)

    plan = _plan_registration(access_data)
    if not plan:
        raise DiscoveryError(f"No devices found in access-info '{access_info_name}'.")
    iosxr_targets = _select_iosxr_targets(access_data)
    iosxe_targets = _select_iosxe_targets(access_data)
    ios_targets = _select_ios_targets(access_data)
    supported_targets: dict[str, dict] = {**iosxr_targets, **iosxe_targets, **ios_targets}

    all_targets: dict[str, dict] = {}
    for device_id, outcome in plan.items():
        if not outcome.supported:
            if outcome.device_type is None:
                outcome.warnings.append("Device type is not set in access-info; discovery was not attempted.")
            else:
                outcome.warnings.append(
                    f"Device type '{outcome.device_type}' is not supported for discovery; discovery was not attempted."
                )
            outcome.lldp = outcome.cdp = "na"
        elif _connection_info_missing(supported_targets[device_id]):
            outcome.warnings.append("Connection information is incomplete; discovery was not attempted.")
        else:
            all_targets[device_id] = supported_targets[device_id]

    # One device's collection (login + its own read-only commands)
    # still runs strictly sequentially within its own worker -- only
    # *different* devices' collectors run concurrently, bounded by
    # DISCOVERY_MAX_WORKERS. Workers return a value (the per-type
    # _bootstrap_collect*'s dict) and touch only their own device's
    # bootstrap session; nothing here is mutated by more than one worker,
    # and no candidate/topology state is touched until every result has
    # been collected below.
    collect_kwargs = {"include_l3": True} if include_l3 else {}
    global _ACTIVE_HOSTKEYS, _ACTIVE_RUN
    if hostkeys is not None and all_targets:
        try:
            hostkeys.prepare(all_targets)
        except hostkey_flow.HostKeyError as exc:
            raise DiscoveryError(str(exc)) from None
    _ACTIVE_HOSTKEYS = hostkeys
    run = _RunControl()
    _ACTIVE_RUN = run
    futures: dict[str, concurrent.futures.Future] = {}

    def guarded(collector, device_id, device_cfg):
        # A fatal condition in any worker cancels the whole run at once --
        # not only after every future has finished -- so other workers
        # cannot start any further credential send, and queued workers do
        # not start at all.
        run.admit()
        try:
            return collector(device_id, device_cfg, **collect_kwargs)
        except DeviceCollectionError:
            raise
        except BaseException:
            run.cancel()
            raise

    try:
        if all_targets:
            worker_count = min(len(all_targets), DISCOVERY_MAX_WORKERS)
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
            try:
                for device_id, device_cfg in all_targets.items():
                    if device_id in iosxr_targets:
                        collector = _bootstrap_collect
                    elif device_id in iosxe_targets:
                        collector = _bootstrap_collect_iosxe
                    else:
                        collector = _bootstrap_collect_ios
                    futures[device_id] = executor.submit(guarded, collector, device_id, device_cfg)
                executor.shutdown(wait=True)
            except BaseException:
                # e.g. the operator's KeyboardInterrupt: stop new sends, drop
                # queued work, then let already-running steps finish.
                run.cancel()
                executor.shutdown(wait=True, cancel_futures=True)
                raise
            # Every submitted future has finished (successfully or not) --
            # so every bootstrap session below is either fully collected or
            # has already failed, never still in flight.
    finally:
        _ACTIVE_HOSTKEYS = None
        _ACTIVE_RUN = None
        for device_id in all_targets:
            terminal.close_bootstrap_terminal(device_id)

    # Deterministic aggregation in access-info order, never in whatever order
    # the thread pool happened to finish. Every device's outcome is recorded;
    # only an explicitly classified DeviceCollectionError is continuable.
    # Any other DiscoveryError (host key, credential-target ambiguity),
    # HostKeyChanged, an unexpected worker exception and KeyboardInterrupt
    # propagate and fail the run closed -- there is deliberately no catch-all.
    cancelled_devices: list[str] = []
    for device_id, outcome in plan.items():
        if device_id not in futures:
            continue
        management_address = all_targets[device_id].get("address")
        try:
            info = futures[device_id].result()
        except DeviceCollectionError as exc:
            _record_collection(outcome, exc.collected, exc, include_l3, management_address)
        except _RunCancelled:
            cancelled_devices.append(device_id)  # a consequence of another worker's fatal error
        except (DiscoveryError, hostkey_flow.HostKeyChanged):
            raise
        except Exception as exc:
            # A worker crash (not a DiscoveryError) is a bug, not an
            # anticipated device/login/command failure -- it fails Discovery
            # closed, through the same bounded error type, without leaking a
            # raw traceback to the caller.
            raise DiscoveryError(f"Device '{device_id}': unexpected collection failure: {exc}") from exc
        else:
            _record_collection(outcome, info, None, include_l3, management_address)

    if cancelled_devices:
        raise DiscoveryError("Discovery was cancelled before every device finished; the candidate was not changed.")

    outcomes = list(plan.values())
    identity_map = {o.device_id: o.hostname for o in outcomes if o.connection == "connected"}

    resolved: list[tuple[NeighborObservation, str]] = []
    unresolved: list[UnresolvedNeighbor] = []
    for outcome in outcomes:
        for obs in outcome.observations:
            remote_id = resolve_remote_identity(obs.remote_device_id_raw, identity_map)
            if remote_id is not None and remote_id in plan:
                resolved.append((obs, remote_id))
            else:
                unresolved.append(
                    UnresolvedNeighbor(
                        remote_device_id_raw=obs.remote_device_id_raw,
                        local_device_id=obs.local_device_id,
                        local_interface=obs.local_interface,
                        remote_port_id=obs.remote_port_id,
                        capabilities=obs.capabilities,
                    )
                )

    links, conflicts = reconcile_links(resolved)
    _fail_on_link_ambiguity(links, conflicts, unresolved)

    devices: dict[str, dict] = {}
    for outcome in outcomes:
        fields: dict = {"type": outcome.device_type} if outcome.device_type else {}
        if outcome.l3_state == "ok":
            fields["interfaces"] = outcome.l3_interfaces
        devices[outcome.device_id] = fields

    l3_ok = [o for o in outcomes if o.l3_state == "ok"]
    return DiscoveryResult(
        access_info_name=access_info_name,
        default_topology_name=access_info_name,
        iosxr_target_count=len(iosxr_targets),
        connected_count=sum(1 for o in outcomes if o.connection == "connected"),
        observation_count=len(_observations_from(outcomes, "lldp")),
        iosxe_target_count=len(iosxe_targets),
        cdp_observation_count=len(_observations_from(outcomes, "cdp")),
        ios_target_count=len(ios_targets),
        l3_enriched_device_count=len(l3_ok),
        l3_interface_count=sum(
            1 for o in l3_ok for value in (o.l3_interfaces or {}).values() if value is not None
        ),
        devices=devices,
        managed_links=links,
        unresolved=unresolved,
        conflicts=conflicts,
        identity_map=identity_map,
        include_l3=include_l3,
        outcomes=outcomes,
        warnings=[f"{o.device_id}: {message}" for o in outcomes for message in o.warnings],
    )


def _observations_from(outcomes: list[DeviceOutcome], source: str):
    """Every usable observation of one protocol ("lldp"/"cdp")."""
    return [obs for o in outcomes for obs in o.observations if obs.source == source]


# The one measured "CDP not supported" case (live read-only capture, IOS XR
# 26.2.2 LNT / NCS540L): the device's `show version` banner identifies an IOS XR
# LNT image and `show cdp neighbors` answers with nothing but this error
# (plus the caret marker line). Deliberately narrow: `% Invalid input` is NOT
# treated as "unsupported" for any other OS, image or command.
_IOSXR_LNT_VERSION_RE = re.compile(r"^Cisco IOS XR Software, Version \S+ LNT\s*$", re.MULTILINE)
_INVALID_INPUT_LINE = "% Invalid input detected at '^' marker."
_CARET_ONLY_RE = re.compile(r"^\s*\^\s*$")


def _iosxr_lnt_cdp_unsupported(show_version: str, cdp_text: str) -> bool:
    """True only for a device whose own `show version` is an IOS XR LNT
    image AND whose `show cdp neighbors` output is exactly the invalid-input
    error (ignoring blank lines, the CLI timestamp, the prompt/command echo
    and the caret marker line). Caller restricts this to `type: iosxr`."""
    if not _IOSXR_LNT_VERSION_RE.search(show_version or ""):
        return False
    meaningful = [
        line
        for line in (raw.strip() for raw in cdp_text.splitlines())
        if line
        and not _CLI_TIMESTAMP_RE.match(line)
        and not _IOSXR_PROMPT_OR_ECHO_RE.match(line)
        and not _CARET_ONLY_RE.match(line)
    ]
    return meaningful == [_INVALID_INPUT_LINE]


def _cdp_table_has_body(raw_text: str) -> bool:
    """True if the recognized CDP table is followed by at least one
    non-blank, non-prompt line before the table ends -- used to tell a
    valid empty table from a broken one whose rows all failed to parse."""
    in_table = False
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not in_table:
            if line.startswith("Device ID") and _CDP_HEADER_MARKER in line and raw_line.find("Port ID") != -1:
                in_table = True
            continue
        if not line:
            return False
        if _IOSXR_PROMPT_RE.search(line):
            return False
        return True
    return False


def _cdp_table_recognized(raw_text: str) -> bool:
    """True if `show cdp neighbors` output carries the CDP table header
    (the same header parse_cdp_neighbors() requires), i.e. the command ran
    and produced a CDP table -- possibly with zero rows."""
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if line.startswith("Device ID") and _CDP_HEADER_MARKER in line and raw_line.find("Port ID") != -1:
            return True
    return False


def _fail_on_link_ambiguity(
    links: list[ManagedLink], conflicts: list[LinkConflict], unresolved: list[UnresolvedNeighbor]
) -> None:
    """Fail Discovery (DiscoveryError) instead of producing a candidate when
    the reconciled evidence is ambiguous: a reconciliation conflict, a
    managed physical interface claimed by more than one link, or a managed
    link endpoint that another observation says faces an unmanaged
    neighbor. Messages name only logical devices and interface names."""
    problems: list[str] = []
    for conflict in conflicts:
        a_dev, a_intf = conflict.endpoint_a
        b_dev, b_intf = conflict.endpoint_b
        if conflict.endpoint_a == conflict.endpoint_b:
            problems.append(f"{a_dev} {a_intf}: conflicting neighbor observations")
        else:
            problems.append(f"{a_dev} {a_intf} <-> {b_dev} {b_intf}: inconsistent reciprocal observation")
    owners: dict[tuple[str, str], list[ManagedLink]] = {}
    raw_names: dict[tuple[str, str], str] = {}  # canonical identity -> a raw spelling for messages
    for link in links:
        for device, interface in ((link.a_device, link.a_interface), (link.b_device, link.b_interface)):
            identity = (device, _canonicalize_interface_name(interface))
            owners.setdefault(identity, []).append(link)
            raw_names.setdefault(identity, interface)
    for endpoint in sorted(owners):
        if len(owners[endpoint]) > 1:
            problems.append(f"{endpoint[0]} {raw_names[endpoint]}: interface appears in more than one link")
    for neighbor in unresolved:
        endpoint = (neighbor.local_device_id, _canonicalize_interface_name(neighbor.local_interface))
        if endpoint in owners:
            problems.append(
                f"{endpoint[0]} {neighbor.local_interface}: observed with both a managed and an unmanaged neighbor"
            )
    if problems:
        unique = list(dict.fromkeys(sorted(problems)))
        raise DiscoveryError("Neighbor reconciliation conflict: " + "; ".join(unique) + ".")


def resolve_default_topology_name(lab_root=None) -> str:
    """The default Discovery target topology name: the committed selected
    access-info definition's own name -- same-basename is
    only this default, never a runtime requirement. Exposed
    separately from discover_topology() so a caller (the CLI) can check
    candidate-switch safety *before* running the real, expensive Discovery
    flow, not just after."""
    lab_root = lab_root or lab.find_lab_root()
    settings = lab.read_settings(lab_root)
    access_info_name = lab.get_active_access_info_name(settings)
    if not access_info_name:
        raise DiscoveryError("No access-info is selected in running-config.")
    return access_info_name


def _topology_link_key(link: dict) -> tuple:
    endpoint_a = (link.get("a"), link.get("a_interface") or "")
    endpoint_b = (link.get("b"), link.get("b_interface") or "")
    return tuple(sorted((endpoint_a, endpoint_b)))


def _canonical_endpoint(device: str, interface: str | None) -> tuple[str, str]:
    return (device, _canonicalize_interface_name(interface) if interface else "")


def _canonical_link_key(link: dict) -> tuple:
    """Identity of a topology link: its unordered pair of (device, canonical
    interface) endpoints, so spelling variants and endpoint order never
    produce a second copy of the same physical link."""
    return tuple(
        sorted(
            (
                _canonical_endpoint(link.get("a"), link.get("a_interface")),
                _canonical_endpoint(link.get("b"), link.get("b_interface")),
            )
        )
    )


def _format_endpoint(device: str, interface: str | None) -> str:
    return f"{device} {interface}" if interface else device


def _format_topology_link(link: dict) -> str:
    return (
        f"{_format_endpoint(link.get('a'), link.get('a_interface'))} <-> "
        f"{_format_endpoint(link.get('b'), link.get('b_interface'))}"
    )


def build_discovery_candidate(result: DiscoveryResult, base: dict) -> dict:
    """Merge this run's DiscoveryResult into `base` -- additive and
    non-destructive. Discovery never deletes anything.

    `base` is the existing topology data (or a new empty topology). Every
    device, link, interface and unknown field it holds is kept. A new device
    is added; a device's missing `type` is filled in; a new link is added
    (an already-known link, in any endpoint order or interface spelling, is
    never duplicated and keeps its stored attributes); a newly observed L3
    interface or L3 field value is added/updated in place. Nothing observed
    as absent, unassigned, `None` or unreachable removes or blanks anything.

    A contradiction is never resolved by overwriting: a device `type` that
    differs from the stored one, or a newly observed link that claims an
    interface already used by a different stored link, raises
    DiscoveryError (the caller then leaves the candidate untouched). The
    only way to rebuild a topology from scratch is for the user to delete it
    explicitly (`no topology <name>`) and run Discovery again.

    Output is deterministic: existing devices and links keep their order,
    newly added devices are sorted by ID and newly added links are in
    canonical (device, interface) endpoint order, sorted. Pure, no I/O, never
    mutates `base`."""
    candidate = {key: copy.deepcopy(value) for key, value in base.items() if key not in ("devices", "links")}
    devices: dict[str, dict] = {
        device_id: (copy.deepcopy(value) if isinstance(value, dict) else {})
        for device_id, value in (base.get("devices") or {}).items()
    }
    problems: list[str] = []

    for device_id in sorted(result.devices):
        fields = result.devices[device_id]
        device = devices.setdefault(device_id, {})
        new_type = fields.get("type")
        old_type = device.get("type")
        if new_type:
            if not old_type:
                device["type"] = new_type
            else:
                try:
                    same = lab.normalize_device_type(str(old_type)) == new_type
                except lab.LabConfigError:
                    same = False
                if not same:
                    problems.append(
                        f"Device '{device_id}': access-info type '{new_type}' conflicts with the topology's "
                        f"existing type '{old_type}'"
                    )
        interfaces = fields.get("interfaces")
        if interfaces is not None:
            merged = device.get("interfaces")
            merged = dict(merged) if isinstance(merged, dict) else {}
            by_canonical = {_canonicalize_interface_name(str(name)): name for name in merged}
            for name, value in sorted(interfaces.items()):
                if value is None:
                    continue  # unassigned/management/unobserved: never blanks stored data
                key = by_canonical.get(_canonicalize_interface_name(name), name)
                stored = merged.get(key)
                merged[key] = {**(stored if isinstance(stored, dict) else {}), **value}
            device["interfaces"] = merged

    links: list[dict] = copy.deepcopy(base.get("links") or [])
    known = {_canonical_link_key(link) for link in links}
    owners: dict[tuple[str, str], dict] = {}
    for link in links:
        for device, interface in ((link.get("a"), link.get("a_interface")), (link.get("b"), link.get("b_interface"))):
            endpoint = _canonical_endpoint(device, interface)
            if endpoint[1]:
                owners.setdefault(endpoint, link)
    new_links: list[dict] = []
    for managed_link in result.managed_links:
        first, second = sorted(
            (
                (managed_link.a_device, managed_link.a_interface),
                (managed_link.b_device, managed_link.b_interface),
            )
        )
        link = {"a": first[0], "a_interface": first[1], "b": second[0], "b_interface": second[1]}
        key = _canonical_link_key(link)
        if key in known:
            continue
        for endpoint in key:
            owner = owners.get(endpoint)
            if owner is not None:
                problems.append(
                    f"Discovered link {_format_topology_link(link)} conflicts with existing link "
                    f"{_format_topology_link(owner)}"
                )
                break
        known.add(key)
        new_links.append(link)
    if problems:
        raise DiscoveryError(
            "Discovery contradicts the existing topology: "
            + "; ".join(dict.fromkeys(sorted(problems)))
            + ". Nothing was overwritten. Edit the topology, or delete it explicitly with "
            "'no topology <name>' and run Discovery again."
        )
    new_links.sort(key=_topology_link_key)
    candidate["devices"] = devices
    candidate["links"] = links + new_links
    return candidate


@dataclass(frozen=True)
class TopologyChanges:
    """What a merge actually changed, computed from the topology as it was
    before Discovery and the validated final candidate. Nothing is ever
    removed, so there is no "removed" count."""

    devices_added: int = 0
    devices_updated: int = 0
    devices_retained: int = 0
    links_added: int = 0
    links_retained: int = 0
    interfaces_added: int = 0
    interfaces_updated: int = 0
    interfaces_retained: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.devices_added or self.devices_updated or self.links_added)


def topology_changes(before: dict, after: dict) -> TopologyChanges:
    before_devices = {k: (v if isinstance(v, dict) else {}) for k, v in (before.get("devices") or {}).items()}
    after_devices = {k: (v if isinstance(v, dict) else {}) for k, v in (after.get("devices") or {}).items()}
    added = updated = retained = 0
    if_added = if_updated = if_retained = 0
    for device_id, device in after_devices.items():
        old = before_devices.get(device_id)
        old_interfaces = ((old or {}).get("interfaces")) or {}
        for name, value in (device.get("interfaces") or {}).items():
            if name not in old_interfaces:
                if_added += 1
            elif old_interfaces[name] != value:
                if_updated += 1
        if old is None:
            added += 1
        elif old != device:
            updated += 1
        else:
            retained += 1
    for device_id, old in before_devices.items():
        new_interfaces = ((after_devices.get(device_id) or {}).get("interfaces")) or {}
        for name, value in (old.get("interfaces") or {}).items():
            if name in new_interfaces and new_interfaces[name] == value:
                if_retained += 1
    before_links = {_canonical_link_key(link) for link in (before.get("links") or [])}
    after_links = {_canonical_link_key(link) for link in (after.get("links") or [])}
    return TopologyChanges(
        devices_added=added,
        devices_updated=updated,
        devices_retained=retained,
        links_added=len(after_links - before_links),
        links_retained=len(before_links & after_links),
        interfaces_added=if_added,
        interfaces_updated=if_updated,
        interfaces_retained=if_retained,
    )
