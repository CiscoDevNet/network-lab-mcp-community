# SPDX-License-Identifier: Apache-2.0
"""Human-facing interactive Network Lab CLI.

Launched via ./run_cli.sh (`python3 -m network_lab_mcp.cli.main`). This is
the Human Configuration / Control Interface described in README.md: it
edits running-config (lab/settings.yaml: which topology/scenario/references
MCP uses), and topology/access-info/scenario/reference *definitions*,
through a candidate -> commit model, using an IOS XR-compatible interaction
style built on top of the command grammar in cli/grammar.py. It never
speaks the MCP stdio protocol and never performs network engineering
reasoning -- that remains Claude Code's job, driven by the committed
configuration this CLI produces.

Responsibilities kept here: the REPL, prompt rendering, mode transitions,
prompt_toolkit integration (completion/help key bindings, history, line
editing), command dispatch, output rendering, external-editor invocation,
and interactive confirmations. Candidate mutation and commit/clear
semantics live in cli/config.py; command grammar, abbreviation, completion,
and help all live in cli/grammar.py.
"""

from __future__ import annotations

import asyncio
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import yaml
from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.history import History
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl

import network_lab_mcp
from network_lab_mcp import discovery
from network_lab_mcp import lab
from network_lab_mcp import terminal
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import editor
from network_lab_mcp.cli import grammar

# Repo checkout root, for a best-effort `git rev-parse` in `show version`.
# Mirrors lab.find_lab_root()'s own repo-root computation; independent of it
# so `show version` never depends on lab/ existing or being valid.
_REPO_ROOT = Path(network_lab_mcp.__file__).resolve().parent.parent.parent


class _ExitCli(Exception):
    """Internal signal used only to unwind the REPL loop on EXEC exit/quit."""


# --------------------------------------------------------------------------
# Sensitive-command history masking
# --------------------------------------------------------------------------


def _is_password_command(line: str) -> bool:
    """Best-effort, deliberately conservative match: a bare password-setting
    command may be abbreviated (e.g. "pas cisco123"), so a raw secret must
    never enter this process's in-memory history even for an abbreviation.
    Over-matching (skipping a line that only looks like one) is safe; the
    only unsafe direction would be under-matching a real password command."""
    stripped = line.strip()
    if not stripped:
        return False
    first, _, rest = stripped.partition(" ")
    if not rest.strip():
        return False
    return bool(first) and "password".startswith(first.lower())


# --------------------------------------------------------------------------
# Multi-line configuration paste
# --------------------------------------------------------------------------


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _split_pasted_command_lines(text: str) -> list[str]:
    """Split a pasted multi-line block into normalized physical command
    lines: CRLF/CR normalized to LF, leading configuration-display
    indentation stripped from each line, and blank lines dropped.
    Everything else in a line -- internal spacing, punctuation, special
    characters inside a value -- is preserved exactly, so this must never
    be used on a single manually-typed line.

    A standalone "!" separator line (exactly "!" once stripped, as
    produced by the show-configuration renderers) is preserved here as a
    literal "!" entry rather than dropped -- execute_input_block() gives
    it its own narrow, mode-bounded meaning (see _apply_structural_bang())
    instead of treating it as an ordinary command line."""
    lines = []
    for raw_line in _normalize_newlines(text).split("\n"):
        content = raw_line.lstrip(" \t")
        stripped = content.strip()
        if not stripped:
            continue
        lines.append("!" if stripped == "!" else content)
    return lines


# access-info paste round-trip: rendered access-info configuration
# closes every device/jump-host/definition block with a standalone "!"
# (see render_access_info_block()), so pasting it back must let "!" close
# the matching block -- otherwise a sibling "device R2" line right after
# "device R1"'s block is parsed while still inside R1's own submode and
# fails as an unknown command. This is deliberately NOT a generic "!" ==
# "exit" alias: it only applies inside a multi-line paste (never to a
# single manually-typed line, see execute_command_line()/run()), and only
# in the five modes of _STRUCTURAL_BANG_EXIT_MODES -- access-info's three
# modes (access_device/access_jump_host/access_info) and the topology
# definition's two (`device` -> topology -> global), so a rendered topology
# delta returns to global like an access-info block -- plus the `running`
# section modes (conditional, see below; the `running-config` section that
# global `show configuration` emits closes before a following global-level
# line). Scenario/reference modes are unaffected (no CLI body grammar, so
# their show configuration output is display-only YAML, never replayable).
# A stray "!" at global configuration or EXEC
# is an explicit, deliberate no-op (never exit/end/quit, never terminates
# the CLI), so an imperfect copy/paste with one extra trailing "!" cannot
# silently leave configuration mode.
_STRUCTURAL_BANG_EXIT_MODES = ("access_device", "access_jump_host", "access_info", "device", "topology")

# Modes whose `show`/`show configuration` mean the running-config selection
# (candidate delta / committed selection), as opposed to a definition.
_RUNNING_SCOPE_MODES = ("running", *cfgmod.RUNNING_COMMON_MODES, *cfgmod.RUNNING_ENTRY_MODES)


# `running` and the name-less access-info submode are only closed by a "!"
# that follows at least one command executed *inside* that section (one that
# did not itself change mode): a leading "!" (show running-config / show
# configuration output starts with one), a "!" right after entering the mode,
# or a "!" right after `exit` never climbs another level.
_CONTENT_BANG_EXIT_MODES = ("running", *cfgmod.RUNNING_COMMON_MODES)


def _apply_structural_bang(session: cfgmod.CliSession, section_has_content: bool = True) -> None:
    if session.mode in _STRUCTURAL_BANG_EXIT_MODES or (
        session.mode in _CONTENT_BANG_EXIT_MODES and section_has_content
    ):
        h_exit(session, {})


class MaskingHistory(History):
    """In-memory-only command history that never retains a raw password
    value. History is never written to disk -- it does not persist across
    CLI process runs.

    `History.append_string()` unconditionally inserts into the in-memory
    `_loaded_strings` list that Up/Down navigation reads from -- overriding
    only `store_string()` (the persistent-backend hook) would not be enough,
    since that list is populated by `append_string()` itself. A sensitive
    line is therefore dropped before `append_string()` ever runs, so it
    never becomes reachable through history navigation at all."""

    def load_history_strings(self):
        return []

    def store_string(self, string: str) -> None:
        pass

    def append_string(self, string: str) -> None:
        if "\n" in _normalize_newlines(string):
            # A multi-line paste: prompt_toolkit hands back the whole
            # pasted block as one string containing embedded newlines.
            # Never store that raw block (it may contain a password on any
            # line) -- store each physical command line individually
            # instead, still excluding any password line, so Up/Down can
            # still recall the non-sensitive pasted commands. A "!"
            # separator is not itself a recallable command (see
            # _apply_structural_bang()), so it is excluded here exactly
            # like any other non-recallable line.
            for line in _split_pasted_command_lines(string):
                if line != "!" and not _is_password_command(line):
                    super().append_string(line)
            return
        if _is_password_command(string):
            return
        super().append_string(string)


# --------------------------------------------------------------------------
# Prompt / context / rendering helpers
# --------------------------------------------------------------------------


def prompt_text(session: cfgmod.CliSession) -> str:
    if session.mode == "exec":
        return "network-lab# "
    if session.mode == "global":
        return "network-lab(config)# "
    if session.mode == "running":
        return "network-lab(config-running)# "
    if session.mode == "topology":
        return f"network-lab(config-topology-{session.definition_name})# "
    if session.mode == "device":
        return f"network-lab(config-device-{session.current_device_name})# "
    if session.mode == "access_info":
        return f"network-lab(config-access-info-{session.definition_name})# "
    if session.mode == "access_device":
        return f"network-lab(config-access-device-{session.current_device_name})# "
    if session.mode == "access_jump_host":
        return f"network-lab(config-access-jump-host-{session.current_jump_host_name})# "
    if session.mode == "scenario":
        return f"network-lab(config-scenario-{session.definition_name})# "
    if session.mode == "reference":
        return f"network-lab(config-reference-{session.definition_name})# "
    if session.mode in cfgmod.RUNNING_COMMON_MODES:
        return f"network-lab(config-running-{cfgmod.RUNNING_COMMON_MODES[session.mode].replace('_', '-')})# "
    if session.mode in cfgmod.RUNNING_ENTRY_MODES:
        kind = cfgmod.RUNNING_ENTRY_MODES[session.mode].replace("_", "-")
        return f"network-lab(config-running-{kind}-{session.current_running_entry_name})# "
    raise AssertionError(f"Unknown CLI mode: {session.mode!r}")


def _no_definition_candidate_names(
    session: cfgmod.CliSession, kind: str, all_names: tuple[str, ...]
) -> tuple[str, ...]:
    """`no <kind> <name>`: see grammar.CliContext's
    `no_<kind>_candidate_names` docstring for the exact rule -- every
    stored name of this kind when nothing (of any kind) is dirty; only
    this exact identity if it is the one already-dirty definition
    (edit-in-progress or already pending deletion contributes nothing,
    since re-deleting it is not a new legal target); nothing at all if a
    *different* kind or name is dirty."""
    if session.definition_kind == kind and session.definition_dirty():
        if session.definition_candidate is None:
            return ()
        return (session.definition_name,)
    if session.definition_kind is not None and session.definition_dirty():
        return ()
    return tuple(all_names)


def _running_selectable_names(
    session: cfgmod.CliSession, kind: str, all_names: tuple[str, ...]
) -> tuple[str, ...]:
    """`config-running# <kind> <name>` selector: see
    grammar.CliContext's `running_<kind>_names` docstring -- every stored
    name of this kind, except one currently pending whole-definition
    deletion in the (separate) definition-editing candidate scope."""
    if session.definition_kind == kind and session.definition_dirty() and session.definition_candidate is None:
        return tuple(n for n in all_names if n != session.definition_name)
    return tuple(all_names)


def _monitor_terminal_target_names(session: cfgmod.CliSession) -> tuple[str, ...]:
    """`terminal monitor <device-id>`: every device eligible
    to be monitored right now -- see grammar.CliContext.monitor_terminal_
    device_ids's docstring for the exact rule. Used both to build that
    completion field and, independently, by h_monitor_terminal() to
    re-validate the typed device_id at execution time (completion hints
    are never trusted as authoritative, matching every other identifier
    in this CLI).

    Existing Discovery-session device IDs are added to the union: a
    device being discovered for the very first time may not yet be in the
    committed active topology (discover_topology()'s targets come from
    access-info, not the topology), so without this a brand-new device's
    live Discovery activity could never be monitored at all."""
    committed_devices: tuple[str, ...] = ()
    try:
        settings = lab.read_settings(session.lab_root)
        topology_name = lab.get_active_topology_name(settings)
        topology = lab.load_topology(topology_name, session.lab_root)
        committed_devices = tuple((topology.get("devices") or {}).keys())
    except lab.LabConfigError:
        pass
    session_devices = tuple(s["device"] for s in terminal.list_device_sessions())
    discovery_devices = tuple(terminal.list_discovery_device_ids())
    return tuple(dict.fromkeys((*committed_devices, *session_devices, *discovery_devices)))


