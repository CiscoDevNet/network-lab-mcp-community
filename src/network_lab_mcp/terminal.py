# SPDX-License-Identifier: Apache-2.0
"""tmux-based terminal session management for Network Lab MCP.

All terminal state lives in a dedicated tmux server (a separate named socket),
which is the single source of truth for session lifetime. This module does not
keep any session registry of its own.

Two structurally separate session namespaces are used:

    network-lab-device-<device-id>       production topology device sessions
    network-lab-validation-<validation>  local validation-only sessions

The namespaces are distinguished by their fixed prefix, never by searching for
the substring "validation" inside a name. This keeps a legitimate topology
device named e.g. "validation-router" (session
"network-lab-device-validation-router") clearly separate from a validation
session such as "network-lab-validation-terminal-io".

Production and local validation callers share the same internal session
primitives (`_ensure_managed_session`, `_send_literal_text`, `_send_special_keys`,
`_capture_pane`, `_list_sessions`, `_close_session`); only the derived session
name and the launched command differ.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

TMUX_SOCKET_NAME = "network-lab-mcp"

PRODUCTION_PREFIX = "network-lab-device-"
VALIDATION_PREFIX = "network-lab-validation-"
DISCOVERY_PREFIX = "network-lab-discovery-"
BOOTSTRAP_SESSION = "network-lab-bootstrap-initializer"

HISTORY_LIMIT = 20000
DEFAULT_READ_LINES = 100

# Repo checkout root, computed the same way lab.find_lab_root() computes its
# own repo-root-relative-to-this-file's-package-directory path -- kept
# independent (no import of network_lab_mcp.lab) so this module never
# depends on lab/ existing or being valid.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOGS_ROOT = _REPO_ROOT / "logs" / "terminal"

_SESSION_START_FORMAT = "%Y%m%dT%H%M%S"
# Group 1: the session-start timestamp prefix. Group 2 (optional): a
# same-second collision suffix (see _unique_log_path()).
_LOG_FILENAME_RE = re.compile(r"^(\d{8}T\d{6})(?:_(\d+))?\.log$")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_SPECIAL_KEYS = {
    "Enter",
    "Escape",
    "Tab",
    "Space",
    "BSpace",
    "Up",
    "Down",
    "Left",
    "Right",
    "Home",
    "End",
    "PageUp",
    "PageDown",
}
_SPECIAL_KEY_RE = re.compile(r"^C-[A-Za-z]$|^M-[A-Za-z]$")


class TerminalError(Exception):
    """A clear, user-facing terminal/session management error."""


# --------------------------------------------------------------------------
# Shared safe SSH password-prompt attribution
#
# OpenSSH's own client-side interactive password prompt is always exactly
# "<user>@<host>'s password: " for whichever hop is currently
# authenticating -- stable, well-documented client-side text (not the
# remote device's own banner/CLI). Matching at this transport level (never
# a device-CLI-specific prompt) is what makes this safely reusable by both
# Discovery's bootstrap login (discovery.py, IOS XR specific elsewhere)
# and normal managed terminal_open() private authentication below (device-
# type agnostic) -- one shared attribution rule, not two independent
# password-prompt parsers.
# --------------------------------------------------------------------------

PASSWORD_PROMPT_RE = re.compile(r"[Pp]assword:\s*$", re.MULTILINE)
SSH_HOP_PASSWORD_PROMPT_RE = re.compile(r"(?P<hop_user>[^\s@]+)@(?P<hop_host>[^\s']+)'s password:\s*$")

# Shared classic-IOS-style login text: originally introduced in
# discovery.py (for Discovery's own IOS XE/IOS Telnet-or-SSH login) and
# moved here once managed terminal_open()'s Telnet authentication (below)
# needed the exact same two patterns -- one definition, not two
# independently-maintained copies. `discovery.py`'s own `_USERNAME_PROMPT_RE`/
# `_IOS_STYLE_PROMPT_RE` now alias these. `IOS_STYLE_PROMPT_RE` matches a
# classic-IOS-style exec prompt in full (just "hostname#"/"hostname>", unlike
# IOS XR's "RP/.../CPU0:hostname#" shape) -- this is the one place a vendor
# prompt shape is recognized outside discovery.py, and only for the narrow,
# already-proven classic-IOS-style Telnet targets this project supports (see
# the Telnet authentication section below), never a generic CLI parser.
USERNAME_PROMPT_RE = re.compile(r"[Uu]sername:\s*$", re.MULTILINE)
IOS_STYLE_PROMPT_RE = re.compile(r"^(?P<hostname>[\w.-]+)[#>]\s*$", re.MULTILINE)


@dataclass(frozen=True)
class PasswordPromptOutcome:
    """Result of attributing one observed password-prompt line to a
    specific SSH hop. `matched_target` is True only when the prompt can be
    confidently attributed to the target device's own address -- never a
    guess. `reason` is a short, fixed, never-secret description of why a
    prompt was rejected (`matched_target=False`); it never includes any
    access-info content."""

    matched_target: bool
    password: str
    reason: str


def resolve_target_password_prompt(device_config: dict, prompt_line: str) -> PasswordPromptOutcome:
    """Shared safe target-vs-jump-host password-prompt attribution.

    Direct SSH (no `jump_host_config`) is unambiguous: the one password
    prompt that can appear is always the target device's own.

    ProxyJump can show *two* separate password prompts in sequence (one
    per hop), and sending the wrong one to the wrong hop must never
    happen. This reads the prompting hop's own address out of OpenSSH's
    prompt text and only answers when it confidently matches the target
    device's own address; a prompt that matches the jump host's address,
    or that cannot be confidently attributed to either hop, fails closed
    (`matched_target=False`) instead of guessing -- see "Bounded ProxyJump
    limitation" in docs/architecture.md. Callers (discovery.py, and
    _authenticate_managed_session() below) each translate a rejected
    outcome into their own caller-appropriate exception with their own
    wording; this function never raises."""
    jump_host_config = device_config.get("jump_host_config")
    if not jump_host_config:
        return PasswordPromptOutcome(True, device_config.get("password") or "", "")

    hop_match = SSH_HOP_PASSWORD_PROMPT_RE.search(prompt_line)
    hop_host = hop_match.group("hop_host") if hop_match else None
    if hop_host is not None and hop_host == str(device_config.get("address")):
        return PasswordPromptOutcome(True, device_config.get("password") or "", "")
    if hop_host is not None and hop_host == str(jump_host_config.get("address")):
        return PasswordPromptOutcome(False, "", "jump-host password prompt")
    return PasswordPromptOutcome(False, "", "ambiguous password prompt")


# --------------------------------------------------------------------------
# Session name derivation
# --------------------------------------------------------------------------


def _validate_identifier(identifier: str, what: str) -> None:
    if not isinstance(identifier, str) or not _NAME_RE.match(identifier):
        raise TerminalError(
            f"{what} '{identifier}' is not a valid identifier. Identifiers must start with "
            "a letter or digit and contain only letters, digits, '-', and '_'."
        )


def derive_production_session_name(device_name: str) -> str:
    """Derive the production tmux session name for an active-topology device."""
    _validate_identifier(device_name, "Device name")
    return f"{PRODUCTION_PREFIX}{device_name}"


def derive_validation_session_name(validation_id: str) -> str:
    """Derive the validation-only tmux session name for a local validation identifier."""
    _validate_identifier(validation_id, "Validation identifier")
    return f"{VALIDATION_PREFIX}{validation_id}"


def derive_discovery_session_name(device_name: str) -> str:
    """Derive the private, temporary Discovery bootstrap tmux session name.

    Structurally separate from both the production and validation
    namespaces (see module docstring) -- never exposed through any public
    MCP tool, and never confused with a topology device's production
    session even when discovering a device outside the active topology."""
    _validate_identifier(device_name, "Device name")
    return f"{DISCOVERY_PREFIX}{device_name}"


def is_production_session(session_name: str) -> bool:
    return session_name.startswith(PRODUCTION_PREFIX)


def is_validation_session(session_name: str) -> bool:
    return session_name.startswith(VALIDATION_PREFIX)


def is_discovery_session(session_name: str) -> bool:
    return session_name.startswith(DISCOVERY_PREFIX)


def production_device_name(session_name: str) -> str:
    """Recover the device name encoded in a production session name."""
    if not is_production_session(session_name):
        raise TerminalError(f"'{session_name}' is not a production session.")
    return session_name[len(PRODUCTION_PREFIX) :]


# --------------------------------------------------------------------------
# Per-session serialization
#
# Different devices execute concurrently; the same device's operations must
# not race each other (tmux send/capture/kill against one pane, and the
# check-then-create in _ensure_managed_session()). One threading.Lock per
# underlying tmux session name (production/validation/Discovery names are
# already namespace-disjoint, so this is naturally one lock per managed
# device *and* transport role) serializes exactly the operations that share
# that one session, while leaving different devices' locks fully
# independent -- never a single global lock around all terminal work (that
# would serialize every device, defeating the point).
#
# Locks are never removed: the set of distinct session names touched over
# one process's lifetime is bounded by the active topology / Discovery
# targets, not by unbounded external input (every session name reaching
# _session_lock() already passed derive_*_session_name()'s
# _validate_identifier()), so a simple process-lifetime registry is
# sufficient. `_registry_guard` protects
# only the dict's own get-or-create step, never the caller's actual
# operation, so acquiring a per-session lock is never itself a point of
# cross-device contention.
# --------------------------------------------------------------------------

_session_locks: dict[str, threading.Lock] = {}
_registry_guard = threading.Lock()


def _session_lock(session_name: str) -> threading.Lock:
    with _registry_guard:
        lock = _session_locks.get(session_name)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session_name] = lock
        return lock


# --------------------------------------------------------------------------
# Low-level tmux invocation
# --------------------------------------------------------------------------


def _tmux_base() -> list[str]:
    if shutil.which("tmux") is None:
        raise TerminalError("The 'tmux' binary is not available on this system.")
    return ["tmux", "-L", TMUX_SOCKET_NAME]


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        _tmux_base() + args,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise TerminalError(f"tmux command failed: {' '.join(args)}: {result.stderr.strip()}")
    return result


# --------------------------------------------------------------------------
# Internal session-management primitives
#
# These are shared by production terminal_open()/... and the local validation
# helpers below; only the session-name namespace and the launched command
# differ between the two callers.
# --------------------------------------------------------------------------


def _server_has_any_session() -> bool:
    result = _run(["list-sessions", "-F", "#{session_name}"], check=False)
    return result.returncode == 0


def _session_exists(session_name: str) -> bool:
    result = _run(["has-session", "-t", session_name], check=False)
    return result.returncode == 0


def _create_session(session_name: str, command: list[str]) -> None:
    _run(["new-session", "-d", "-s", session_name, "-x", "220", "-y", "50", *command])


def _create_logged_session(session_name: str, device_name: str, command: list[str]) -> None:
    """Like _create_session(), but attaches persistent pipe-pane logging
    (see _start_session_logging()) *before* `command` (the ssh/telnet
    transport) starts running in the pane, instead of after -- so the
    earliest output (login banner, host-key message, the very first
    password prompt) is never lost to the log. The pane starts with a
    neutral shell (no command), which is what gives logging a moment to
    attach before `command` is typed into it and executed."""
    _run(["new-session", "-d", "-s", session_name, "-x", "220", "-y", "50"])
    _start_session_logging(session_name, device_name)
    _send_literal_text(session_name, shlex.join(command))
    _send_enter(session_name)


def _kill_session_if_exists(session_name: str) -> None:
    if _session_exists(session_name):
        _run(["kill-session", "-t", session_name], check=False)


def _ensure_tmux_environment() -> bool:
    """Ensure the dedicated tmux server exists and the history-limit is configured.

    Returns True if a temporary bootstrap session had to be created to start
    the dedicated tmux server. The bootstrap session is internal only and is
    never exposed through any public tool.

    The "does any session exist yet" check below is the one place a
    different-device race can still reach across two distinct per-device
    locks: two different devices' first-ever opens can both see
    an empty server and both try to create the *same* shared bootstrap
    session name. Rather than adding a second, separate global lock just
    for this rare one-time window, tolerate the race directly -- a losing
    caller's own create fails, but by then the server unquestionably has a
    session (the winner's), so it simply proceeds.
    """
    bootstrap_created = False
    if not _server_has_any_session():
        try:
            _create_session(BOOTSTRAP_SESSION, ["cat"])
            bootstrap_created = True
        except TerminalError:
            if not _server_has_any_session():
                raise
    # Idempotent: safe to run on every call, including when the server and its
    # managed panes already existed before this process started.
    _run(["set-option", "-g", "history-limit", str(HISTORY_LIMIT)])
    # Keep a pane around after its command exits (e.g. ssh/telnet disconnects)
    # so terminal_read() can still observe the final output instead of the
    # session silently disappearing.
    _run(["set-option", "-g", "remain-on-exit", "on"])
    return bootstrap_created


def _ensure_managed_session(session_name: str, command: list[str], *, log_device_name: str | None = None) -> bool:
    """Ensure a managed session exists, creating it with `command` if needed.

    Returns True if an existing session was reused, False if a new one was
    created. Existing sessions are never destroyed to reapply configuration.

    `log_device_name`, when given, creates the session via
    _create_logged_session() instead of _create_session() -- logging
    attached before `command` starts, rather than after. Reuse never
    touches logging either way (a reused session is already logging from
    when it was first created)."""
    bootstrap_created = _ensure_tmux_environment()
    reused = _session_exists(session_name)
    if not reused:
        if log_device_name is not None:
            _create_logged_session(session_name, log_device_name, command)
        else:
            _create_session(session_name, command)
    if bootstrap_created and session_name != BOOTSTRAP_SESSION:
        _kill_session_if_exists(BOOTSTRAP_SESSION)
    return reused


def _send_text_via_stdin(session_name: str, buffer_name: str, text: str) -> None:
    """Deliver `text` into `session_name`'s pane without ever placing it in
    a subprocess's own argv (a process listing on the host, or a
    `f"...{' '.join(args)}..."`-style error message from a failed command,
    could otherwise reveal it): `text` is piped to `tmux load-buffer -`
    over stdin into a `buffer_name`-named buffer, then `tmux paste-buffer
    -r -d` types that buffer's content into the pane and deletes the
    buffer immediately afterward. `-r` ("no replacement") is required, not
    optional -- paste-buffer's own default silently replaces every LF in
    the buffer with a CR separator, which would corrupt any literal text
    or secret containing an embedded newline; `-r` preserves every byte
    exactly, matching `send-keys -l --`'s own literal, unmodified-byte
    behavior (confirmed empirically against a real tmux session; see
    tests/test_terminal_send_literal_text_injection.py). `buffer_name`
    must be unique across concurrently-active sessions (the caller derives
    it from `session_name`), so concurrent text delivery on different
    sessions never shares a buffer. Neither this function's own tmux
    command lines nor its error messages ever include `text` itself."""
    if not _session_exists(session_name):
        raise TerminalError(f"Session '{session_name}' does not exist.")
    load_result = subprocess.run(
        _tmux_base() + ["load-buffer", "-b", buffer_name, "-"],
        input=text,
        capture_output=True,
        text=True,
    )
    if load_result.returncode != 0:
        raise TerminalError(f"tmux command failed: load-buffer: {load_result.stderr.strip()}")
    try:
        _run(["paste-buffer", "-r", "-d", "-b", buffer_name, "-t", session_name])
    finally:
        # Defensive: `paste-buffer -d` already deletes the buffer itself on
        # success; this only guards against a buffer surviving if
        # paste-buffer raised (e.g. the session vanished mid-operation).
        _run(["delete-buffer", "-b", buffer_name], check=False)


def _send_literal_text(session_name: str, text: str) -> None:
    """Ordinary operator/terminal_send() text -- see _send_text_via_stdin()
    for why this never uses `send-keys -l --` (which would place `text` in
    that tmux subprocess's own argv, and potentially in a raised
    TerminalError's message on failure). `text` may itself be a username,
    password, or other private configuration value being typed into a
    device, so this path gets the same argv/error-message safety as
    dedicated credential input, without changing terminal_send()'s public
    byte-level delivery contract."""
    _send_text_via_stdin(session_name, f"text-{session_name}", text)


def _send_secret_text(session_name: str, secret: str) -> None:
    """Dedicated credential input (a password, or a Telnet username): see
    _send_text_via_stdin() for the shared transport. Kept as its own named
    wrapper (rather than calling _send_text_via_stdin() directly at each
    call site) so every credential call site reads unambiguously as
    credential-handling code."""
    _send_text_via_stdin(session_name, f"secret-{session_name}", secret)


def _send_special_keys(session_name: str, keys: list[str]) -> None:
    if not _session_exists(session_name):
        raise TerminalError(f"Session '{session_name}' does not exist.")
    for key in keys:
        if key not in _SPECIAL_KEYS and not _SPECIAL_KEY_RE.match(key):
            raise TerminalError(
                f"Unsupported special key '{key}'. Supported: {sorted(_SPECIAL_KEYS)} "
                "or 'C-<letter>' / 'M-<letter>' combinations."
            )
    if keys:
        _run(["send-keys", "-t", session_name, "--", *keys])


def _send_enter(session_name: str) -> None:
    if not _session_exists(session_name):
        raise TerminalError(f"Session '{session_name}' does not exist.")
    _run(["send-keys", "-t", session_name, "--", "Enter"])


def _capture_pane(session_name: str, lines: int) -> str:
    if not _session_exists(session_name):
        raise TerminalError(f"Session '{session_name}' does not exist.")
    # Capture the whole available buffer (history + current screen) and slice
    # in Python. tmux's negative -S offsets are relative to history, which is
    # empty until the visible pane has scrolled at least once, so asking tmux
    # itself for "the last N lines" via -S/-E is unreliable while a pane is
    # still filling its first screen. capture-pane is cheap even over the
    # full history, so this stays simple and correct in both cases.
    result = _run(["capture-pane", "-t", session_name, "-p", "-S", "-"])
    text = result.stdout.rstrip("\n")
    if not text:
        return ""
    return "\n".join(text.split("\n")[-lines:])


def _pane_state(session_name: str) -> str:
    result = _run(["list-panes", "-t", session_name, "-F", "#{pane_dead}"], check=False)
    if result.returncode != 0:
        return "unknown"
    lines = result.stdout.strip().splitlines()
    return "exited" if lines and lines[0] == "1" else "running"


def _list_sessions(prefix: str) -> list[str]:
    if not _server_has_any_session():
        return []
    result = _run(["list-sessions", "-F", "#{session_name}"], check=False)
    if result.returncode != 0:
        return []
    return [name for name in result.stdout.splitlines() if name.startswith(prefix)]


def _close_session(session_name: str) -> bool:
    """Close a managed session. Returns True if it existed and was closed."""
    if not _session_exists(session_name):
        return False
    _run(["kill-session", "-t", session_name])
    return True


def _wait_for_pattern(
    session_name: str,
    pattern: "re.Pattern[str]",
    timeout: float,
    poll_interval: float = 0.3,
    baseline_text: str | None = None,
) -> str:
    """Poll pane content until `pattern` matches the tail of the captured
    text, or raise TerminalError on timeout. Returns the full captured pane
    text at the moment of the match. Used by the private Discovery
    bootstrap path (see discovery.py) and by _authenticate_managed_session()
    below -- terminal_read()/terminal_send() themselves remain a
    simple, unattended capture/send with no waiting loop.

    `baseline_text`, when given, is the pane content captured *before* the
    command that's now being waited on was sent. A match is only accepted
    once the captured text has actually changed from that baseline --
    otherwise a prompt already sitting in the pane from a *previous*
    command could satisfy `pattern` immediately, before the newly sent
    command has produced any output at all (a stale-prompt race). Omit it
    (the default) only when there is genuinely nothing prior to be stale
    relative to, e.g. a session that was just freshly created."""
    deadline = time.monotonic() + timeout
    last_text = baseline_text or ""
    while time.monotonic() < deadline:
        last_text = _capture_pane(session_name, HISTORY_LIMIT)
        if baseline_text is None or last_text != baseline_text:
            tail = "\n".join(last_text.splitlines()[-5:])
            if pattern.search(tail):
                return last_text
        time.sleep(poll_interval)
    raise TerminalError(
        f"Timed out after {timeout:.0f}s waiting for expected output on session '{session_name}'."
    )


# --------------------------------------------------------------------------
# Persistent terminal transcript logging (logs/terminal/<device-id>/*.log)
#
# tmux's own pipe-pane mechanism is the single source of truth for what
# gets logged -- this module never re-renders or duplicates pane content
# into a second log path. Logging is started once, right after a session
# is newly created (never on reuse, since the pipe is already attached to
# that pane for its whole lifetime); tmux pane state remains the runtime
# session SSOT and terminal_read() is unchanged -- these logs are a
# separate, persistent, write-only historical record.
# --------------------------------------------------------------------------


def _device_log_dir(device_name: str) -> Path:
    _validate_identifier(device_name, "Device name")
    return LOGS_ROOT / device_name


def _session_start_timestamp() -> str:
    return datetime.now().strftime(_SESSION_START_FORMAT)


def _unique_log_path(log_dir: Path, timestamp: str) -> Path:
    """The normal filename is `<timestamp>.log`. If two sessions for the
    same device start within the same second, that name would already
    exist -- append the smallest `_2`, `_3`, ... suffix that doesn't,
    rather than silently overwriting/appending to the earlier session's
    log."""
    candidate = log_dir / f"{timestamp}.log"
    suffix = 2
    while candidate.exists():
        candidate = log_dir / f"{timestamp}_{suffix}.log"
        suffix += 1
    return candidate


def _start_session_logging(session_name: str, device_name: str) -> Path:
    log_dir = _device_log_dir(device_name)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = _unique_log_path(log_dir, _session_start_timestamp())
    # -o: only start piping if this pane isn't already being piped (a no-op
    # on an already-logging pane, so this is safe to call unconditionally
    # right after session creation).
    _run(["pipe-pane", "-o", "-t", session_name, f"cat >> {shlex.quote(str(log_path))}"])
    return log_path


def list_logged_device_ids() -> list[str]:
    """Device IDs that have a valid logging directory under
    logs/terminal/ -- directory-existence based, not log-content based,
    so a device whose logs were all deleted (but whose directory was
    intentionally left behind, see delete_all_device_logs()) is still
    listed, with zero logs. A symlink is never a
    valid device logging directory -- it is
    excluded here, the single SSOT this and every other consumer (`show
    logging`, `delete logging ...`, directory-deletion eligibility) reads
    valid device directories from."""
    if not LOGS_ROOT.is_dir():
        return []
    return sorted(
        p.name for p in LOGS_ROOT.iterdir() if p.is_dir() and not p.is_symlink() and _NAME_RE.match(p.name)
    )


def list_device_logs(device_name: str) -> list[tuple[datetime, str]]:
    """List (session_start, filename) pairs for one device, newest first.

    Returns an empty list for an unknown device or one with no logs yet --
    never raises for that; this is display-only, read-only data. Session
    Start is always derived from the `<timestamp>` prefix, even for a
    collision-suffixed filename (`<timestamp>_2.log`, ...) -- a same-
    second collision is broken by the numeric suffix (higher = later),
    not by filename string order."""
    if not _NAME_RE.match(device_name):
        return []
    log_dir = _device_log_dir(device_name)
    if not log_dir.is_dir():
        return []
    entries = []
    for path in log_dir.iterdir():
        if not path.is_file():
            continue
        match = _LOG_FILENAME_RE.match(path.name)
        if not match:
            continue
        try:
            started = datetime.strptime(match.group(1), _SESSION_START_FORMAT)
        except ValueError:
            continue
        collision_suffix = int(match.group(2)) if match.group(2) else 0
        entries.append((started, collision_suffix, path.name))
    entries.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return [(started, name) for started, _collision_suffix, name in entries]


def read_device_log(device_name: str, filename: str) -> str:
    """Read one device's log file by exact filename.

    Fails closed (TerminalError, never a silent guess) unless `device_name`
    is a valid identifier and `filename` is exactly one of that device's own
    already-listed log files -- this rejects path traversal (`../`, an
    absolute path, or any name not matching the fixed `<timestamp>.log`
    format) without needing to special-case those forms individually."""
    if not _NAME_RE.match(device_name):
        raise TerminalError(f"Device '{device_name}' has no terminal logs.")
    valid_filenames = {name for _, name in list_device_logs(device_name)}
    if filename not in valid_filenames:
        raise TerminalError(f"No log file '{filename}' for device '{device_name}'.")
    log_path = _device_log_dir(device_name) / filename
    return log_path.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# Terminal log deletion (EXEC `delete logging ...`)
#
# Eligibility is defined by reusing list_device_logs() -- the exact same
# enumeration `show logging` uses -- so there is no second log-discovery
# model; what can be deleted never drifts from what is displayed.
#
# Active-writer protection: both a normal production session
# (derive_production_session_name()) and a Discovery bootstrap session
# (derive_discovery_session_name()) attach persistent pipe-pane logging to
# the *same* logs/terminal/<device-id>/ directory, keyed by device name
# only -- see _create_logged_session()/open_device_terminal()/
# open_bootstrap_terminal(). Validation sessions never attach logging at
# all (open_validation_session() never passes log_device_name), so they
# are never a protection concern here. tmux's pipe-pane is attached once
# at session creation and never explicitly detached before the session is
# killed, so an existing session is treated as still actively writing.
#
# The current architecture does not record which exact log file a live
# session is piping to anywhere retrievable after creation (the path is
# computed once in _start_session_logging() and its return value is
# discarded by both callers) -- there is no session-to-log-path registry
# to consult. The only reliable, provable primitive is therefore
# device-level: "does a production or Discovery session for this exact
# device currently exist?". Protection is applied at that granularity,
# deliberately never guessing the exact active file from timestamp,
# filename, size, or modification time (see module docstring for why: it
# would be a guess, not a proof) -- an active device fails closed for
# individual-file deletion, device-all deletion, and (transitively)
# global-all deletion.
# --------------------------------------------------------------------------


def _device_has_active_session(device_name: str) -> bool:
    """True if a production or Discovery bootstrap session for this exact
    device currently exists -- the only two session kinds that ever
    attach persistent logging. This is a read-only check: it never
    creates, closes, or otherwise touches either session."""
    return _session_exists(derive_production_session_name(device_name)) or _session_exists(
        derive_discovery_session_name(device_name)
    )


def _eligible_log_files(device_name: str) -> list[Path]:
    """Resolved filesystem paths for device_name's own eligible log files,
    derived from list_device_logs() -- never a second enumeration. A
    symlink is excluded defensively: it is not a persistent log this
    subsystem itself wrote, so it is simply never an eligible deletion
    target (unlinking a symlink only ever removes the link itself, never
    a target it points to, but this keeps deletion scoped to exactly the
    regular files show logging already exposes)."""
    log_dir = _device_log_dir(device_name)
    paths = []
    for _started, filename in list_device_logs(device_name):
        path = log_dir / filename
        if path.is_symlink():
            continue
        paths.append(path)
    return paths


def _unlink_eligible_log(path: Path) -> None:
    """Delete exactly one already-resolved eligible log file. Defense in
    depth beyond the exact-enumeration match that produced `path`: refuse
    anything that is not a plain file confined under LOGS_ROOT (a
    belt-and-suspenders check; a path outside LOGS_ROOT or a non-regular
    file can never actually reach here through the exact-match callers
    below, since every path passed in was built from LOGS_ROOT / a
    listed device / a listed filename)."""
    resolved_root = LOGS_ROOT.resolve()
    resolved_path = path.resolve()
    if resolved_root != resolved_path and resolved_root not in resolved_path.parents:
        raise TerminalError("Refusing to delete a file outside the terminal log root.")
    if path.is_symlink() or not path.is_file():
        raise TerminalError("Refusing to delete a non-regular terminal log file.")
    path.unlink()


@dataclass(frozen=True)
class DeletionPlan:
    """An immutable, comparable snapshot of exactly what one `delete
    logging ...` operation would do -- built by one of the
    build_*_deletion_plan() functions below, applied (unchanged) by
    apply_deletion_plan(). `device_name` is the single targeted device
    for a device-scoped operation, or None for a global one. `files` and
    `directories` are sorted deterministically so two independently-built
    plans for the same real state always compare equal, and the
    confirm-then-re-preflight flow (see cli/main.py) can detect drift by
    simple equality: build once to show the user what will happen, build
    again right after they confirm, and only apply if the two plans are
    identical -- never trusting a stale, possibly-outdated plan."""

    device_name: str | None
    files: tuple[Path, ...]
    directories: tuple[str, ...]


def build_file_deletion_plan(device_name: str, filename: str) -> DeletionPlan:
    """Preflight for `delete logging <device> <log-file>`. Raises
    TerminalError (nothing to plan, nothing deleted) if the filename does
    not exactly match one of that device's own already-listed eligible
    logs, or if the device currently has an active writer (device-level
    fail-closed -- see module docstring above)."""
    if not _NAME_RE.match(device_name):
        raise TerminalError(f"No log file '{filename}' for device '{device_name}'.")
    eligible = {path.name: path for path in _eligible_log_files(device_name)}
    if filename not in eligible:
        raise TerminalError(f"No log file '{filename}' for device '{device_name}'.")
    if _device_has_active_session(device_name):
        raise TerminalError(f"Cannot delete an active terminal log for device '{device_name}'.")
    return DeletionPlan(device_name, (eligible[filename],), ())


def build_device_all_deletion_plan(device_name: str) -> DeletionPlan:
    """Preflight for `delete logging <device> all`. Raises TerminalError
    if the device has no eligible logs at all, or if it currently has an
    active writer. The device's own logging *directory* is deliberately
    never part of this plan -- `all` only ever targets files;
    use build_device_directory_deletion_plan() for
    directory removal."""
    if not _NAME_RE.match(device_name):
        raise TerminalError(f"No terminal logs found for device '{device_name}'.")
    targets = _eligible_log_files(device_name)
    if not targets:
        raise TerminalError(f"No terminal logs found for device '{device_name}'.")
    if _device_has_active_session(device_name):
        raise TerminalError(f"Cannot delete all logs for '{device_name}' while a terminal log is active.")
    return DeletionPlan(device_name, tuple(sorted(targets, key=str)), ())


def build_device_directory_deletion_plan(device_name: str) -> DeletionPlan:
    """Preflight for `delete logging <device> directory`. Raises
    TerminalError (nothing deleted) if: the device has no valid logging
    directory at all; that directory contains anything other than
    eligible terminal log files (an unknown regular file, a nested
    directory, or any symlink -- including one shaped like a
    '<timestamp>.log' name); or the device currently has an active
    writer. Deliberately non-recursive: this only ever inspects the
    device directory's own direct entries, never descends further."""
    if not _NAME_RE.match(device_name) or device_name not in list_logged_device_ids():
        raise TerminalError(f"No logging directory found for device '{device_name}'.")
    device_dir = _device_log_dir(device_name)
    eligible_names = {path.name for path in _eligible_log_files(device_name)}
    for entry in device_dir.iterdir():
        if entry.is_symlink() or entry.is_dir() or entry.name not in eligible_names:
            raise TerminalError(
                f"Cannot delete logging directory for '{device_name}' because it contains "
                "non-terminal-log entries."
            )
    if _device_has_active_session(device_name):
        raise TerminalError(
            f"Cannot delete logging directory for '{device_name}' while a managed terminal session is still open."
        )
    files = tuple(sorted((device_dir / name for name in eligible_names), key=str))
    return DeletionPlan(device_name, files, (device_name,))


def build_global_all_deletion_plan() -> DeletionPlan:
    """Preflight for `delete logging all`. Preflights the WHOLE operation
    before deleting anything: if ANY device targeted by this call (one
    with at least one eligible log) currently has an active writer,
    nothing at all is deleted -- not even the logs of devices that are
    themselves inactive. Device directories are never part of this plan
    (see build_device_all_deletion_plan()'s note -- the same "files
    only" policy applies globally)."""
    all_targets: list[Path] = []
    active_devices: list[str] = []
    for device_name in list_logged_device_ids():
        targets = _eligible_log_files(device_name)
        if not targets:
            continue
        if _device_has_active_session(device_name):
            active_devices.append(device_name)
            continue
        all_targets.extend(targets)
    if active_devices:
        raise TerminalError(
            "Cannot delete all terminal logs while terminal logs are active. "
            "Close or wait for the active sessions first."
        )
    if not all_targets:
        raise TerminalError("No terminal logs found.")
    return DeletionPlan(None, tuple(sorted(all_targets, key=str)), ())


def build_global_directory_deletion_plan() -> DeletionPlan:
    """Preflight for `delete logging all directory`. Reuses
    build_device_directory_deletion_plan() once per valid device logging
    directory -- the exact same per-device safety checks (active writer,
    unknown entries, symlinks), so there is no duplicated safety logic --
    and lets the first unsafe device's own TerminalError (already naming
    that device) propagate, blocking the entire operation before
    anything is deleted."""
    device_names = list_logged_device_ids()
    if not device_names:
        raise TerminalError("No device logging directories found.")
    all_files: list[Path] = []
    for device_name in device_names:
        plan = build_device_directory_deletion_plan(device_name)
        all_files.extend(plan.files)
    return DeletionPlan(None, tuple(sorted(all_files, key=str)), tuple(sorted(device_names)))


def _rmdir_device_directory(device_name: str) -> None:
    """Remove exactly one now-empty device logging directory. Non-recursive
    (`rmdir`, never `shutil.rmtree`/recursive unlink) -- if the directory
    is not actually empty (an unknown entry appeared after the plan was
    built and applied, which should never happen given a correctly
    re-preflighted plan), this raises OSError rather than silently
    removing unexpected contents. Defense in depth beyond the exact-name
    match that produced this call: refuse anything that is not a direct,
    non-symlink child of LOGS_ROOT."""
    device_dir = _device_log_dir(device_name)
    resolved_root = LOGS_ROOT.resolve()
    resolved_dir = device_dir.resolve()
    if resolved_dir.parent != resolved_root:
        raise TerminalError("Refusing to remove a directory outside the terminal log root.")
    if device_dir.is_symlink() or not device_dir.is_dir():
        raise TerminalError("Refusing to remove a non-directory terminal log path.")
    device_dir.rmdir()


def apply_deletion_plan(plan: DeletionPlan) -> None:
    """Apply an already-built, already-reconfirmed DeletionPlan: unlink
    every planned file, then rmdir every planned (now-empty) device
    directory. Callers (cli/main.py) are responsible for confirmation and
    for re-building the plan immediately beforehand to catch drift -- see
    module docstring's re-preflight requirement; this function itself
    performs no confirmation and no additional safety checks beyond what
    _unlink_eligible_log()/_rmdir_device_directory() always enforce."""
    for path in plan.files:
        _unlink_eligible_log(path)
    for device_name in plan.directories:
        _rmdir_device_directory(device_name)


# --------------------------------------------------------------------------
# Transport command construction
# --------------------------------------------------------------------------


def _require_binary(binary: str) -> None:
    if shutil.which(binary) is None:
        raise TerminalError(f"The '{binary}' binary is not available on this system.")


def _ssh_target(config: dict, default_port: int) -> tuple[str, int]:
    address = config.get("address")
    if not address:
        raise TerminalError("Device configuration is missing 'address'.")
    port = config.get("port", default_port)
    username = config.get("username")
    target = f"{username}@{address}" if username else str(address)
    return target, port


def _build_transport_command(device_config: dict, *, accept_new_host_keys: bool = False) -> tuple[str, list[str]]:
    """`accept_new_host_keys` is used only by the private Discovery bootstrap
    path (see discovery.py): Discovery is an unattended flow with no human
    to answer an interactive host-key confirmation prompt, so it passes
    `-o StrictHostKeyChecking=accept-new` (still verifies/records the key,
    it just never blocks on a fresh one) instead of automating that prompt.
    terminal_open() (human/Claude-driven) never sets this -- its behavior is
    completely unchanged."""
    transport = device_config.get("transport")
    address = device_config.get("address")
    if not address:
        raise TerminalError("Device configuration is missing 'address'.")

    if transport == "ssh":
        _require_binary("ssh")
        target, port = _ssh_target(device_config, 22)
        host_key_opts = (
            ["-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=no"] if accept_new_host_keys else []
        )
        command = ["ssh", *host_key_opts, "-p", str(port), target]

        jump_host = device_config.get("jump_host_config")
        if jump_host is not None:
            # Single-hop native OpenSSH ProxyJump (-J jump-target:jump-port).
            # No shell-hop automation: OpenSSH itself opens the second SSH
            # connection through the first, and the tmux pane still just
            # sees one interactive session to read/send against, exactly
            # like a direct connection.
            jump_target, jump_port = _ssh_target(jump_host, 22)
            command = ["ssh", *host_key_opts, "-J", f"{jump_target}:{jump_port}", "-p", str(port), target]

        return transport, command

    if transport == "telnet":
        _require_binary("telnet")
        port = device_config.get("port", 23)
        return transport, ["telnet", str(address), str(port)]

    raise TerminalError(f"Unsupported transport '{transport}'. Expected 'ssh' or 'telnet'.")


# --------------------------------------------------------------------------
# Private managed-terminal SSH authentication
#
# Closes the gap between "native SSH session created" and "usable for
# terminal_send()/terminal_read()" for a password-authenticated device:
# previously, terminal_open() never automated a password prompt at all
# (only Discovery's separate bootstrap login did), leaving the AI/human to
# discover and answer it interactively -- but the whole point of a
# *managed* device's private access-info is that the password already
# belongs to Network Lab MCP, not the AI. This reuses
# resolve_target_password_prompt() (above) unchanged, so the exact same
# jump-host/ambiguous-prompt fail-closed rule Discovery already relies on
# protects managed opens too -- never a second, independently-written
# password-prompt parser.
#
# Deliberately transport-level, not device-CLI-specific (never parses an
# IOS XR/any other vendor prompt): the only things ever recognized here
# are OpenSSH's own password prompt and a small, stable set of OpenSSH's
# own authentication-failure messages. This keeps it safe for every
# device type terminal_open() supports, not just IOS XR, and matches the
# project's existing "Claude reads the pane" model for anything beyond
# that -- if no password prompt or failure ever appears (key/agent auth,
# a host-key confirmation prompt, a slow connection, telnet), this
# function does nothing further and normal interactive use proceeds
# unaffected.
# --------------------------------------------------------------------------

# Bounded waits, deliberately separate constants from Discovery's own
# LOGIN_TIMEOUT_SECONDS/COMMAND_TIMEOUT_SECONDS (do not change Discovery's
# existing timeouts) -- similar order of magnitude,
# tuned for one interactive terminal_open() MCP call rather than an
# unattended multi-command bootstrap collection.
_MANAGED_LOGIN_TIMEOUT_SECONDS = 15
_MANAGED_AUTH_SETTLE_TIMEOUT_SECONDS = 15

# A small, stable set of OpenSSH client-side (never device-CLI) messages
# that unambiguously mean authentication did not succeed. Deliberately not
# extended to generic device-CLI error text -- see module docstring above.
_SSH_AUTH_FAILURE_RE = re.compile(
    r"Permission denied|Authentication failed|Connection closed by|Connection refused|"
    r"Connection timed out|Host key verification failed",
    re.IGNORECASE,
)
# re.MULTILINE + re.IGNORECASE re-applied on the combined pattern: both
# flags are set individually on PASSWORD_PROMPT_RE/
# _SSH_AUTH_FAILURE_RE, but combining their `.pattern` text into a new
# re.compile() call does not carry those flags forward -- without
# re.MULTILINE here, `$` only anchors to the true end of the whole
# captured string, so a match on a line that is not the very last line at
# capture time (e.g. once something else has already been appended after
# it) silently fails.
_MANAGED_LOGIN_WAIT_RE = re.compile(
    f"(?:{PASSWORD_PROMPT_RE.pattern})|(?:{_SSH_AUTH_FAILURE_RE.pattern})", re.MULTILINE | re.IGNORECASE
)
# Matches literally any visible content -- used only together with
# _wait_for_pattern()'s own baseline-diff requirement, so this really means
# "wait until the pane changes at all", with no vendor-specific assumption
# about what the new content looks like.
_ANY_VISIBLE_CONTENT_RE = re.compile(r"\S")


def _last_nonblank_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line
    return ""


def _authenticate_managed_session(
    device_name: str, device_config: dict, session_name: str, *, newly_created: bool
) -> None:
    """Dispatch managed private authentication by transport: `ssh` uses
    `_authenticate_managed_ssh_session()`; `telnet` uses
    `_authenticate_managed_telnet_session()`; any other/unknown transport
    does nothing. Splitting by transport here -- rather than inside one
    large function -- keeps each
    flow's own prompt/failure vocabulary (OpenSSH's for SSH, classic-IOS-
    style for Telnet) from leaking into the other."""
    transport = device_config.get("transport")
    if transport == "ssh":
        _authenticate_managed_ssh_session(device_name, device_config, session_name, newly_created=newly_created)
    elif transport == "telnet":
        _authenticate_managed_telnet_session(device_name, device_config, session_name, newly_created=newly_created)


def _authenticate_managed_ssh_session(
    device_name: str, device_config: dict, session_name: str, *, newly_created: bool
) -> None:
    """Complete SSH password authentication for a managed session if (and
    only if) the target's own SSH password prompt is currently, or
    imminently, showing -- never otherwise. Fails closed (TerminalError,
    always sanitized: never includes the password or any other
    access-info content) on a wrong-host/jump-host/ambiguous prompt, a
    missing configured password, a repeated identical prompt after one
    send, or an explicit SSH authentication-failure message. Sends the
    password at most once per call.

    `newly_created` decides how the *first* observation is made, which is
    what keeps an already-authenticated, already-idempotent session
    completely undisturbed: a brand-new
    session's connection is still in flight, so this polls briefly for a
    prompt/failure to first appear; an already-existing session's pane is
    already settled, so this takes exactly one immediate read-only
    capture -- if that does not show a password prompt right now (the
    ordinary case: already authenticated, or showing unrelated output),
    nothing further happens: no send, no wait, no disturbance."""
    if newly_created:
        try:
            text = _wait_for_pattern(session_name, _MANAGED_LOGIN_WAIT_RE, _MANAGED_LOGIN_TIMEOUT_SECONDS)
        except TerminalError:
            return  # no password prompt and no failure signal within the bounded wait -- proceed
    else:
        text = _capture_pane(session_name, HISTORY_LIMIT)

    last_line = _last_nonblank_line(text)
    if not PASSWORD_PROMPT_RE.search(last_line):
        return  # not currently at a password prompt -- nothing to do

    outcome = resolve_target_password_prompt(device_config, last_line)
    if not outcome.matched_target:
        raise TerminalError(
            f"Device '{device_name}': cannot safely complete authentication ({outcome.reason}). "
            "Configure key/agent-based (non-interactive) SSH authentication, or answer the "
            "prompt manually via terminal_send()/terminal_read()."
        )
    if not outcome.password:
        raise TerminalError(
            f"Device '{device_name}': a password prompt appeared but no private password is "
            "configured in the active access-info definition."
        )

    baseline = text
    _send_secret_text(session_name, outcome.password)
    _send_enter(session_name)
    try:
        settled = _wait_for_pattern(
            session_name, _ANY_VISIBLE_CONTENT_RE, _MANAGED_AUTH_SETTLE_TIMEOUT_SECONDS, baseline_text=baseline
        )
    except TerminalError as exc:
        raise TerminalError(f"Device '{device_name}': timed out waiting for authentication to complete.") from exc

    settled_last_line = _last_nonblank_line(settled)
    if PASSWORD_PROMPT_RE.search(settled_last_line):
        # The same prompt reappeared -- the password was rejected. Never
        # send it again: one attempt per call.
        raise TerminalError(f"Device '{device_name}': SSH authentication failed (password rejected).")
    if _SSH_AUTH_FAILURE_RE.search(settled_last_line) or _SSH_AUTH_FAILURE_RE.search(settled):
        raise TerminalError(f"Device '{device_name}': SSH authentication failed.")
    # Anything else that appeared is treated as authentication having
    # progressed past the password prompt -- matching the project's
    # existing "Claude reads the pane" model for whatever comes next,
    # never a vendor-specific device-prompt parser.


# --------------------------------------------------------------------------
# Private managed-terminal Telnet authentication
#
# Closes the last transport inconsistency: SSH managed sessions and
# Discovery's own Telnet login (discovery._login_ios_style()) both
# already authenticate privately -- managed Telnet sessions did not. A
# real Claude Code -> Network Lab MCP acceptance test hit this
# exact gap on a real PAGENT-style device: terminal_open() reached
# "Password:" over Telnet and simply stopped, leaving the AI to ask a
# human for the password.
#
# Deliberately narrow, matching this project's real target (Cisco IOS-
# style Telnet lab sessions, e.g. PAGENT): only an optional "Username:"
# prompt followed by "Password:" is ever answered, each at most once, and
# only the shared IOS_STYLE_PROMPT_RE exec-prompt shape counts as having
# reached a usable terminal -- not a generic interactive-login framework,
# not TACACS/OTP/MFA/enable-password automation, and once that exec prompt
# is reached this stops watching entirely (never a persistent prompt-
# answering loop over the life of the session). Reuses
# resolve_target_password_prompt() unchanged for the password itself: a
# Telnet device structurally cannot have a jump_host_config (lab.py's
# schema validation requires transport 'ssh' for that), so its "no
# jump_host_config -> unambiguous" branch already answers a Telnet
# password prompt correctly with no Telnet-specific attribution logic.
# --------------------------------------------------------------------------

# A small, stable set of classic-IOS-style Telnet login-failure text --
# deliberately not a large error-string catalog (see module docstring
# above): observed directly against a real device during this feature's
# own development ("% Bad passwords", "Connection closed by foreign
# host." after repeated bad attempts) plus the commonly documented
# "% Login invalid". Never a generic device-CLI error catalog -- this is
# specifically about Telnet *login* failing, nothing else.
_TELNET_AUTH_FAILURE_RE = re.compile(
    r"%\s*Bad passwords|%\s*Login invalid|Connection closed by foreign host",
    re.IGNORECASE,
)
# re.MULTILINE + re.IGNORECASE re-applied on the combined pattern -- see
# _MANAGED_LOGIN_WAIT_RE's own comment above for why this is required, not
# optional.
_TELNET_MANAGED_LOGIN_WAIT_RE = re.compile(
    f"(?:{USERNAME_PROMPT_RE.pattern})|(?:{PASSWORD_PROMPT_RE.pattern})|(?:{IOS_STYLE_PROMPT_RE.pattern})|"
    f"(?:{_TELNET_AUTH_FAILURE_RE.pattern})",
    re.MULTILINE | re.IGNORECASE,
)


def _authenticate_managed_telnet_session(
    device_name: str, device_config: dict, session_name: str, *, newly_created: bool
) -> None:
    """Complete classic-IOS-style Telnet login for a managed session if
    (and only if) it is currently, or imminently, sitting at a bounded
    Username:/Password: login prompt -- never otherwise. Fails closed
    (TerminalError, always sanitized: never includes the username,
    password, or any other access-info content) on a missing configured
    username/password, a repeated identical prompt after one send, or an
    explicit Telnet login-failure message. Sends the username (if
    prompted) and the password each at most once per call.

    `newly_created` mirrors `_authenticate_managed_ssh_session()`'s own
    contract exactly, for the same reason: a brand-new session's Telnet
    connection is still in flight (and may still be printing transport
    preamble text like "Trying .../Connected to.../Escape character is
    '^]'." -- never mistaken for login completion, since none of it
    matches the bounded login/failure patterns below), so this polls
    briefly for a recognizable state to first appear; an already-existing
    session's pane is already settled, so this takes exactly one
    immediate read-only capture -- if that is not clearly a Username:/
    Password: prompt right now, nothing further happens at all (no
    guessing at ambiguous CLI output)."""
    if newly_created:
        try:
            text = _wait_for_pattern(session_name, _TELNET_MANAGED_LOGIN_WAIT_RE, _MANAGED_LOGIN_TIMEOUT_SECONDS)
        except TerminalError:
            return  # no recognizable login/prompt/failure state within the bounded wait -- proceed
    else:
        text = _capture_pane(session_name, HISTORY_LIMIT)

    last_line = _last_nonblank_line(text)

    if USERNAME_PROMPT_RE.search(last_line):
        username = device_config.get("username")
        if not username:
            raise TerminalError(
                f"Device '{device_name}': a username prompt appeared but no username is configured "
                "in the active access-info definition."
            )
        _send_secret_text(session_name, str(username))
        _send_enter(session_name)
        try:
            text = _wait_for_pattern(
                session_name, _TELNET_MANAGED_LOGIN_WAIT_RE, _MANAGED_LOGIN_TIMEOUT_SECONDS, baseline_text=text
            )
        except TerminalError as exc:
            raise TerminalError(
                f"Device '{device_name}': timed out waiting for a response after the username."
            ) from exc
        last_line = _last_nonblank_line(text)

    if not PASSWORD_PROMPT_RE.search(last_line):
        if _TELNET_AUTH_FAILURE_RE.search(text):
            raise TerminalError(f"Device '{device_name}': Telnet login failed.")
        return  # not currently at a password prompt -- nothing to do

    outcome = resolve_target_password_prompt(device_config, last_line)
    if not outcome.matched_target:
        raise TerminalError(
            f"Device '{device_name}': cannot safely complete authentication ({outcome.reason})."
        )
    if not outcome.password:
        raise TerminalError(
            f"Device '{device_name}': a password prompt appeared but no private password is "
            "configured in the active access-info definition."
        )

    baseline = text
    _send_secret_text(session_name, outcome.password)
    _send_enter(session_name)
    try:
        settled = _wait_for_pattern(
            session_name, _TELNET_MANAGED_LOGIN_WAIT_RE, _MANAGED_AUTH_SETTLE_TIMEOUT_SECONDS, baseline_text=baseline
        )
    except TerminalError as exc:
        raise TerminalError(f"Device '{device_name}': timed out waiting for authentication to complete.") from exc

    settled_last_line = _last_nonblank_line(settled)
    if IOS_STYLE_PROMPT_RE.search(settled_last_line):
        return  # success -- reached the device's own exec prompt
    if PASSWORD_PROMPT_RE.search(settled_last_line):
        # The same prompt reappeared -- the password was rejected. Never
        # send it again: one attempt per call.
        raise TerminalError(f"Device '{device_name}': Telnet authentication failed (password rejected).")
    if _TELNET_AUTH_FAILURE_RE.search(settled):
        raise TerminalError(f"Device '{device_name}': Telnet authentication failed.")
    # Unlike SSH's more lenient "anything else counts as progress" model,
    # Telnet requires positively reaching the known exec-prompt shape --
    # the narrower, already-proven signal Discovery's own Telnet login
    # relies on (see IOS_STYLE_PROMPT_RE's own docstring) -- rather than
    # assuming an unrecognized response means success.
    raise TerminalError(f"Device '{device_name}': did not reach a device prompt after Telnet login.")


# --------------------------------------------------------------------------
# Public production API (used by the terminal_* MCP tools)
# --------------------------------------------------------------------------


def open_device_terminal(device_name: str, device_config: dict) -> dict:
    """Open (or reuse) the production terminal session for an active-topology device.

    Serialized per-device: a concurrent open/send/read/close for
    this same device waits its turn instead of racing this one's
    check-then-create against it; a different device's call uses a
    different lock and proceeds independently -- so concurrent opens of
    the *same* device can never send its password twice (the second call
    blocks until the first's whole open-and-authenticate sequence
    finishes), while different devices continue to authenticate fully in
    parallel.

    Completes private authentication using the device's own access-info
    credentials if (and only if) a recognized login prompt actually
    appears -- SSH password authentication or Telnet
    username/password login, depending on the device's own
    configured transport; see _authenticate_managed_session()'s own
    docstring for the exact per-transport rules. If *this* call created a
    new session and authentication definitively fails, that now-unusable
    session is closed (never a pre-existing session this call merely
    reused)."""
    session_name = derive_production_session_name(device_name)
    with _session_lock(session_name):
        transport, command = _build_transport_command(device_config)
        reused = _ensure_managed_session(session_name, command, log_device_name=device_name)
        try:
            _authenticate_managed_session(device_name, device_config, session_name, newly_created=not reused)
        except TerminalError:
            if not reused:
                _close_session(session_name)
            raise
        return {
            "device": device_name,
            "session_name": session_name,
            "reused": reused,
            "transport": transport,
        }


def send_to_device(device_name: str, text: str | None, keys: list[str] | None, enter: bool) -> dict:
    """Send input to a device's production session: text, then keys, then Enter
    (in that order). Serialized per-device: see open_device_terminal()."""
    session_name = derive_production_session_name(device_name)
    with _session_lock(session_name):
        if text:
            _send_literal_text(session_name, text)
        if keys:
            _send_special_keys(session_name, keys)
        if enter:
            _send_enter(session_name)
        return {"device": device_name, "session_name": session_name}


def read_device(device_name: str, lines: int = DEFAULT_READ_LINES) -> dict:
    """Capture recent pane content from a device's production session.
    Serialized per-device: see open_device_terminal()."""
    session_name = derive_production_session_name(device_name)
    with _session_lock(session_name):
        content = _capture_pane(session_name, lines)
        return {"device": device_name, "session_name": session_name, "content": content}


def list_device_sessions() -> list[dict]:
    """List managed production sessions (network-lab-device-* namespace only).

    Deliberately not per-device-locked: this is a read-only enumeration
    over whatever tmux reports at the moment it is asked (best-effort,
    matching the pre-existing contract), and tmux's own `list-sessions` is
    already a single atomic query against its server -- serializing it
    against every device's lock would only add contention, not
    correctness."""
    sessions = []
    for session_name in _list_sessions(PRODUCTION_PREFIX):
        sessions.append(
            {
                "device": production_device_name(session_name),
                "session_name": session_name,
                "state": _pane_state(session_name),
            }
        )
    return sessions


def close_device_terminal(device_name: str) -> dict:
    """Close a device's production session. Never touches the validation
    namespace. Serialized per-device: see open_device_terminal()."""
    session_name = derive_production_session_name(device_name)
    with _session_lock(session_name):
        closed = _close_session(session_name)
        return {"device": device_name, "session_name": session_name, "closed": closed}


# --------------------------------------------------------------------------
# Read-only human observation (`terminal monitor <device-id>`)
#
# Deliberately separate from terminal_read()/read_device(): that is an MCP
# *operation* (part of the interactive AI terminal contract); this is a
# passive, continuously-repeated human view of the exact same production
# session, with its own three-state model (waiting/active/ended) a live
# monitor UI needs and terminal_read() has no reason to expose. Pure
# observation: has-session/list-panes/capture-pane only, in that order,
# never send-keys/new-session/kill-session -- see cli/main.py's monitor UI,
# which never calls anything else here.
#
# Deliberately NOT wrapped in _session_lock(): every call this makes is
# already a plain read (no check-then-act mutation to protect), so the
# only "race" possible is the session disappearing between this
# function's own two tmux calls -- which is exactly the WAITING
# transition the monitor is designed to tolerate (see
# capture_device_terminal_view()'s docstring), not a bug to prevent.
# Holding the per-device lock here would additionally serialize a
# continuously-polling human monitor against the AI's own interactive
# terminal_send()/terminal_read() for that device for no correctness
# benefit -- and since `terminal monitor` normally runs in a separate
# `./run_cli.sh` process with its own empty, process-local lock registry
# (these locks are in-memory, not cross-process), taking the lock
# here could not provide real cross-process exclusion even if it were
# otherwise desirable.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminalMonitorSnapshot:
    """One read-only observation of a device's terminal activity, for a
    live human monitor view. `status` is exactly one of:

        "waiting"  neither a production nor a Discovery session currently
                   exists for this device
        "active"   the selected session/pane exists and its process is
                   still running
        "ended"    the selected session/pane exists but its process has
                   exited (tmux's `remain-on-exit`) -- `pane_text` is its
                   last content, not live output

    `source` says which session `status`/`pane_text` describe:
    "managed" (the normal production session), "discovery" (a Discovery
    bootstrap session, in the absence of a managed one), or "none" (status
    is always "waiting" then).

    `pane_text` is the *current visible pane*, not the full scrollback
    transcript (that role belongs to the persistent per-session log file,
    see _start_session_logging()) -- always "" for "waiting"."""

    device_id: str
    status: str
    source: str
    pane_text: str


def _observe_named_session(session_name: str, lines: int) -> tuple[str, str] | None:
    """Read-only has-session/list-panes/capture-pane observation of one
    already-derived session name. Returns None if the session does not
    exist, or on an ordinary transient tmux observation hiccup
    indistinguishable from that (see _pane_state()'s own "unknown" case)
    -- including the target vanishing between the state check and the
    capture (e.g. a concurrent terminal_close()/Discovery cleanup) -- the
    caller's fallback/WAITING handling either way; this never raises for
    that. Otherwise returns (status, pane_text) with status "active" or
    "ended"."""
    state = _pane_state(session_name)
    if state == "unknown":
        return None
    try:
        pane_text = _capture_pane(session_name, lines)
    except TerminalError:
        return None
    return ("ended" if state == "exited" else "active", pane_text)


def capture_device_terminal_view(device_name: str, lines: int = DEFAULT_READ_LINES) -> TerminalMonitorSnapshot:
    """Observe the currently preferred terminal activity for a device, for
    `terminal monitor`. Never creates, closes, or sends
    anything -- see the module section docstring above.

    Source priority, re-evaluated fresh on every call (so a managed
    session appearing/disappearing, or a Discovery session appearing/
    disappearing, is picked up on the very next observation with no
    special-casing needed):

        managed session > Discovery session > waiting

    A normal managed production session (derive_production_session_name())
    is preferred whenever it exists; only when it does not is a Discovery
    bootstrap session (derive_discovery_session_name()) for the same
    device considered instead -- reusing the exact same SSOT session-name
    helpers and observation primitive (_observe_named_session()) either
    way, never a second implementation. This never creates a Discovery
    session merely by being asked to observe one: discover_topology() (via
    terminal.open_bootstrap_terminal()) remains the only thing that ever
    creates one, and this monitor never delays or blocks its cleanup --
    it is a plain read, so a concurrent kill-session (e.g. Discovery's own
    cleanup) is just the ordinary vanished-target race
    _observe_named_session() already tolerates."""
    managed = _observe_named_session(derive_production_session_name(device_name), lines)
    if managed is not None:
        status, pane_text = managed
        return TerminalMonitorSnapshot(device_name, status, "managed", pane_text)
    discovered = _observe_named_session(derive_discovery_session_name(device_name), lines)
    if discovered is not None:
        status, pane_text = discovered
        return TerminalMonitorSnapshot(device_name, status, "discovery", pane_text)
    return TerminalMonitorSnapshot(device_name, "waiting", "none", "")


def discovery_device_name(session_name: str) -> str:
    """Recover the device name encoded in a Discovery bootstrap session
    name -- the Discovery-namespace counterpart of production_device_name()."""
    if not is_discovery_session(session_name):
        raise TerminalError(f"'{session_name}' is not a Discovery session.")
    return session_name[len(DISCOVERY_PREFIX) :]


def list_discovery_device_ids() -> list[str]:
    """Device IDs that currently have an active Discovery bootstrap
    session. `terminal monitor` target-eligibility use only
    (a device being discovered for the first time may not yet be in the
    committed active topology at all) -- never a public MCP/terminal_*
    surface, and this enumeration itself never creates, closes, or
    observes pane content for anything."""
    return [discovery_device_name(name) for name in _list_sessions(DISCOVERY_PREFIX)]


# --------------------------------------------------------------------------
# Private Discovery bootstrap connectivity (see discovery.py)
#
# Not exposed as an MCP tool and not reachable through terminal_open()'s
# public device-namespace/active-topology restriction, which stays
# completely unchanged. This reuses the exact same session primitives and
# _build_transport_command() (direct SSH and single-hop ProxyJump alike) as
# production, in a structurally separate namespace so a Discovery session
# can never be mistaken for, list alongside, or be closed by any public
# terminal_* tool call.
# --------------------------------------------------------------------------


def open_bootstrap_terminal(device_name: str, device_config: dict) -> dict:
    """Open a private, temporary session for Discovery only. Always creates
    a fresh session (Discovery never reuses a prior bootstrap session -- see
    close_bootstrap_terminal(), which callers must use once collection for
    that device finishes).

    Serialized per-device, using the Discovery namespace's own
    lock key (structurally distinct from that device's production session
    lock -- see derive_discovery_session_name()) -- one device's parallel
    Discovery collector thread never contends with another device's."""
    session_name = derive_discovery_session_name(device_name)
    with _session_lock(session_name):
        _close_session(session_name)  # never reuse a stale bootstrap session
        transport, command = _build_transport_command(device_config, accept_new_host_keys=True)
        _ensure_managed_session(session_name, command, log_device_name=device_name)
        return {"device": device_name, "session_name": session_name, "transport": transport}


def send_to_bootstrap(device_name: str, text: str | None, keys: list[str] | None, enter: bool) -> None:
    session_name = derive_discovery_session_name(device_name)
    with _session_lock(session_name):
        if text:
            _send_literal_text(session_name, text)
        if keys:
            _send_special_keys(session_name, keys)
        if enter:
            _send_enter(session_name)


def send_secret_to_bootstrap(device_name: str, secret: str) -> None:
    """Like send_to_bootstrap(), but for credential input only (a Discovery
    login's username or password): types `secret` without ever placing it
    in any subprocess's own argv -- see _send_secret_text()'s own
    docstring. Always followed by Enter, matching every current caller's
    own usage (Discovery answers exactly one line of credential input at a
    time)."""
    session_name = derive_discovery_session_name(device_name)
    with _session_lock(session_name):
        _send_secret_text(session_name, secret)
        _send_enter(session_name)


def wait_for_bootstrap_pattern(
    device_name: str, pattern: "re.Pattern[str]", timeout: float, baseline_text: str | None = None
) -> str:
    session_name = derive_discovery_session_name(device_name)
    with _session_lock(session_name):
        return _wait_for_pattern(session_name, pattern, timeout, baseline_text=baseline_text)


def read_bootstrap(device_name: str, lines: int = HISTORY_LIMIT) -> str:
    session_name = derive_discovery_session_name(device_name)
    with _session_lock(session_name):
        return _capture_pane(session_name, lines)


def close_bootstrap_terminal(device_name: str) -> bool:
    session_name = derive_discovery_session_name(device_name)
    with _session_lock(session_name):
        return _close_session(session_name)


# --------------------------------------------------------------------------
# Internal local-validation helpers
#
# Not exposed as MCP tools and not a public transport. These exist only so
# that local validation can exercise the exact same
# session-management primitives used in production, against a safe local
# command instead of `ssh`/`telnet`.
# --------------------------------------------------------------------------


def open_validation_session(validation_id: str, command: list[str]) -> dict:
    session_name = derive_validation_session_name(validation_id)
    reused = _ensure_managed_session(session_name, command)
    return {"validation_id": validation_id, "session_name": session_name, "reused": reused}


def send_to_validation(validation_id: str, text: str | None, keys: list[str] | None, enter: bool) -> dict:
    session_name = derive_validation_session_name(validation_id)
    if text:
        _send_literal_text(session_name, text)
    if keys:
        _send_special_keys(session_name, keys)
    if enter:
        _send_enter(session_name)
    return {"validation_id": validation_id, "session_name": session_name}


def read_validation(validation_id: str, lines: int = DEFAULT_READ_LINES) -> str:
    session_name = derive_validation_session_name(validation_id)
    return _capture_pane(session_name, lines)


def validation_session_exists(validation_id: str) -> bool:
    return _session_exists(derive_validation_session_name(validation_id))


def list_validation_sessions() -> list[str]:
    return _list_sessions(VALIDATION_PREFIX)


def close_validation_session(validation_id: str) -> bool:
    session_name = derive_validation_session_name(validation_id)
    return _close_session(session_name)