def build_context(session: cfgmod.CliSession) -> grammar.CliContext:
    lab_root = session.lab_root
    candidate_references: tuple[str, ...] = ()
    candidate_access_info_names: tuple[str, ...] = ()
    candidate_topology_names: tuple[str, ...] = ()
    candidate_scenario_names: tuple[str, ...] = ()
    if session.settings_candidate is not None:
        candidate_references = tuple(session.settings_candidate.get("active_references") or [])
        candidate_access_info_names = tuple(filter(None, (session.settings_candidate.get("active_access_info"),)))
        candidate_topology_names = tuple(filter(None, (session.settings_candidate.get("active_topology"),)))
        candidate_scenario_names = tuple(filter(None, (session.settings_candidate.get("active_scenario"),)))
    topology_device_names: tuple[str, ...] = ()
    access_info_device_names: tuple[str, ...] = ()
    access_info_jump_host_names: tuple[str, ...] = ()
    if session.definition_candidate is not None:
        if session.definition_kind == "topology":
            topology_device_names = tuple((session.definition_candidate.get("devices") or {}).keys())
        elif session.definition_kind == "access_info":
            access_info_device_names = tuple((session.definition_candidate.get("devices") or {}).keys())
            access_info_jump_host_names = tuple((session.definition_candidate.get("jump_hosts") or {}).keys())
    log_device_ids = tuple(terminal.list_logged_device_ids())
    log_files_by_device = {
        device_id: tuple(filename for _, filename in terminal.list_device_logs(device_id))
        for device_id in log_device_ids
    }
    try:
        committed_active_reference_names = tuple(lab.get_active_reference_names(lab.read_settings(lab_root)))
    except lab.LabConfigError:
        committed_active_reference_names = ()

    topology_names_list = tuple(lab.list_topology_names(lab_root))
    access_info_names_list = tuple(lab.list_access_info_names(lab_root))
    scenario_names_list = tuple(lab.list_scenario_names(lab_root))
    reference_names_list = tuple(lab.list_reference_names(lab_root))

    no_topology_candidate_names = _no_definition_candidate_names(session, "topology", topology_names_list)
    no_access_info_candidate_names = _no_definition_candidate_names(session, "access_info", access_info_names_list)
    no_scenario_candidate_names = _no_definition_candidate_names(session, "scenario", scenario_names_list)
    no_reference_candidate_names = _no_definition_candidate_names(session, "reference", reference_names_list)

    running_topology_names = _running_selectable_names(session, "topology", topology_names_list)
    running_access_info_names = _running_selectable_names(session, "access_info", access_info_names_list)
    running_scenario_names = _running_selectable_names(session, "scenario", scenario_names_list)
    running_reference_names = _running_selectable_names(session, "reference", reference_names_list)

    monitor_terminal_device_ids = _monitor_terminal_target_names(session)

    return grammar.CliContext(
        topology_names=topology_names_list,
        scenario_names=scenario_names_list,
        reference_names=reference_names_list,
        access_info_names=access_info_names_list,
        candidate_reference_names=candidate_references,
        candidate_access_info_names=candidate_access_info_names,
        candidate_topology_names=candidate_topology_names,
        candidate_scenario_names=candidate_scenario_names,
        topology_candidate_device_names=topology_device_names,
        access_info_candidate_device_names=access_info_device_names,
        access_info_candidate_jump_host_names=access_info_jump_host_names,
        log_device_ids=log_device_ids,
        log_files_by_device=log_files_by_device,
        committed_active_reference_names=committed_active_reference_names,
        no_topology_candidate_names=no_topology_candidate_names,
        no_access_info_candidate_names=no_access_info_candidate_names,
        no_scenario_candidate_names=no_scenario_candidate_names,
        no_reference_candidate_names=no_reference_candidate_names,
        running_topology_names=running_topology_names,
        running_access_info_names=running_access_info_names,
        running_scenario_names=running_scenario_names,
        running_reference_names=running_reference_names,
        monitor_terminal_device_ids=monitor_terminal_device_ids,
    )


# A device's 'jump_host' field is spelled 'jump-host' as a CLI keyword and
# in every rendered view; every other field is spelled identically in YAML
# and in the CLI.
_FIELD_DISPLAY_NAMES = {"jump_host": "jump-host"}


def _display_field_name(field_name: str) -> str:
    return _FIELD_DISPLAY_NAMES.get(field_name, field_name)


def render_topology_block(data: dict) -> list[str]:
    """Safe logical topology only: no address/transport/port/username/password.

    `links` has no structured CLI editing command (topology mode has no
    `link <a> <a-if> <b> <b-if>` command -- it is only ever set by
    `discover topology` or the external `edit`), so it is rendered as
    read-only review information, not as a re-typeable command line, under
    its own `links` block. A device's optional per-interface L3 data
    (`ipv4_address`/`vrf`) is rendered the same way, one `interface <name>`
    block per interface -- also only ever populated by `discover topology`
    or the external `edit`, never a structured CLI command."""
    lines = [f"topology {data.get('name', '')}"]
    description = data.get("description")
    if description:
        lines.append(f" description {description}")
    for device_name, device in (data.get("devices") or {}).items():
        lines.append(f" device {device_name}")
        device_type = (device or {}).get("type")
        if device_type not in (None, ""):
            lines.append(f"  type {device_type}")
        for interface_name, interface_data in ((device or {}).get("interfaces") or {}).items():
            lines.append(f"  interface {interface_name}")
            ipv4_address = (interface_data or {}).get("ipv4_address")
            if ipv4_address not in (None, ""):
                lines.append(f"   ipv4_address {ipv4_address}")
            vrf = (interface_data or {}).get("vrf")
            if vrf not in (None, ""):
                lines.append(f"   vrf {vrf}")
            lines.append("  !")
        lines.append(" !")
    links = data.get("links") or []
    if links:
        lines.append(" links")
        for link in links:
            a = link.get("a", "?")
            b = link.get("b", "?")
            a_interface = link.get("a_interface")
            b_interface = link.get("b_interface")
            a_side = f"{a} {a_interface}" if a_interface else a
            b_side = f"{b} {b_interface}" if b_interface else b
            lines.append(f"  {a_side} <-> {b_side}")
        lines.append(" !")
    lines.append("!")
    return lines


def render_access_info_block(data: dict) -> list[str]:
    """Explicit local access-info rendering: passwords are shown in clear
    text here (this is a lab tool; see README.md "Password display
    policy") -- MCP tool results, logs, errors, `?`, completion, and
    history never go through this function and remain password-free."""
    lines = [f"access-info {data.get('name', '')}"]
    for jump_host_name, jump_host in (data.get("jump_hosts") or {}).items():
        lines.append(f" jump-host {jump_host_name}")
        for field_name in cfgmod.JUMP_HOST_FIELD_ORDER:
            value = (jump_host or {}).get(field_name)
            if value not in (None, ""):
                lines.append(f"  {_display_field_name(field_name)} {value}")
        lines.append(" !")
    for device_name, device in (data.get("devices") or {}).items():
        lines.append(f" device {device_name}")
        for field_name in cfgmod.DEVICE_FIELD_ORDER:
            value = (device or {}).get(field_name)
            if value not in (None, ""):
                lines.append(f"  {_display_field_name(field_name)} {value}")
        lines.append(" !")
    lines.append("!")
    return lines


def render_generic_definition(data: dict) -> str:
    """Scenario/reference: schema is intentionally not fixed yet (see
    docs/scenario_format.md), so the candidate mapping is shown as YAML."""
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True).rstrip("\n")


_NONE_DISPLAY = "<none>"


def _running_config_lines(settings: dict) -> str:
    """`show running-config` selection summary (EXEC/global/running --
    one shared renderer). The `access-info` and `reference`
    sections always appear, even when empty, showing the display-only
    `<none>` marker instead of being silently omitted -- this makes an
    optional selection's absence explicit rather than ambiguous (was it
    never rendered, or genuinely unset?). `<none>` is rendering only: it
    is never written to settings.yaml, never a valid selector value, and
    never returned through candidate-diff (`show configuration`) or MCP
    state -- see _running_config_delta_lines() below, unchanged.

    `topology`/`scenario` are mandatory selections and keep their
    pre-existing behavior unchanged (a section is only appended if a
    name is actually present) -- there is no `<none>`
    for them; a missing mandatory value is not a state this renderer
    tries to make presentable, it is left exactly as before."""
    descriptions = cfgmod.normalized_settings(settings).get(cfgmod.RUNNING_DESCRIPTIONS_KEY) or {}

    def entry_lines(kind: str, value: str) -> list[str]:
        lines = [f"  {value}"]
        description = (descriptions.get(kind) or {}).get(value)
        if description:
            lines.append(f"   description {description}")
        return lines

    sections: list[tuple[str, list[str]]] = []
    access_info_name = settings.get("active_access_info")
    sections.append(("access-info", entry_lines("access_info", access_info_name) if access_info_name else [f"  {_NONE_DISPLAY}"]))
    topology_name = settings.get("active_topology")
    if topology_name:
        sections.append(("topology", entry_lines("topology", topology_name)))
    scenario_name = settings.get("active_scenario")
    if scenario_name:
        sections.append(("scenario", entry_lines("scenario", scenario_name)))
    references = settings.get("active_references") or []
    reference_lines = [line for ref in references for line in entry_lines("reference", ref)]
    sections.append(("reference", reference_lines if references else [f"  {_NONE_DISPLAY}"]))
    lines: list[str] = []
    for label, body in sections:
        lines.append("!")
        lines.append(f" {label}")
        lines.extend(body)
    lines.append("!")
    return "\n".join(lines)


def render_committed_running_config(session: cfgmod.CliSession) -> str:
    """`show running-config` in EXEC/global/running mode: the committed MCP
    running-config selection -- never a definition's own content. Not used
    once a topology/access-info/scenario/reference (or its device/jump-host
    submode) is the current context; see render_committed_definition() for
    that."""
    return _running_config_lines(lab.read_settings(session.lab_root))


# Nested submodes ("device" under topology, "access_device"/
# "access_jump_host" under access-info) show only the one sub-object being
# edited, not every device/jump-host in the parent definition --
# context-local, like the definition-level modes.
_DEVICE_SUBMODES = ("device", "access_device")


def _scoped_to_current_device(data: Optional[dict], device_name: Optional[str]) -> Optional[dict]:
    """Limit whole-definition `data` to just `device_name`'s entry, or None
    if that device isn't present (e.g. a brand-new, never-committed
    device) -- the caller renders None as "no output"."""
    if data is None or device_name is None:
        return None
    devices = data.get("devices") or {}
    if device_name not in devices:
        return None
    scoped = dict(data)
    scoped["devices"] = {device_name: devices[device_name]}
    scoped.pop("jump_hosts", None)
    # links is a topology-level (not device-level) concept and any given
    # link necessarily names another device -- never another device's data
    # leaking into this one device's scoped view (see
    # render_topology_block()'s links section).
    scoped.pop("links", None)
    return scoped


def _scoped_to_jump_host(data: Optional[dict], jump_host_name: Optional[str]) -> Optional[dict]:
    """Same idea as _scoped_to_current_device(), for a jump-host submode."""
    if data is None or jump_host_name is None:
        return None
    jump_hosts = data.get("jump_hosts") or {}
    if jump_host_name not in jump_hosts:
        return None
    scoped = dict(data)
    scoped["jump_hosts"] = {jump_host_name: jump_hosts[jump_host_name]}
    scoped.pop("devices", None)
    return scoped


def _scope_to_session_context(session: cfgmod.CliSession, data: Optional[dict]) -> Optional[dict]:
    if session.mode in _DEVICE_SUBMODES:
        return _scoped_to_current_device(data, session.current_device_name)
    if session.mode == "access_jump_host":
        return _scoped_to_jump_host(data, session.current_jump_host_name)
    return data


def _render_definition_block(kind: Optional[str], data: Optional[dict]) -> str:
    if kind is None or data is None:
        return ""
    if kind == "topology":
        return "\n".join(render_topology_block(data))
    if kind == "access_info":
        return "\n".join(render_access_info_block(data))
    return render_generic_definition(data)  # scenario / reference


def render_committed_definition(session: cfgmod.CliSession) -> str:
    """`show running-config` while a topology/access-info/scenario/
    reference definition (or its device/jump-host submode) is the current
    context: that same object's *full* committed-on-disk state, re-read
    fresh -- never the in-memory candidate -- so it is empty for a
    definition (or sub-object) that has never been committed, and reflects
    a commit made moments ago in this same session."""
    data = cfgmod.load_committed_definition(session.definition_kind, session.definition_name, session.lab_root)
    data = _scope_to_session_context(session, data)
    return _render_definition_block(session.definition_kind, data)


# --------------------------------------------------------------------------
# `show configuration` / bare `show`: UNCOMMITTED CHANGES ONLY (a bounded,
# pragmatic delta -- not a full candidate dump, and not a generic minimal
# recursive diff engine). See docs/cli_reference.md "show configuration" for
# the rationale.
# --------------------------------------------------------------------------


def _field_delta_lines(original_obj: dict, candidate_obj: dict, field_order: tuple[str, ...]) -> list[str]:
    """Field-level delta for structured scalar configuration (access-info/
    jump-host/topology-device fields): only changed fields are rendered,
    each as a "field value" (set/changed) or "no field" (cleared) line --
    unchanged fields are never repeated."""
    lines = []
    for field_name in field_order:
        old_value = original_obj.get(field_name)
        new_value = candidate_obj.get(field_name)
        if old_value == new_value:
            continue
        display_name = _display_field_name(field_name)
        if new_value not in (None, ""):
            lines.append(f"  {display_name} {new_value}")
        else:
            lines.append(f"  no {display_name}")
    return lines


def _topology_device_interfaces_delta(original_interfaces: dict, candidate_interfaces: dict) -> list[str]:
    """Render only the interfaces (within one topology device) that
    actually changed, one nesting level deeper than the device's own
    field lines: a whole interface entry removed from the candidate (e.g.
    Discovery re-observed it as `unassigned`, or excluded it as a
    management address -- see discovery.py's own removal semantics) as
    '  no interface <name>'; an interface added or changed
    (ipv4_address/vrf) as its own '  interface <name>' / field lines /
    '  !' block. `interfaces` has no structured CLI editing command
    (same precedent as `links`), so this is read-only review information."""
    lines: list[str] = []
    for interface_name in original_interfaces:
        if interface_name not in candidate_interfaces:
            lines.append(f"  no interface {interface_name}")
    for interface_name, candidate_data in candidate_interfaces.items():
        original_data = original_interfaces.get(interface_name) or {}
        field_lines = _field_delta_lines(original_data, candidate_data or {}, ("ipv4_address", "vrf"))
        if field_lines:
            lines.append(f"  interface {interface_name}")
            lines.extend(f" {line}" for line in field_lines)
            lines.append("  !")
    return lines


def _topology_devices_delta(original_map: dict, candidate_map: dict) -> list[str]:
    """Like _named_objects_delta(), but topology-specific: a device block
    is rendered if its own `type` changed *or* if any of its interfaces'
    L3 data changed -- an L3-only change (device type unchanged) must
    still surface a device block, not disappear silently."""
    lines: list[str] = []
    for name in original_map:
        if name not in candidate_map:
            lines.append(f" no device {name}")
    for name, candidate_obj in candidate_map.items():
        original_obj = original_map.get(name) or {}
        field_lines = _field_delta_lines(original_obj, candidate_obj or {}, ("type",))
        interface_lines = _topology_device_interfaces_delta(
            (original_obj or {}).get("interfaces") or {}, (candidate_obj or {}).get("interfaces") or {}
        )
        if field_lines or interface_lines:
            lines.append(f" device {name}")
            lines.extend(field_lines)
            lines.extend(interface_lines)
            lines.append(" !")
    return lines


def _named_objects_delta(keyword: str, original_map: dict, candidate_map: dict, field_order: tuple[str, ...]) -> list[str]:
    """Render only the named objects (devices/jump-hosts) that actually
    changed: a whole object present in `original_map` but absent from
    `candidate_map` (removed via `no <keyword> <name>`) as a single
    ` no <keyword> <name>` line, in committed order; then, for every object
    still in `candidate_map`, its own ' <keyword> <name>' / field lines /
    ' !' block if any field actually differs -- a brand-new object (absent
    from original_map) is diffed against {}, so all of its populated
    fields show up as "new". A candidate-only object that nets out
    identical to committed (e.g. removed then recreated identically, or
    never existed and was removed again) contributes nothing, by
    construction -- neither loop below renders it."""
    lines: list[str] = []
    for name in original_map:
        if name not in candidate_map:
            lines.append(f" no {keyword} {name}")
    for name, candidate_obj in candidate_map.items():
        original_obj = original_map.get(name) or {}
        field_lines = _field_delta_lines(original_obj, candidate_obj or {}, field_order)
        if field_lines:
            lines.append(f" {keyword} {name}")
            lines.extend(field_lines)
            lines.append(" !")
    return lines


def render_access_info_configuration_delta(name: str, original: Optional[dict], candidate: dict) -> str:
    original = original or {}
    body = _named_objects_delta("jump-host", original.get("jump_hosts") or {}, candidate.get("jump_hosts") or {}, cfgmod.JUMP_HOST_FIELD_ORDER)
    body += _named_objects_delta("device", original.get("devices") or {}, candidate.get("devices") or {}, cfgmod.DEVICE_FIELD_ORDER)
    if not body:
        return ""
    return "\n".join([f"access-info {name}", *body, "!"])


_TOPOLOGY_TRACKED_KEYS = ("name", "description", "devices")


def render_topology_configuration_delta(name: str, original: Optional[dict], candidate: dict) -> str:
    original = original or {}
    other_keys = (set(candidate) | set(original)) - set(_TOPOLOGY_TRACKED_KEYS)
    if any(candidate.get(k) != original.get(k) for k in other_keys):
        # A field the structured CLI has no representation for changed
        # (e.g. 'links', or something an external editor added) -- fall
        # back to the whole candidate block so nothing is silently lost,
        # per the bounded/pragmatic delta policy for open structures.
        return "\n".join(render_topology_block(candidate))

    lines: list[str] = []
    old_description = original.get("description")
    new_description = candidate.get("description")
    if old_description != new_description:
        lines.append(f" description {new_description}" if new_description else " no description")
    lines += _topology_devices_delta(original.get("devices") or {}, candidate.get("devices") or {})
    if not lines:
        return ""
    return "\n".join([f"topology {name}", *lines, "!"])


def render_generic_configuration_delta(name: str, original: Optional[dict], candidate: dict) -> str:
    """Scenario/reference: open YAML with no structured CLI representation
    below the whole-document level, so the "changed object" is the whole
    (small) document -- an acceptable, bounded fallback, not a claim that
    every field in it changed."""
    if (original or {}) == candidate:
        return ""
    return render_generic_definition(candidate)


def _render_configuration_delta(kind: Optional[str], name: Optional[str], original: Optional[dict], candidate: Optional[dict]) -> str:
    if kind is None:
        return ""
    if candidate is None:
        # A whole definition is prospectively deleted (`no <kind> <name>`
        # -- see cfgmod.CliSession.remove_definition()). `original` is
        # always the real committed definition being removed here: a
        # brand-new, never-committed candidate is discarded outright by
        # remove_definition() instead of ever reaching this state, so
        # `original is None` alongside `candidate is None` never
        # represents a genuine deletion. Never render nested field-level
        # detail (and never the deleted definition's own content, which
        # for access-info may include plaintext credentials) -- only the
        # single whole-definition line. `kind.replace("_", "-")` recovers
        # the CLI keyword ("access_info" -> "access-info"; a no-op for
        # the other three kinds).
        if original is not None:
            return f"no {kind.replace('_', '-')} {name}"
        return ""
    if kind == "topology":
        return render_topology_configuration_delta(name, original, candidate)
    if kind == "access_info":
        return render_access_info_configuration_delta(name, original, candidate)
    return render_generic_configuration_delta(name, original, candidate)


def _running_config_delta_chunks(original: dict, candidate: dict) -> list[list[str]]:
    """Field-level delta for the running-config selection, as self-contained
    chunks: only the selection(s) that actually changed, using the existing
    set/`no` rendering conventions -- unrelated unchanged selections are
    never repeated. A chunk is either one running-level command line
    (`no access-info`, `no reference <name>`) or a `!`-delimited block that
    enters a name-less selection submode.

    Topology and scenario have no running-level `no`: clearing one is the
    submode's `no <name>`, so it renders as a block naming the COMMITTED
    selection (the only place the removed name still exists). Running-entry
    descriptions are compared on the normalized (active-entries-only) view,
    so a stale description never shows up."""
    original = cfgmod.normalized_settings(original)
    candidate = cfgmod.normalized_settings(candidate)
    old_desc = original.get(cfgmod.RUNNING_DESCRIPTIONS_KEY) or {}
    new_desc = candidate.get(cfgmod.RUNNING_DESCRIPTIONS_KEY) or {}

    def description_of(source: dict, kind: str, name: str) -> Optional[str]:
        return (source.get(kind) or {}).get(name)

    def entry_block(label: str, kind: str, names: list[str]) -> list[str]:
        return ["!", f" {label}", *(f"  {name}" for name in names), "!"]

    def named_entry(label: str, name: str, text: Optional[str]) -> list[str]:
        """Description edits are valid only in the named submode
        (`<kind> <name>` selects/enters it; `exit` returns to running, since a
        `!` there is a no-op)."""
        return [f"{label} {name}", f" description {text}" if text else " no description", " exit"]

    chunks: list[list[str]] = []
    for key, kind, label in (
        ("active_access_info", "access_info", "access-info"),
        ("active_topology", "topology", "topology"),
        ("active_scenario", "scenario", "scenario"),
    ):
        old_value = original.get(key)
        new_value = candidate.get(key)
        if old_value != new_value:
            if new_value:
                text = description_of(new_desc, kind, new_value)
                chunks.append(named_entry(label, new_value, text) if text else entry_block(label, kind, [new_value]))
            elif kind == "access_info":
                chunks.append([f"no {label}"])
            else:
                chunks.append(["!", f" {label}", f"  no {old_value}", "!"])
        elif new_value and description_of(old_desc, kind, new_value) != description_of(new_desc, kind, new_value):
            chunks.append(named_entry(label, new_value, description_of(new_desc, kind, new_value)))

    old_refs = original.get("active_references") or []
    new_refs = candidate.get("active_references") or []
    added_refs = [r for r in new_refs if r not in old_refs]
    removed_refs = [r for r in old_refs if r not in new_refs]
    changed_refs = [
        r for r in new_refs if r in old_refs and description_of(old_desc, "reference", r) != description_of(new_desc, "reference", r)
    ]
    plain: list[str] = []
    for reference in added_refs:
        text = description_of(new_desc, "reference", reference)
        if text:
            if plain:
                chunks.append(entry_block("reference", "reference", plain))
                plain = []
            chunks.append(named_entry("reference", reference, text))
        else:
            plain.append(reference)
    if plain:
        chunks.append(entry_block("reference", "reference", plain))
    for reference in changed_refs:
        chunks.append(named_entry("reference", reference, description_of(new_desc, "reference", reference)))
    for reference in removed_refs:
        chunks.append([f"no reference {reference}"])
    return chunks


def _running_config_delta_lines(original: dict, candidate: dict) -> list[str]:
    lines: list[str] = []
    previous: Optional[list[str]] = None
    for chunk in _running_config_delta_chunks(original, candidate):
        if previous is not None and previous[0] != "!" and chunk[0] == "!":
            # After a running-level command line the running section HAS
            # content, so a block's leading "!" separator would climb out of
            # running when pasted; it is purely visual, so omit it here.
            chunk = chunk[1:]
        lines.extend(chunk)
        previous = chunk
    return lines


def _common_selection_lines(mode: str, kind: str, original: dict, candidate: dict) -> list[str]:
    """`show configuration` in a name-less Common submode: ONLY this kind's
    selection delta, in the submode's own grammar (`<name>` selects, `no
    <name>` unselects; no `!`, no description, no other kind, no definition
    body), so it replays into a Fresh Session's same submode. Unset names
    come from the committed selection. A name that the submode's grammar
    reads as a keyword (e.g. a definition called `show`) cannot be typed as
    `<name>` here; it is rendered as a `%` line, which stops a paste instead
    of silently running that keyword."""
    original = cfgmod.normalized_settings(original)
    candidate = cfgmod.normalized_settings(candidate)

    def select_line(name: str) -> str:
        parsed = grammar.parse(mode, name)
        if parsed.ok and parsed.action == f"{mode}.select":
            return name
        return f"% {kind.replace('_', '-')} '{name}' collides with a submode keyword; select it from config-running"

    if kind == "reference":
        old_refs = original.get("active_references") or []
        new_refs = candidate.get("active_references") or []
        return [f"no {r}" for r in old_refs if r not in new_refs] + [
            select_line(r) for r in new_refs if r not in old_refs
        ]
    old_value = original.get(f"active_{kind}")
    new_value = candidate.get(f"active_{kind}")
    if old_value == new_value:
        return []
    return [select_line(new_value)] if new_value else [f"no {old_value}"]


def _global_running_section(original: dict, candidate: dict) -> list[str]:
    """Global-mode `show configuration` aggregates two independent candidate
    scopes. A top-level `no reference A` there means "delete the stored
    definition" (the global command), and `topology`/`scenario` blocks mean
    nothing at global level, so the WHOLE running-config selection delta is
    grouped under an explicit `running-config` section: pasted back at global
    it enters running mode, replays each chunk with running grammar, and
    returns to global before any following global-level line.

    The section is closed by `!` when the last chunk is a running-level
    command, but a `!` that follows a block's own closing `!` (or a named
    entry's `exit`) is a no-op in the structural-bang model (the section has
    no content of its own since), so a trailing block is followed by an explicit `exit` instead -- never
    leaving a later global line (e.g. a definition-deleting `no reference`)
    to run in running mode. Running-mode output is untouched."""
    chunks = _running_config_delta_chunks(original, candidate)
    if not chunks:
        return []
    body: list[str] = []
    for chunk in chunks:
        if chunk[0] == "!":
            # A block already carries its own nesting; its leading "!" is only
            # a visual separator (dropped), its closing "!" is nested one level.
            body.extend([*chunk[1:-1], " !"])
        else:
            body.extend(f" {line}" for line in chunk)
    last = chunks[-1][-1]
    return ["running-config", *body, "!" if last.startswith("no ") else "exit"]


def render_configuration_candidate(session: cfgmod.CliSession) -> str:
    """`show configuration` (and bare `show` in a configuration mode):
    **uncommitted changes only** in the current context -- never a full
    dump of the candidate. `running` mode shows the running-config
    selection delta; `global` mode aggregates that delta with the open
    definition's delta (each dirty scope rendered with its own type-aware
    delta renderer, not merged into one cross-definition diff); a
    definition mode (or its device/jump-host submode) shows only that
    object's delta, scoped to the current sub-object where applicable.
    Entering a new, still-empty object produces no output at all."""
    if session.mode in cfgmod.RUNNING_COMMON_MODES:
        return "\n".join(
            _common_selection_lines(
                session.mode,
                cfgmod.RUNNING_COMMON_MODES[session.mode],
                session.committed_settings or {},
                session.settings_candidate or {},
            )
        )

    if session.mode in _RUNNING_SCOPE_MODES:
        return "\n".join(_running_config_delta_lines(session.committed_settings or {}, session.settings_candidate or {}))

    if session.mode == "global":
        parts = []
        settings_lines = _global_running_section(session.committed_settings or {}, session.settings_candidate or {})
        if settings_lines:
            parts.append("\n".join(settings_lines))
        if session.definition_kind is not None:
            original = cfgmod.load_committed_definition(session.definition_kind, session.definition_name, session.lab_root)
            text = _render_configuration_delta(session.definition_kind, session.definition_name, original, session.definition_candidate)
            if text:
                parts.append(text)
        return "\n".join(parts)

    kind = session.definition_kind
    if kind is None:
        return ""
    original = cfgmod.load_committed_definition(kind, session.definition_name, session.lab_root)
    original = _scope_to_session_context(session, original)
    candidate = _scope_to_session_context(session, session.definition_candidate)
    return _render_configuration_delta(kind, session.definition_name, original, candidate)


def print_help_result(result: grammar.HelpResult) -> None:
    for line in result.lines:
        print(f"  {line.token:<20} {line.description}")
    if result.show_cr:
        print("  <cr>")


# Purely explanatory footer for the exact `config-running# no ?`
# help context (grammar.is_bare_no_context() gates it). access-info and
# reference are the ONLY real grammar candidates under running-config's
# "no" -- topology/scenario never gained "no topology"/"no scenario" (both
# are mandatory running-config selections, unset via "topology <name>" /
# "scenario <name>" instead, never by removal). This table only explains
# that existing shape in prose; it is not itself parsed, completed, or
# stored anywhere -- see the semantic-drift tests in
# tests/test_running_config_selection_model.py that assert the real
# grammar still matches every claim made here.
_RUNNING_CONFIG_SELECTION_MODEL = """\
Running-config selection model:
Type         Selection     Can be unset  Behavior
-----------  ------------  ------------  --------------------------------------------
access-info  Single        Yes           Without it, terminal access and discovery
                                          are unavailable
topology     Single        No            Required; use "topology <name>" to switch
scenario     Single        No            Required; use "scenario <name>" to switch
reference    Multiple      Yes           Use "no reference <name>" to remove one"""


def _render_running_config_selection_model() -> str:
    return _RUNNING_CONFIG_SELECTION_MODEL


def _should_show_running_no_footer(mode: str, text_before: str) -> bool:
    """Gate for the running-config-selection-model footer: only the spaced `no ?` context, only
    in running mode. Factored out of the `?` key-binding so it is testable
    without prompt_toolkit machinery."""
    return mode == "running" and grammar.is_bare_no_context(mode, text_before)


def print_parse_error(error: grammar.ParseError) -> None:
    if error.kind == "unknown":
        print(f'% Unknown command: "{error.token}"')
    elif error.kind == "ambiguous":
        print(f'% Ambiguous command: "{error.token}"')
    elif error.kind == "incomplete":
        print("% Incomplete command.")
    elif error.kind == "invalid":
        if error.span is not None:
            print(" " * error.span[0] + "^")
        print("% Invalid input detected at '^' marker.")
        if error.detail:
            print(f"% {error.detail}")


def _confirm(message: str) -> bool:
    try:
        answer = input(message)
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in ("y", "yes")


def _do_commit(session: cfgmod.CliSession) -> None:
    """`commit` only saves the candidate -- it never changes `mode`. IOS
    XR-style navigation (`root`/`exit`/`end`) is how you leave a mode; a
    successful or no-op commit leaves you exactly where you were, so a
    freshly-committed object's mode (e.g. a device just created and
    committed) stays reachable immediately afterward."""
    try:
        changed = session.commit()
    except cfgmod.CommitValidationError as exc:
        for error in exc.errors:
            print(f"% {error}")
        return
    except OSError as exc:
        # Persistence failure: never claim success, keep the session (and
        # its candidate) alive. Class name only -- no raw exception text.
        print(f"% Commit failed: could not write configuration ({type(exc).__name__}). Candidate kept; retry 'commit'.")
        return
    if not changed:
        print("No changes to commit.")
        return
    print("Commit complete.")
    _print_commit_warnings(session.last_commit_report)


def _print_commit_warnings(report: Optional[cfgmod.CommitReport]) -> None:
    """Warn about pre-existing, unrelated missing running-config references
    that this commit deliberately left untouched. Built only from the
    actual commit result (what was really written), and shows definition
    kinds/names only -- never definition content."""
    if report is None or not report.warnings:
        return
    noun = "definition" if len(report.warnings) == 1 else "definitions"
    print(f"\n% Warning: Running-config references missing {noun}:")
    for item in report.warnings:
        print(f"  {item}")
    print()
    if report.definition is not None:
        kind, name, action = report.definition
        verb = "removed" if action == "deleted" else "committed"
        print(f"  The {kind.replace('_', '-')} '{name}' was {verb} successfully.")
    if not report.settings_written:
        print("  The existing running-config was not modified.")


def _guarded_leave_configure(session: cfgmod.CliSession) -> None:
    if session.overall_dirty():
        print("% Uncommitted changes exist. Use 'commit' or 'clear'.")
        return
    session.reset_to_exec()


# --------------------------------------------------------------------------
# Command handlers -- dispatched by grammar action id
# --------------------------------------------------------------------------


def h_exec_configure(session: cfgmod.CliSession, args: dict) -> None:
    session.enter_configure()


def h_exec_exit(session: cfgmod.CliSession, args: dict) -> None:
    raise _ExitCli()


# `show running-config` means the committed MCP running-config selection
# only in these three modes; everywhere else it means the current
# topology/access-info/scenario/reference (or device submode) definition's
# own committed state instead (render_committed_definition()).
_MCP_SELECTION_MODES = ("exec", "global", *_RUNNING_SCOPE_MODES)


def h_show_running_config(session: cfgmod.CliSession, args: dict) -> None:
    if session.mode in _MCP_SELECTION_MODES:
        print(render_committed_running_config(session))
        return
    text = render_committed_definition(session)
    if text:
        print(text)


# --------------------------------------------------------------------------
# `show running-config <definition-type>` (EXEC only) -- a read-only
# dereference of one committed active-definition selection. Always reads
# committed running-config + the committed definition fresh from disk
# (never the in-memory candidate), and always reuses the same renderers
# definition mode's own `show running-config` uses -- there is no second
# rendering system.
# --------------------------------------------------------------------------


def h_show_running_config_access_info(session: cfgmod.CliSession, args: dict) -> None:
    settings = lab.read_settings(session.lab_root)
    name = lab.get_active_access_info_name(settings)
    if not name:
        print("% No active access-info is configured.")
        return
    data = lab.load_access_info(name, session.lab_root)
    print("\n".join(render_access_info_block(data)))


def h_show_running_config_topology(session: cfgmod.CliSession, args: dict) -> None:
    settings = lab.read_settings(session.lab_root)
    name = lab.get_active_topology_name(settings)
    data = lab.load_topology(name, session.lab_root)
    print("\n".join(render_topology_block(data)))


def h_show_running_config_scenario(session: cfgmod.CliSession, args: dict) -> None:
    settings = lab.read_settings(session.lab_root)
    name = lab.get_active_scenario_name(settings)
    data = lab.load_scenario(name, session.lab_root)
    print(render_generic_definition(data))


def h_show_running_config_reference(session: cfgmod.CliSession, args: dict) -> None:
    settings = lab.read_settings(session.lab_root)
    names = lab.get_active_reference_names(settings)
    if not names:
        print("% No active references are configured.")
        return
    # Fail-closed and atomic: load_references() loads every name in
    # committed order and raises immediately if any one fails, before any
    # reference block is ever rendered -- never a partial view.
    references = lab.load_references(names, session.lab_root)
    print("\n!\n".join(render_generic_definition(reference) for reference in references))


def h_show_running_config_reference_name(session: cfgmod.CliSession, args: dict) -> None:
    name = args["name"]
    settings = lab.read_settings(session.lab_root)
    active_names = lab.get_active_reference_names(settings)
    if name not in active_names:
        print(f"% Reference '{name}' is not active in running-config.")
        return
    data = lab.load_reference(name, session.lab_root)
    print(render_generic_definition(data))


def h_show_configuration(session: cfgmod.CliSession, args: dict) -> None:
    text = render_configuration_candidate(session)
    if text:
        print(text)


def _run_git(*args: str) -> str:
    """Shared best-effort `git` invocation for `show version`'s checkout
    metadata. Never raises and never surfaces a raw git error: missing
    `git`, a non-repository checkout, or any other failure all collapse to
    "unavailable"."""
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    if result.returncode != 0:
        return "unavailable"
    return result.stdout.strip() or "unavailable"


def _resolve_git_commit() -> str:
    """Best-effort short commit hash for the checkout `show version` is
    running from."""
    return _run_git("rev-parse", "--short", "HEAD")


def _resolve_git_branch() -> str:
    """Best-effort current branch name for the same checkout. Detached
    HEAD (e.g. mid-rebase, or a CI checkout of a bare commit) prints the
    literal "HEAD" -- a true, non-raising answer -- rather than being
    collapsed into "unavailable", which is reserved for "no git" or "not a
    repository" instead."""
    return _run_git("rev-parse", "--abbrev-ref", "HEAD")


def render_version_info() -> str:
    """Read-only software information: no candidate/dirty-state change, no
    access-info, no dependency on the active topology/scenario/reference."""
    return "\n".join(
        [
            "Network Lab MCP",
            "",
            f"  Version:       {network_lab_mcp.__version__}",
            f"  Release date:  {network_lab_mcp.__release_date__}",
            f"  Git commit:    {_resolve_git_commit()}",
            f"  Git branch:    {_resolve_git_branch()}",
            f"  Author:        {network_lab_mcp.__author__}",
            f"  License:       {network_lab_mcp.__license__}",
            f"  Python:        {platform.python_version()}",
            f"  Platform:      {platform.system()}",
            f"  MCP SDK:       {network_lab_mcp.__mcp_sdk_version__}",
        ]
    )


def render_quick_start() -> str:
    """Network Lab MCP's own Quick Start/usage help -- distinct from the
    IOS XR-style `?` context-sensitive syntax help, which is unaffected by
    this and lives entirely in the "?" key binding below."""
    return "\n".join(
        [
            "Network Lab MCP CLI",
            "",
            "Purpose:",
            "  Configure the lab definitions and settings used by Network Lab MCP.",
            "",
            "Typical workflow:",
            "  1. Configure private device access information (access-info).",
            "  2. Create or edit a topology.",
            "  3. Create or edit a scenario.",
            "  4. Create or edit references.",
            "  5. Select access-info/topology/scenario/references in running-config.",
            "  6. Commit the configuration.",
            "  7. Use Claude Code with Network Lab MCP.",
            "",
            "Configuration areas:",
            "  running-config   Select topology, scenario, and references used by MCP",
            "  access-info      Configure private device access information",
            "  topology         Create or edit the logical topology",
            "  scenario         Create or edit the task scenario",
            "  reference        Create or edit reusable knowledge",
            "",
            "Editing:",
            "  Topology, scenario, and reference definitions support external YAML editing.",
            "  Editor selection: $VISUAL -> $EDITOR -> vim",
            "",
            "CLI help:",
            "  Press '?' at any prompt for context-sensitive command help.",
            "",
            "Additional help:",
            "  help claude",
            "  help workflow",
            "  help editor",
            "  help cli",
        ]
    )


def render_help_claude() -> str:
    return "\n".join(
        [
            "Claude Code Integration",
            "",
            "Network Lab MCP exposes exactly seven MCP tools to Claude Code:",
            "  get_active_topology, get_execution_instructions, terminal_open,",
            "  terminal_send, terminal_read, terminal_list, terminal_close.",
            "",
            "Claude receives:",
            "  - the committed active topology (safe logical data only)",
            "  - the committed active scenario",
            "  - the committed active references",
            "",
            "Claude never receives:",
            "  - device passwords, usernames, or addresses",
            "  - any access-info definition, including jump-host details",
            "  - uncommitted candidate configuration",
            "",
            "Claude addresses a device by its logical device ID only (e.g. \"R1\").",
            "Network Lab MCP resolves the private connection details internally --",
            "including an optional single-hop SSH jump host, entirely inside",
            "terminal_open() -- and opens the terminal session; Claude never sees",
            "any of that resolution.",
            "",
            "Use running-config (see 'help workflow') to select which committed",
            "access-info/topology/scenario/references MCP uses and exposes to Claude.",
            "",
            "Getting started with Claude Code:",
            "  1. Install Network Lab MCP into an activated Python environment",
            "     (see README.md, \"Quick Start\").",
            "  2. Register it once, using the absolute path of the installed executable:",
            "       claude mcp add network-lab -s user -- /path/to/network-lab-mcp/.venv/bin/network-lab-mcp",
            "  3. Start Claude Code from your own task workspace (not this",
            "     repository) once Network Lab MCP has been configured and",
            "     committed.",
        ]
    )


def render_help_workflow() -> str:
    return "\n".join(
        [
            "Typical Workflow",
            "",
            "1. Configure access-info (device credentials, optional jump hosts).",
            "2. Create or edit topology.",
            "3. Create or edit scenario.",
            "4. Create or edit references.",
            "5. Select access-info/topology/scenario/references in running-config.",
            "6. Review uncommitted changes (show / show configuration).",
            "7. Commit.",
            "8. Use Claude Code.",
            "",
            "Definitions:",
            "  access-info     Private device connection information",
            "  topology        Safe logical network model",
            "  scenario        Task intent",
            "  reference       Reusable knowledge",
            "  running-config  Definitions currently used by MCP",
            "",
            "Topology can be created/edited with structured CLI commands, an",
            "external YAML editor ('edit'), or 'discover topology' (LLDP/CDP",
            "neighbor discovery, in global configuration mode) -- all three",
            "produce a topology candidate the same way; nothing is committed",
            "until 'commit'.",
        ]
    )


def render_help_editor() -> str:
    return "\n".join(
        [
            "External YAML Editor",
            "",
            "Available for topology, scenario, and reference definitions (not",
            "access-info or jump hosts, which stay on the structured CLI so",
            "credentials are never written to an external editor's temp file).",
            "",
            "Editor resolution order:",
            "  1. $VISUAL",
            "  2. $EDITOR",
            "  3. vim (fallback; no .vimrc required -- Network Lab MCP enables",
            "     basic YAML syntax highlighting itself only for this fallback,",
            "     never when you set $VISUAL/$EDITOR yourself)",
            "",
            "'edit' opens the current candidate in a secure, uniquely named",
            "temporary .yaml file. Saving and quitting the editor only updates",
            "the in-memory candidate; the real committed file is not changed",
            "until 'commit'. 'clear' discards the edit (and any other",
            "uncommitted change in the current configure session) without",
            "touching disk.",
        ]
    )


def render_help_cli() -> str:
    return "\n".join(
        [
            "CLI Usage",
            "",
            "  ?                     Context-sensitive help for the current position",
            "  Tab / Ctrl-I          Complete the current token",
            "  configure             Enter configuration mode",
            "  show                  Same as 'show configuration' (bare show)",
            "  show configuration    Uncommitted changes in the current context",
            "  show running-config   Committed state in the current context",
            "  commit                Save the candidate; stays in the current mode",
            "  root                  Jump to global configuration mode (candidate kept)",
            "  exit                  Move one configuration level up (candidate kept)",
            "  end                   Return to EXEC (blocked while uncommitted changes exist)",
            "  clear                 Discard uncommitted configure-session changes",
            "  Ctrl-C                Cancel the current input line only",
            "  show logging          (EXEC) List/view persistent terminal session logs",
            "",
            "Pasting a multi-line configuration block is supported: each line runs in",
            "order as if typed manually, stopping at the first invalid line.",
            "",
            "'commit' never leaves the mode you were in -- use 'root'/'exit'/'end' to",
            "navigate. 'show running-config' means the committed MCP running-config",
            "selection in EXEC/global/running mode, and the current object's own",
            "committed state everywhere else.",
            "",
            "See docs/cli_reference.md for the full command reference.",
        ]
    )


_HELP_TOPIC_RENDERERS: dict[str, Callable[[], str]] = {
    "claude": render_help_claude,
    "workflow": render_help_workflow,
    "editor": render_help_editor,
    "cli": render_help_cli,
}


def h_help(session: cfgmod.CliSession, args: dict) -> None:
    print(render_quick_start())


def h_help_topic(session: cfgmod.CliSession, args: dict) -> None:
    print(_HELP_TOPIC_RENDERERS[args["topic"]]())


def h_show_version(session: cfgmod.CliSession, args: dict) -> None:
    print(render_version_info())


# --------------------------------------------------------------------------
# `terminal monitor <device-id>` (EXEC only) -- a live,
# read-only human view of whichever Network Lab MCP terminal session
# currently has priority for a device (managed > discovery > waiting; see
# terminal.capture_device_terminal_view()'s own docstring). Passive
# observation only: it never sends anything to a pane and never creates/
# closes a session, and it deliberately does not take the per-device
# lock (observation is a plain read, and the lock is process-local
# anyway).
#
# The UI is `full_screen=False` (never the alternate screen
# buffer), so terminal activity already streamed to the human stays in
# the *normal* terminal emulator scrollback -- it is printed once via
# run_in_terminal() (the same "print permanently above a live area"
# mechanism the `?`/Tab key bindings already use elsewhere in this file)
# and never repainted. Only a small 3-line status block at the bottom
# (`Monitoring terminal <device> | Read-only | Source: ... | Status: ...
# | q: quit`, width-adaptive) is live/redrawn, via prompt_toolkit's own
# `renderer.erase()` on exit -- which erases only that live area, not the
# scrollback above it, so quitting cleanly restores `network-lab#`
# without touching anything already printed. Monitor lifetime remains
# independent of session lifetime: it survives WAITING/ENDED and source
# switches alike, resuming live output automatically -- only an explicit
# q/Q/Ctrl-C ends it.
# --------------------------------------------------------------------------

# Human-observation cadence: frequent enough to feel live, far below a
# busy loop (a handful of tmux subprocess calls per second at most, only
# while a human actually has a monitor open).
_MONITOR_REFRESH_INTERVAL = 0.3

# How much recent context to print once when a source first becomes
# active -- deliberately bounded (matches the
# existing "visible pane" convention, terminal.DEFAULT_READ_LINES), never
# the full multi-thousand-line tmux history.
_MONITOR_INITIAL_CONTEXT_LINES = terminal.DEFAULT_READ_LINES


@dataclass
class _MonitorStreamCursor:
    """Per-monitor-run incremental-output bookkeeping. Pure UI
    state, not a terminal.py concept: reset (a "new instance") whenever
    the observed source changes, or a same-named source's captured
    history no longer has the previously-seen prefix as its own prefix --
    the same-name case is exactly what happens when a session is closed
    and a new one is created under the identical name (a fresh tmux pane
    never carries over the old pane's history), so this needs no extra
    tmux identity query (pane_id/session_id): the content-prefix check
    already implies it. The one honestly-documented edge case this cannot
    distinguish from a real recreation is history-limit (20000-line)
    eviction truncating the same session's own history -- both are
    treated identically (a "resumed" instance, bounded fresh context),
    which never loses correctness for the human observer (everything
    already emitted already reached their terminal), only occasionally
    reprints a small bounded amount of already-seen tail content."""

    source: str = "none"
    lines_shown: int = 0
    last_full_lines: list[str] = field(default_factory=list)


def _monitor_stream_step(device_id: str, cursor: _MonitorStreamCursor) -> tuple[list[str], terminal.TerminalMonitorSnapshot]:
    """One observe-and-diff step: returns (new permanent lines to print,
    latest snapshot). Deliberately separate from the async poll loop
    below so every lifecycle/incremental-output case is directly
    unit-testable without any real time passing or prompt_toolkit
    machinery. Mutates `cursor` in place; never touches
    tmux beyond the one read-only terminal.capture_device_terminal_view()
    call (full history, so burst output between polls -- up to the
    20000-line history-limit -- is never lost merely because it scrolled
    off the visible pane)."""
    snapshot = terminal.capture_device_terminal_view(device_id, lines=terminal.HISTORY_LIMIT)
    printable: list[str] = []

    if snapshot.status == "waiting":
        if cursor.source != "none":
            printable.append(f"[monitor] {cursor.source} terminal activity ended; waiting")
        cursor.source = "none"
        cursor.lines_shown = 0
        cursor.last_full_lines = []
        return printable, snapshot

    full_lines = snapshot.pane_text.split("\n") if snapshot.pane_text else []
    is_new_instance = (
        snapshot.source != cursor.source
        or full_lines[: cursor.lines_shown] != cursor.last_full_lines[: cursor.lines_shown]
    )

    if is_new_instance:
        if cursor.source == "none":
            printable.append(f"[monitor] {snapshot.source} terminal activity started")
        elif snapshot.source != cursor.source:
            printable.append(f"[monitor] switched to {snapshot.source} terminal activity")
        else:
            printable.append(f"[monitor] {snapshot.source} terminal activity resumed")
        start = max(0, len(full_lines) - _MONITOR_INITIAL_CONTEXT_LINES)
        printable.extend(full_lines[start:])
    elif len(full_lines) > cursor.lines_shown:
        printable.extend(full_lines[cursor.lines_shown :])
    # else: unchanged poll -- append nothing.

    cursor.source = snapshot.source
    cursor.lines_shown = len(full_lines)
    cursor.last_full_lines = full_lines
    return printable, snapshot


def _monitor_status_line(device_id: str, snapshot: terminal.TerminalMonitorSnapshot) -> str:
    parts = [f"Monitoring terminal {device_id}", "Read-only"]
    if snapshot.status == "waiting":
        parts.append("Status: waiting for terminal activity")
    else:
        parts.append(f"Source: {snapshot.source}")
        parts.append(f"Status: {snapshot.status}")
    parts.append("q: quit")
    return " | ".join(parts)


def _monitor_status_block(device_id: str, snapshot: terminal.TerminalMonitorSnapshot) -> str:
    """The small 3-line live status area (separator/content/separator),
    width-adaptive -- never touches the managed/
    Discovery tmux pane's own geometry, only this local rendering."""
    width = shutil.get_terminal_size(fallback=(80, 24)).columns
    separator = "-" * width
    line = _monitor_status_line(device_id, snapshot)
    if len(line) > width:
        line = line[: max(0, width - 1)]
    return f"{separator}\n{line}\n{separator}"


async def _monitor_poll_loop(
    device_id: str, cursor: _MonitorStreamCursor, status_holder: list[str], refresh_interval: float, app: Application
) -> None:
    """Background task: polls, prints any new permanent
    activity via run_in_terminal() (the same mechanism the `?`/Tab key
    bindings already use to print above a live prompt_toolkit area), then
    updates the live status text and invalidates the display. Polls
    immediately on start (so the very first frame already reflects real
    state), then on `refresh_interval`. Automatically cancelled when the
    Application exits (prompt_toolkit's own create_background_task()
    contract) -- no manual stop flag needed."""
    while True:
        printable, snapshot = _monitor_stream_step(device_id, cursor)
        if printable:
            text = "\n".join(printable)
            await run_in_terminal(lambda text=text: print(text))
        status_holder[0] = _monitor_status_block(device_id, snapshot)
        app.invalidate()
        await asyncio.sleep(refresh_interval)


def _build_monitor_application(device_id: str, refresh_interval: float) -> Application:
    """Construct (but do not run) the monitor's Application -- separated
    from run_terminal_monitor() so tests can inspect its key bindings
    without entering the blocking event loop.

    `full_screen=False`: never the alternate screen buffer,
    so the terminal emulator's normal scrollback -- including whatever
    this run prints via _monitor_poll_loop()'s run_in_terminal() calls --
    is preserved after exit."""
    kb = KeyBindings()

    @kb.add("q")
    @kb.add("Q")
    @kb.add("c-c")
    def _(event) -> None:
        # Exits only this Application (event.app), never the surrounding
        # CLI process/PromptSession -- see run_terminal_monitor().
        event.app.exit()

    status_holder = ["Monitoring terminal {}\nRead-only".format(device_id)]
    control = FormattedTextControl(text=lambda: status_holder[0])
    app = Application(
        layout=Layout(Window(content=control, height=3)),
        key_bindings=kb,
        full_screen=False,
    )
    cursor = _MonitorStreamCursor()
    app.pre_run_callables.append(
        lambda: app.create_background_task(
            _monitor_poll_loop(device_id, cursor, status_holder, refresh_interval, app)
        )
    )
    return app


def run_terminal_monitor(device_id: str, *, refresh_interval: float = _MONITOR_REFRESH_INTERVAL) -> None:
    """Blocking: runs the monitor until the user quits it (q/Q/Ctrl-C).
    Every other keystroke is simply unbound -- this Application's only
    control is a non-editable FormattedTextControl, so there is no text
    buffer for a stray key to be inserted into, and nothing here ever
    forwards a keystroke to tmux. Exiting erases only the live status
    area (prompt_toolkit's own renderer.erase() on exit) -- already
    streamed activity, printed as normal terminal output, is untouched --
    and adds nothing to CLI command history -- this loop never touches
    PromptSession/history at all."""
    _build_monitor_application(device_id, refresh_interval).run()


def h_monitor_terminal(session: cfgmod.CliSession, args: dict) -> None:
    _require_interactive(args)
    device_id = args["device_id"]
    if device_id not in _monitor_terminal_target_names(session):
        raise terminal.TerminalError(
            f"Device '{device_id}' is not in the active topology and has no existing terminal session."
        )
    run_terminal_monitor(device_id)


# --------------------------------------------------------------------------
# `show logging` (EXEC only) -- read-only terminal transcript log listing.
# --------------------------------------------------------------------------


def _render_log_table(columns: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    if not rows:
        return "No terminal logs found."
    widths = [
        max(len(columns[i]), max((len(row[i]) for row in rows), default=0)) for i in range(len(columns))
    ]
    lines = [
        "  ".join(columns[i].ljust(widths[i]) for i in range(len(columns))),
        "  ".join("-" * widths[i] for i in range(len(columns))),
    ]
    for row in rows:
        lines.append("  ".join(row[i].ljust(widths[i]) for i in range(len(columns))))
    return "\n".join(lines)


def _format_session_start(started) -> str:
    return started.strftime("%Y-%m-%d %H:%M:%S")


def _render_log_summary_table(counts: list[tuple[str, int]]) -> str:
    """`show logging summary`: one row per valid
    device logging directory with its eligible log-file count -- an
    empty directory (e.g. after `delete logging <device> all`) is still
    shown, with 0, since it remains a meaningful `delete logging <device>
    directory` target -- plus a Total row (sum of eligible logs only,
    never a directory count) separated by its own dashed rule. Distinct
    from _render_log_table() (a flat per-file listing), which
    `show logging <device>` still uses unchanged."""
    columns = ("Device", "Log Files")
    body = [(device, str(count)) for device, count in counts]
    footer = ("Total", str(sum(count for _device, count in counts)))
    widths = [max(len(columns[i]), max(len(row[i]) for row in body + [footer])) for i in range(len(columns))]
    separator = "  ".join("-" * widths[i] for i in range(len(columns)))
    lines = ["  ".join(columns[i].ljust(widths[i]) for i in range(len(columns))), separator]
    lines.extend("  ".join(row[i].ljust(widths[i]) for i in range(len(columns))) for row in body)
    lines.append(separator)
    lines.append("  ".join(footer[i].ljust(widths[i]) for i in range(len(columns))))
    return "\n".join(lines)


def h_show_logging(session: cfgmod.CliSession, args: dict) -> None:
    """Bare `show logging`: a flat listing of every device's
    persistent log files, newest first. The per-device eligible-log-count
    table is available explicitly as `show logging summary`."""
    rows = [
        (device_id, _format_session_start(started), filename)
        for device_id in sorted(terminal.list_logged_device_ids())
        for started, filename in terminal.list_device_logs(device_id)
    ]
    rows.sort(key=lambda row: row[1], reverse=True)
    print(_render_log_table(("Device", "Session Start", "Log File"), rows))


def h_show_logging_summary(session: cfgmod.CliSession, args: dict) -> None:
    device_ids = sorted(terminal.list_logged_device_ids())
    if not device_ids:
        print("No terminal logs found.")
        return
    counts = [(device_id, len(terminal.list_device_logs(device_id))) for device_id in device_ids]
    print(_render_log_summary_table(counts))


def h_show_logging_device(session: cfgmod.CliSession, args: dict) -> None:
    device_id = args["device_id"]
    rows = [(_format_session_start(started), filename) for started, filename in terminal.list_device_logs(device_id)]
    if not rows:
        print(f"No terminal logs found for device '{device_id}'.")
        return
    print(_render_log_table(("Session Start", "Log File"), rows))


def h_show_logging_device_file(session: cfgmod.CliSession, args: dict) -> None:
    content = terminal.read_device_log(args["device_id"], args["log_file"])
    if content:
        print(content, end="" if content.endswith("\n") else "\n")


# ---- delete logging (EXEC only) ----
#
# terminal.py owns enumeration/preflight/manifest/deletion (DeletionPlan,
# build_*_deletion_plan(), apply_deletion_plan()) and never reads
# interactive input; this module owns the [y/N] confirmation and message
# text only. terminal.TerminalError is
# already caught generically by execute_command_line() and printed as
# "% <message>", so a preflight rejection needs no handling here.


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _read_confirmation_line() -> str:
    """Thin wrapper around input() (no prompt argument -- the prompt is
    always printed separately by _confirm_delete(), so tests can observe
    it via captured stdout regardless of how the answer itself is
    supplied). Isolated so tests can supply a deterministic answer
    without a real interactive stdin, and so Ctrl-C/EOF handling lives in
    exactly one place. Uses plain input(), not prompt_toolkit's own
    PromptSession/history, so a confirmation answer is never inserted
    into command history and never confused with a pasted physical line
    (see h_delete_logging_*'s _require_interactive() check, which runs
    before this is ever reached)."""
    return input()


def _confirm_delete(message: str) -> bool:
    """[y/N] confirmation for one destructive delete logging operation.
    Enter alone (or any of n/N) is No -- never destructive by omission.
    Anything else that isn't y/Y/n/N re-prompts instead of being silently
    interpreted either way. Ctrl-C/EOF cancel safely, matching the
    pre-existing topology case-collision confirmation's own convention
    (see _confirm() above) without touching that separate helper."""
    prompt = f"{message} [y/N]: "
    while True:
        print(prompt, end="", flush=True)
        try:
            answer = _read_confirmation_line().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if answer in ("y", "Y"):
            return True
        if answer in ("n", "N", ""):
            return False
        print("Please enter y or n.")


def _require_interactive(args: dict) -> None:
    """`delete logging` requires a real [y/N] answer, which a multi-line
    paste can never safely supply (see execute_input_block()'s
    `interactive` flag) -- fail closed instead of either blocking the
    paste on stdin or silently treating the next pasted line as the
    answer."""
    if not args.get("_interactive", True):
        raise terminal.TerminalError(
            "delete logging requires interactive confirmation and cannot be run from multi-line paste."
        )


def _confirm_and_apply(initial_plan, message: str, build_plan) -> "terminal.DeletionPlan | None":
    """Shared confirm-then-re-preflight-then-apply flow for every
    destructive delete logging command.
    `initial_plan` is what was already built (and used to compute
    `message`); `build_plan` is called again only after the user
    confirms, to catch anything that changed while they were deciding
    (a new log, a newly active writer, new directory contents) -- if the
    freshly-built plan differs at all from `initial_plan`, nothing is
    deleted and the caller is told to retry. Returns the applied plan, or
    None if the user declined or the target state changed underneath
    them (either way, a message has already been printed)."""
    if not _confirm_delete(message):
        print("Delete cancelled.")
        return None
    reconfirmed_plan = build_plan()
    if reconfirmed_plan != initial_plan:
        print("% Logging state changed while waiting for confirmation.")
        print("% No logs were deleted. Retry the command.")
        return None
    terminal.apply_deletion_plan(reconfirmed_plan)
    return reconfirmed_plan


def h_delete_logging_device_file(session: cfgmod.CliSession, args: dict) -> None:
    _require_interactive(args)
    device_id = args["device_id"]
    log_file = args["log_file"]
    build_plan = lambda: terminal.build_file_deletion_plan(device_id, log_file)
    plan = build_plan()
    message = f"Delete terminal log {device_id}/{log_file}?"
    if _confirm_and_apply(plan, message, build_plan) is not None:
        print(f"Deleted terminal log {device_id}/{log_file}.")


def h_delete_logging_device_all(session: cfgmod.CliSession, args: dict) -> None:
    _require_interactive(args)
    device_id = args["device_id"]
    build_plan = lambda: terminal.build_device_all_deletion_plan(device_id)
    plan = build_plan()
    count = len(plan.files)
    message = f"Delete all {count} terminal {_plural(count, 'log', 'logs')} for {device_id}?"
    if _confirm_and_apply(plan, message, build_plan) is not None:
        print(f"Deleted {count} terminal {_plural(count, 'log', 'logs')} for {device_id}.")


def h_delete_logging_device_directory(session: cfgmod.CliSession, args: dict) -> None:
    _require_interactive(args)
    device_id = args["device_id"]
    build_plan = lambda: terminal.build_device_directory_deletion_plan(device_id)
    plan = build_plan()
    count = len(plan.files)
    if count == 0:
        message = f"Delete empty logging directory for {device_id}?"
        success = f"Deleted logging directory for {device_id}."
    else:
        message = (
            f"Delete all {count} terminal {_plural(count, 'log', 'logs')} "
            f"and logging directory for {device_id}?"
        )
        success = (
            f"Deleted {count} terminal {_plural(count, 'log', 'logs')} "
            f"and logging directory for {device_id}."
        )
    if _confirm_and_apply(plan, message, build_plan) is not None:
        print(success)


def h_delete_logging_all(session: cfgmod.CliSession, args: dict) -> None:
    _require_interactive(args)
    build_plan = terminal.build_global_all_deletion_plan
    plan = build_plan()
    count = len(plan.files)
    message = f"Delete all {count} terminal {_plural(count, 'log', 'logs')}?"
    if _confirm_and_apply(plan, message, build_plan) is not None:
        print(f"Deleted {count} terminal {_plural(count, 'log', 'logs')}.")


def h_delete_logging_all_directory(session: cfgmod.CliSession, args: dict) -> None:
    _require_interactive(args)
    build_plan = terminal.build_global_directory_deletion_plan
    plan = build_plan()
    file_count = len(plan.files)
    dir_count = len(plan.directories)
    message = (
        f"Delete all {file_count} terminal {_plural(file_count, 'log', 'logs')} and "
        f"{dir_count} device log {_plural(dir_count, 'directory', 'directories')}?"
    )
    if _confirm_and_apply(plan, message, build_plan) is not None:
        print(
            f"Deleted {file_count} terminal {_plural(file_count, 'log', 'logs')} and "
            f"{dir_count} device log {_plural(dir_count, 'directory', 'directories')}."
        )


def h_commit(session: cfgmod.CliSession, args: dict) -> None:
    _do_commit(session)


def h_clear(session: cfgmod.CliSession, args: dict) -> None:
    session.clear()


def h_end(session: cfgmod.CliSession, args: dict) -> None:
    _guarded_leave_configure(session)


def h_root(session: cfgmod.CliSession, args: dict) -> None:
    """`root`: jump straight to global configuration mode from any nested
    submode, preserving candidate state -- never commits, never clears."""
    session.go_to_global()


# `exit` moves exactly one configuration level up, per cfgmod._EXIT_PARENT_MODE
# (the grammar/config single source of truth for the mode hierarchy -- no
# separate per-mode table to keep in sync here). A leaf submode's own
# current-object attribute is cleared on the way out.
_LEAF_MODE_CURRENT_ATTR = {
    "device": "current_device_name",
    "access_device": "current_device_name",
    "access_jump_host": "current_jump_host_name",
    **{mode: "current_running_entry_name" for mode in cfgmod.RUNNING_ENTRY_MODES},
}


def h_exit(session: cfgmod.CliSession, args: dict) -> None:
    attr = _LEAF_MODE_CURRENT_ATTR.get(session.mode)
    if attr is not None:
        setattr(session, attr, None)
    session.mode = cfgmod._EXIT_PARENT_MODE[session.mode]


def h_edit(session: cfgmod.CliSession, args: dict) -> None:
    try:
        result = editor.edit_yaml_candidate(
            session.definition_candidate, topology=session.definition_kind == "topology"
        )
    except editor.EditorError as exc:
        print(f"% {exc}")
        return
    session.replace_definition_candidate(result)


# ---- global configuration mode ----


def h_global_running_config(session: cfgmod.CliSession, args: dict) -> None:
    session.mode = "running"


def _group_unresolved_neighbors(unresolved: list) -> dict:
    grouped: dict = {}
    for obs in unresolved:
        grouped.setdefault(obs.remote_device_id_raw, []).append(obs)
    return grouped


def render_discovery_summary(result: "discovery.DiscoveryResult") -> str:
    grouped_unresolved = _group_unresolved_neighbors(result.unresolved)
    lines = [
        "Discovery complete.",
        "",
        f"  Access-info:          {result.access_info_name}",
        f"  IOS XR targets:       {result.iosxr_target_count}",
        f"  IOS XE targets:       {result.iosxe_target_count}",
        f"  IOS targets:          {result.ios_target_count}",
        f"  Connected:            {result.connected_count}",
        f"  LLDP observations:    {result.observation_count}",
        f"  CDP observations:     {result.cdp_observation_count}",
        f"  Managed links:        {len(result.managed_links)}",
        f"  Unresolved neighbors: {len(grouped_unresolved)}",
    ]
    if result.include_l3:
        lines.append(
            f"  L3 enrichment:        {result.l3_enriched_device_count}/{result.connected_count} devices, "
            f"{result.l3_interface_count} interfaces"
        )
    else:
        lines.append("  L3 enrichment:        Disabled")
        lines.append("  Previous L3 data:     Not carried over")
    lines.append(f"  Topology candidate:   {result.default_topology_name}")
    if result.protocol_status:
        lines.append("")
        lines.append("Neighbor discovery protocol status:")
        for label, devices in result.protocol_status.items():
            count = f"{len(devices)} device" + ("" if len(devices) == 1 else "s")
            lines.append(f"  {label + ':':<22}{count} ({', '.join(devices)})")
    if result.protocol_warnings:
        lines.append("")
        lines.append(f"Neighbor discovery warnings ({len(result.protocol_warnings)}):")
        for warning in result.protocol_warnings:
            lines.append(f"  {warning}")
    if result.l3_warnings:
        lines.append("")
        lines.append(f"L3 enrichment warnings ({len(result.l3_warnings)}):")
        for warning in result.l3_warnings:
            lines.append(f"  {warning}")
    if result.conflicts:
        lines.append("")
        lines.append(f"Protocol/link reconciliation conflicts ({len(result.conflicts)}, not added):")
        for conflict in result.conflicts:
            a_dev, a_intf = conflict.endpoint_a
            b_dev, b_intf = conflict.endpoint_b
            if conflict.endpoint_a == conflict.endpoint_b:
                lines.append(
                    f"  {a_dev} {a_intf}: conflicting neighbor observations "
                    f"({conflict.observation_a.source} -> {conflict.observation_a.remote_device_id_raw} "
                    f"vs {conflict.observation_b.source} -> {conflict.observation_b.remote_device_id_raw})"
                )
            else:
                lines.append(f"  {a_dev} {a_intf} <-> {b_dev} {b_intf}: inconsistent reciprocal observation")
    if grouped_unresolved:
        lines.append("")
        lines.append("Unresolved neighbors:")
        for raw_id, observations in grouped_unresolved.items():
            lines.append(f"  {raw_id}")
            for obs in observations:
                capability = ",".join(obs.capabilities) if obs.capabilities else "-"
                lines.append(
                    f"    {obs.local_device_id} {obs.local_interface} -> {obs.remote_port_id} ({capability})"
                )
    lines.append("")
    if result.protocol_warnings or result.l3_warnings:
        lines.append("Topology discovery completed with warnings.")
    else:
        lines.append("Topology discovery completed successfully.")
    return "\n".join(lines)


def _run_discover_topology(session: cfgmod.CliSession, include_l3: bool) -> None:
    target_name = discovery.resolve_default_topology_name(session.lab_root)
    ok, message = session.check_discovery_allowed(target_name)
    if not ok:
        print(f"% {message}")
        return
    print(f"Discovering topology from access-info '{target_name}'...")
    try:
        result = discovery.discover_topology(session.lab_root, include_l3=include_l3)
        removed_devices, removed_links = session.discovery_removals(result)
        # Everything that can fail has run; only now is the candidate
        # replaced (apply validates before it touches any session state).
        if removed_devices or removed_links:
            print(f"Discovery will remove from candidate '{target_name}' (committed definition is unchanged):")
            for device in removed_devices:
                print(f"  device {device}")
            for link in removed_links:
                print(f"  link   {link}")
        session.apply_discovery_result(result)
    except (discovery.DiscoveryError, lab.LabConfigError) as exc:
        raise discovery.DiscoveryError(f"{exc}\n% Topology candidate not updated.") from exc
    print(render_discovery_summary(result))


def h_global_discover_topology(session: cfgmod.CliSession, args: dict) -> None:
    _run_discover_topology(session, include_l3=False)


def h_global_discover_topology_l3(session: cfgmod.CliSession, args: dict) -> None:
    _run_discover_topology(session, include_l3=True)


def h_global_access_info(session: cfgmod.CliSession, args: dict) -> None:
    name = args["name"]
    ok, message = session.can_switch_definition("access_info", name)
    if not ok:
        print(f"% {message}")
        return
    session.enter_access_info_definition(name)


def h_global_no_access_info(session: cfgmod.CliSession, args: dict) -> None:
    session.remove_definition("access_info", args["name"])


def h_global_topology(session: cfgmod.CliSession, args: dict) -> None:
    name = args["name"]
    ok, message = session.can_switch_definition("topology", name)
    if not ok:
        print(f"% {message}")
        return
    plan = session.plan_topology_definition(name)
    if plan.kind == "case_collision":
        confirmed = _confirm(
            f"% An existing topology '{plan.existing}' differs only by letter case.\n"
            f"Create a separate topology named '{plan.name}'? [yes/no]: "
        )
        if not confirmed:
            return
        plan = cfgmod.TopologyPlan("create_new", plan.name)
    session.apply_topology_definition_plan(plan)


def h_global_no_topology(session: cfgmod.CliSession, args: dict) -> None:
    session.remove_topology_definition(args["name"])


def h_global_scenario(session: cfgmod.CliSession, args: dict) -> None:
    name = args["name"]
    ok, message = session.can_switch_definition("scenario", name)
    if not ok:
        print(f"% {message}")
        return
    session.enter_scenario_definition(name)


def h_global_no_scenario(session: cfgmod.CliSession, args: dict) -> None:
    session.remove_definition("scenario", args["name"])


def h_global_reference(session: cfgmod.CliSession, args: dict) -> None:
    name = args["name"]
    ok, message = session.can_switch_definition("reference", name)
    if not ok:
        print(f"% {message}")
        return
    session.enter_reference_definition(name)


def h_global_no_reference(session: cfgmod.CliSession, args: dict) -> None:
    session.remove_definition("reference", args["name"])


# ---- running-config mode ----


def h_running_access_info(session: cfgmod.CliSession, args: dict) -> None:
    session.select_access_info(args["name"])
    session.enter_running_entry("access_info", args["name"])


def h_running_access_info_remove(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_access_info_selection()


def h_running_topology(session: cfgmod.CliSession, args: dict) -> None:
    session.select_topology(args["name"])
    session.enter_running_entry("topology", args["name"])


def h_running_scenario(session: cfgmod.CliSession, args: dict) -> None:
    session.set_scenario(args["name"])
    session.enter_running_entry("scenario", args["name"])


def h_running_reference_add(session: cfgmod.CliSession, args: dict) -> None:
    # Selecting an already-active reference just (re-)enters its submode;
    # activating a new one keeps the existing add_reference() validation.
    if args["name"] not in (session.settings_candidate.get("active_references") or []):
        session.add_reference(args["name"])
    session.enter_running_entry("reference", args["name"])


def h_running_reference_remove(session: cfgmod.CliSession, args: dict) -> None:
    session.remove_reference(args["name"])


# Running-entry submodes (`config-running-<kind>-<name>`): the shared
# metadata commands operate on the submode's own entry.


def h_running_entry_description(session: cfgmod.CliSession, args: dict) -> None:
    session.set_current_running_description(args["text"])


def h_running_entry_clear_description(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_current_running_description()


# ---- topology definition mode ----


def h_topology_description(session: cfgmod.CliSession, args: dict) -> None:
    session.set_topology_description(args["text"])


def h_topology_device(session: cfgmod.CliSession, args: dict) -> None:
    session.enter_topology_device(args["name"])


# ---- topology device submode (safe fields only) ----


def h_device_set_type(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("type", args["value"])


# ---- access-info definition mode ----


def h_access_info_device(session: cfgmod.CliSession, args: dict) -> None:
    session.enter_access_info_device(args["name"])


def h_access_info_jump_host(session: cfgmod.CliSession, args: dict) -> None:
    session.enter_access_info_jump_host(args["name"])


def h_access_info_remove_device(session: cfgmod.CliSession, args: dict) -> None:
    session.remove_device(args["name"])


def h_access_info_remove_jump_host(session: cfgmod.CliSession, args: dict) -> None:
    session.remove_jump_host(args["name"])


# ---- access-info device submode (private connection fields) ----


def h_access_device_set_type(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("type", args["value"])


def h_access_device_set_address(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("address", args["value"])


def h_access_device_set_transport(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("transport", args["value"])


def h_access_device_set_port(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("port", int(args["value"]))


def h_access_device_set_username(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("username", args["value"])


def h_access_device_set_password(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("password", args["value"])


def h_access_device_clear_type(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("type")


def h_access_device_clear_address(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("address")


def h_access_device_clear_transport(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("transport")


def h_access_device_clear_username(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("username")


def h_access_device_clear_password(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("password")


def h_access_device_clear_port(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("port")


def h_access_device_set_jump_host(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("jump_host", args["value"])


def h_access_device_clear_jump_host(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("jump_host")


# ---- access-info jump-host submode (single-hop OpenSSH ProxyJump endpoint) ----


def h_access_jump_host_set_type(session: cfgmod.CliSession, args: dict) -> None:
    session.set_jump_host_field("type", args["value"])


def h_access_jump_host_set_address(session: cfgmod.CliSession, args: dict) -> None:
    session.set_jump_host_field("address", args["value"])


def h_access_jump_host_set_transport(session: cfgmod.CliSession, args: dict) -> None:
    session.set_jump_host_field("transport", args["value"])


def h_access_jump_host_set_port(session: cfgmod.CliSession, args: dict) -> None:
    session.set_jump_host_field("port", int(args["value"]))


def h_access_jump_host_set_username(session: cfgmod.CliSession, args: dict) -> None:
    session.set_jump_host_field("username", args["value"])


def h_access_jump_host_set_password(session: cfgmod.CliSession, args: dict) -> None:
    session.set_jump_host_field("password", args["value"])


def h_access_jump_host_clear_type(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_jump_host_field("type")


def h_access_jump_host_clear_address(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_jump_host_field("address")


def h_access_jump_host_clear_transport(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_jump_host_field("transport")


def h_access_jump_host_clear_username(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_jump_host_field("username")


def h_access_jump_host_clear_password(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_jump_host_field("password")


def h_access_jump_host_clear_port(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_jump_host_field("port")


HANDLERS: dict[str, Callable[[cfgmod.CliSession, dict], None]] = {
    "exec.configure": h_exec_configure,
    "exec.show_running_config": h_show_running_config,
    "exec.show_running_config_access_info": h_show_running_config_access_info,
    "exec.show_running_config_topology": h_show_running_config_topology,
    "exec.show_running_config_scenario": h_show_running_config_scenario,
    "exec.show_running_config_reference": h_show_running_config_reference,
    "exec.show_running_config_reference_name": h_show_running_config_reference_name,
    "exec.show_version": h_show_version,
    "exec.monitor_terminal": h_monitor_terminal,
    "exec.show_logging": h_show_logging,
    "exec.show_logging_summary": h_show_logging_summary,
    "exec.show_logging_device": h_show_logging_device,
    "exec.show_logging_device_file": h_show_logging_device_file,
    "exec.delete_logging_all": h_delete_logging_all,
    "exec.delete_logging_all_directory": h_delete_logging_all_directory,
    "exec.delete_logging_device_all": h_delete_logging_device_all,
    "exec.delete_logging_device_directory": h_delete_logging_device_directory,
    "exec.delete_logging_device_file": h_delete_logging_device_file,
    "exec.help": h_help,
    "exec.help_topic": h_help_topic,
    "exec.exit": h_exec_exit,
    "exec.quit": h_exec_exit,
    "global.running_config": h_global_running_config,
    "global.discover_topology": h_global_discover_topology,
    "global.discover_topology_l3": h_global_discover_topology_l3,
    "global.access_info": h_global_access_info,
    "global.topology": h_global_topology,
    "global.no_topology": h_global_no_topology,
    "global.no_access_info": h_global_no_access_info,
    "global.no_scenario": h_global_no_scenario,
    "global.no_reference": h_global_no_reference,
    "global.scenario": h_global_scenario,
    "global.reference": h_global_reference,
    "global.exit": h_end,
    "running.access_info": h_running_access_info,
    "running.access_info_remove": h_running_access_info_remove,
    "running.topology": h_running_topology,
    "running.scenario": h_running_scenario,
    "running.reference_add": h_running_reference_add,
    "running.reference_remove": h_running_reference_remove,
    "running_access_info.description": h_running_entry_description,
    "running_access_info.clear_description": h_running_entry_clear_description,
    "running_topology.description": h_running_entry_description,
    "running_topology.clear_description": h_running_entry_clear_description,
    "running_scenario.description": h_running_entry_description,
    "running_scenario.clear_description": h_running_entry_clear_description,
    "running_reference.description": h_running_entry_description,
    "running_reference.clear_description": h_running_entry_clear_description,
    "topology.description": h_topology_description,
    "topology.device": h_topology_device,
    "topology.edit": h_edit,
    "device.set_type": h_device_set_type,
    "access_info.device": h_access_info_device,
    "access_info.jump_host": h_access_info_jump_host,
    "access_info.remove_device": h_access_info_remove_device,
    "access_info.remove_jump_host": h_access_info_remove_jump_host,
    "access_device.set_type": h_access_device_set_type,
    "access_device.set_address": h_access_device_set_address,
    "access_device.set_transport": h_access_device_set_transport,
    "access_device.set_port": h_access_device_set_port,
    "access_device.set_username": h_access_device_set_username,
    "access_device.set_password": h_access_device_set_password,
    "access_device.set_jump_host": h_access_device_set_jump_host,
    "access_device.clear_type": h_access_device_clear_type,
    "access_device.clear_address": h_access_device_clear_address,
    "access_device.clear_transport": h_access_device_clear_transport,
    "access_device.clear_username": h_access_device_clear_username,
    "access_device.clear_password": h_access_device_clear_password,
    "access_device.clear_port": h_access_device_clear_port,
    "access_device.clear_jump_host": h_access_device_clear_jump_host,
    "access_jump_host.set_type": h_access_jump_host_set_type,
    "access_jump_host.set_address": h_access_jump_host_set_address,
    "access_jump_host.set_transport": h_access_jump_host_set_transport,
    "access_jump_host.set_port": h_access_jump_host_set_port,
    "access_jump_host.set_username": h_access_jump_host_set_username,
    "access_jump_host.set_password": h_access_jump_host_set_password,
    "access_jump_host.clear_type": h_access_jump_host_clear_type,
    "access_jump_host.clear_address": h_access_jump_host_clear_address,
    "access_jump_host.clear_transport": h_access_jump_host_clear_transport,
    "access_jump_host.clear_username": h_access_jump_host_clear_username,
    "access_jump_host.clear_password": h_access_jump_host_clear_password,
    "access_jump_host.clear_port": h_access_jump_host_clear_port,
    "scenario.edit": h_edit,
    "reference.edit": h_edit,
}

# Commands shared verbatim by every configuration mode (show/clear/commit/
# end/help/exit). `exit`'s target per mode comes from
# cfgmod._EXIT_PARENT_MODE (the config module's own mode-hierarchy SSOT), so
# there is nothing mode-specific to list here even though the destination
# differs. `root` is omitted at "global" (see grammar.py's
# _add_common_subtree(include_root=False) -- global has no `root` node to
# dispatch). `show version` is deliberately EXEC-only (see grammar.py's
# _add_show_subtree), so it is not part of this shared registration either.
for _mode in (
    "global",
    "running",
    "topology",
    "device",
    "access_info",
    "access_device",
    "access_jump_host",
    "scenario",
    "reference",
    *cfgmod.RUNNING_ENTRY_MODES,
    *cfgmod.RUNNING_COMMON_MODES,
):
    HANDLERS[f"{_mode}.show_running_config"] = h_show_running_config
    HANDLERS[f"{_mode}.show_configuration"] = h_show_configuration
    HANDLERS[f"{_mode}.commit"] = h_commit
    HANDLERS[f"{_mode}.clear"] = h_clear
    HANDLERS[f"{_mode}.end"] = h_end
    HANDLERS[f"{_mode}.help"] = h_help
    HANDLERS[f"{_mode}.help_topic"] = h_help_topic
    if _mode != "global":
        # global's "exit" is guarded (equivalent to "end"; see the literal
        # "global.exit": h_end entry above) since global is the top of the
        # configure-session hierarchy -- every other mode's "exit" moves up
        # one level via cfgmod._EXIT_PARENT_MODE, unguarded.
        HANDLERS[f"{_mode}.exit"] = h_exit
        HANDLERS[f"{_mode}.root"] = h_root


_COMMON_KIND_LABELS = {
    "access_info": "Access information",
    "topology": "Topology",
    "scenario": "Scenario",
    "reference": "Reference",
}


def _register_running_common_handlers(kind: str) -> None:
    """Handlers for `config-running-<kind>` (name-less). Selection and
    unselection call the SAME session methods as the parent/named commands;
    only the Mode Transition differs (stay in the submode)."""
    mode = f"running_{kind}_common"

    def enter(session: cfgmod.CliSession, args: dict) -> None:
        session.enter_running_common(kind)

    def select(session: cfgmod.CliSession, args: dict) -> None:
        name = args["name"]
        if kind == "access_info":
            session.select_access_info(name)
        elif kind == "topology":
            session.select_topology(name)
        elif kind == "scenario":
            session.set_scenario(name)
        else:
            session.select_reference_idempotent(name)

    def no(session: cfgmod.CliSession, args: dict) -> None:
        # Exact match against the Candidate's current selection, then the
        # same Candidate update as the parent-mode unselect.
        name = args["name"]
        if kind == "reference":
            session.remove_reference(name)  # raises "... is not currently selected."
            return
        if session.settings_candidate.get(f"active_{kind}") != name:
            raise cfgmod.ConfigError(f"{_COMMON_KIND_LABELS[kind]} '{name}' is not currently selected.")
        {
            "access_info": session.clear_access_info_selection,
            "topology": session.clear_topology_selection,
            "scenario": session.clear_scenario_selection,
        }[kind]()

    HANDLERS[f"running.{kind}_common"] = enter
    HANDLERS[f"{mode}.select"] = select
    HANDLERS[f"{mode}.no_select"] = no


for _kind in cfgmod.RUNNING_COMMON_MODES.values():
    _register_running_common_handlers(_kind)


# --------------------------------------------------------------------------
# REPL
# --------------------------------------------------------------------------


def _longest_common_prefix(candidates: list[str]) -> str:
    """Pure longest-common-prefix helper for the `tab` key binding's
    multiple-candidate case (e.g. "pagent_pkts"/"pagent_tgn" -> "pagent_").
    Character-by-character, case-sensitive (matching every dynamic-argument
    identifier's own case sensitivity) -- never a path-aware comparison.
    Empty input (never actually reached, since the caller only calls this
    when `len(candidates) > 1`) returns ""."""
    if not candidates:
        return ""
    prefix = candidates[0]
    for candidate in candidates[1:]:
        limit = min(len(prefix), len(candidate))
        i = 0
        while i < limit and prefix[i] == candidate[i]:
            i += 1
        prefix = prefix[:i]
        if not prefix:
            break
    return prefix


def _tab_multi_candidate_extension(result: grammar.CompletionResult) -> Optional[str]:
    """Pure decision for the `tab` key binding's multiple-candidate branch:
    the text (if any) that should replace the already-typed
    `result.replace_prefix` -- the longest common prefix across every
    candidate, but only when that is strictly longer than what is already
    typed. Returns None when there is nothing further to extend to (the
    typed text is already the full common prefix), which the caller must
    not treat as a completion. Never returns a value ending in a trailing
    space: an ambiguous multi-candidate match is never marked syntactically
    complete, even when the common prefix happens to equal one whole
    candidate that is itself also a prefix of another (e.g.
    "multi_flow_path_validation")."""
    common_prefix = _longest_common_prefix(result.candidates)
    return common_prefix if len(common_prefix) > len(result.replace_prefix) else None


def _make_key_bindings(session: cfgmod.CliSession) -> KeyBindings:
    kb = KeyBindings()

    @kb.add("?")
    def _(event) -> None:
        buffer = event.current_buffer
        text_before = buffer.document.text_before_cursor
        text_after = buffer.document.text_after_cursor
        result = grammar.help(session.mode, text_before, build_context(session))

        def _show() -> None:
            # Echo the "prompt + buffer + ?" line into scrollback first, the
            # same way a real terminal/IOS XR leaves a transcript of what was
            # actually pressed, before the help lines that answer it.
            print(f"{prompt_text(session)}{text_before}?{text_after}")
            print_help_result(result)
            # The running-config selection model footer is scoped
            # to exactly the spaced "no ?" context in running mode -- never
            # the attached "no?", never any other mode/token. Presentation
            # only: it adds no grammar candidate and cannot affect Tab
            # completion, parsing, or command history.
            if _should_show_running_no_footer(session.mode, text_before):
                print()
                print(_render_running_config_selection_model())

        if result.lines or result.show_cr:
            run_in_terminal(_show)
        else:
            event.app.output.bell()

    @kb.add("tab")
    def _(event) -> None:
        buffer = event.current_buffer
        text = buffer.document.text_before_cursor
        result = grammar.complete(session.mode, text, build_context(session))

        if len(result.candidates) == 1:
            candidate = result.candidates[0]
            if candidate != result.replace_prefix:
                buffer.delete_before_cursor(len(result.replace_prefix))
                buffer.insert_text(candidate)
            else:
                event.app.output.bell()
        elif len(result.candidates) > 1:
            # Cisco-style staged Tab: as long as the longest common prefix
            # still extends past what's typed, each Tab press only grows
            # the buffer that far -- the ambiguous candidate list is never
            # shown while there's still unambiguous ground to cover. Only
            # once the buffer already *is* the full common prefix (LCP
            # can't extend it any further) does Tab fall through to
            # listing every candidate. Purely a function of the current
            # buffer and this one completion lookup -- no persistent
            # "already listed once" state, so a Tab pressed again with the
            # buffer unchanged lists again, and a Backspace in between
            # re-evaluates from scratch next Tab.
            extension = _tab_multi_candidate_extension(result)
            if extension is not None:
                buffer.delete_before_cursor(len(result.replace_prefix))
                buffer.insert_text(extension)
                return

            def _show() -> None:
                print()
                for candidate in result.candidates:
                    print(f"  {candidate}")

            run_in_terminal(_show)
        else:
            event.app.output.bell()

    return kb


def execute_command_line(session: cfgmod.CliSession, line: str, *, interactive: bool = True) -> bool:
    """Parse and execute exactly one physical command line against the
    session's current authoritative mode/candidate state, exactly as a
    manually typed line would be. Used both for ordinary single-line input
    and for each physical line of a multi-line paste: a line that changes
    mode or candidate state (device/exit/root/end/clear/commit/...) mutates
    `session` in place, so the *next* call always sees the resulting state
    -- there is no separate paste-side notion of "current mode". Returns
    True on success, False if a parse or handler error was printed.

    `interactive` (default True, matching every pre-existing call site)
    is only ever False for a physical line inside a multi-line paste (see
    execute_input_block()) -- it is threaded into the handler's `args` as
    `"_interactive"`, a synthetic key every handler except the delete
    logging ones ignores, so a confirmation-requiring command can fail
    closed instead of ever blocking on stdin (or worse, misreading the
    next pasted line as its answer) when run from a paste.

    Propagates `_ExitCli` (EXEC `exit`/`quit`) uncaught, matching how a
    manually typed `exit` unwinds the REPL loop in `run()`."""
    result = grammar.parse(session.mode, line)
    if not result.ok:
        print_parse_error(result.error)
        if session.mode in cfgmod.RUNNING_COMMON_MODES and line.split(None, 1)[0].lower() == "description":
            print("% Use the named mode: access-info <name>")
        return False

    handler = HANDLERS[result.action]
    args = dict(result.args or {})
    args["_interactive"] = interactive
    try:
        handler(session, args)
    except (
        cfgmod.ConfigError,
        cfgmod.CommitValidationError,
        lab.LabConfigError,
        editor.EditorError,
        terminal.TerminalError,
        discovery.DiscoveryError,
    ) as exc:
        if isinstance(exc, cfgmod.CommitValidationError):
            for error in exc.errors:
                print(f"% {error}")
        else:
            print(f"% {exc}")
        return False
    return True


def execute_input_block(session: cfgmod.CliSession, text: str) -> None:
    """Execute one accepted REPL input, which is either a single manually
    typed command or a pasted multi-line configuration block (prompt_toolkit
    hands back pasted text as one string with embedded newlines). A
    single-line input is executed exactly as before, unchanged -- a single
    manually typed "!" is not special-cased here at all and reaches
    execute_command_line()/grammar.parse() exactly as it always has. A
    multi-line input is split into normalized physical command lines and
    executed sequentially, stopping at the first error -- lines already
    applied stay in the candidate (no rollback), and nothing here or in
    execute_command_line() ever calls commit implicitly. A "!" physical
    line is the one exception to "every line goes through
    execute_command_line()": it is dispatched to
    _apply_structural_bang() instead, per session.mode *at that point* in
    the sequence -- same authoritative re-read-after-every-line model as
    every other mode-changing line here.

    Propagates `_ExitCli` uncaught, so a pasted `exit`/`quit` at EXEC level
    unwinds the REPL loop exactly like a manually typed one."""
    if "\n" in _normalize_newlines(text):
        section_has_content = False
        for line in _split_pasted_command_lines(text):
            if line == "!":
                _apply_structural_bang(session, section_has_content)
                section_has_content = False
                continue
            mode_before = session.mode
            if not execute_command_line(session, line, interactive=False):
                break
            # A line that moved between modes (enter or `exit`/`root`) opens a
            # fresh, still-empty section: a following "!" is then redundant
            # (e.g. `exit` + "!" in legacy scripts, or "!" right after
            # `running-config`) and must not climb another level.
            section_has_content = session.mode == mode_before
    else:
        execute_command_line(session, text)


def run() -> None:
    lab_root = lab.find_lab_root()
    session = cfgmod.CliSession(lab_root)
    history = MaskingHistory()
    key_bindings = _make_key_bindings(session)
    prompt_session: PromptSession = PromptSession(
        history=history,
        key_bindings=key_bindings,
        complete_while_typing=False,
    )

    print("Network Lab CLI. Type 'help' or press '?' for available commands.")

    while True:
        try:
            raw = prompt_session.prompt(prompt_text(session))
        except KeyboardInterrupt:
            # Cancel only the current partially typed input line; the
            # candidate configuration and current mode are untouched.
            continue
        except EOFError:
            if session.overall_dirty():
                print()
                print("% Uncommitted changes exist. Use 'commit' or 'clear'.")
                continue
            print()
            break

        if not raw.strip():
            continue

        try:
            execute_input_block(session, raw)
        except _ExitCli:
            break


def main() -> None:
    try:
        run()
    except lab.LabConfigError as exc:
        print(f"% {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
