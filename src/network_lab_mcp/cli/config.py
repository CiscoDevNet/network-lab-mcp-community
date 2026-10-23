# SPDX-License-Identifier: Apache-2.0
"""Candidate configuration session for the Network Lab CLI.

Two independent candidate scopes exist, and `clear`/`commit` always act on
both together:

- the running-config candidate (`settings_candidate`): which topology,
  scenario, and references MCP will use once committed.
- at most one definition candidate at a time (`definition_kind` /
  `definition_candidate`): a topology, access-info, scenario, or reference
  *definition* being created or edited. Selecting a different definition
  while the current one is dirty is blocked, mirroring the original
  topology-switch guard; moving into or out of running-config mode is not,
  since it is an independent scope.

Everything here is memory-only until commit() writes changed YAML to disk.
The validators in `network_lab_mcp.lab` remain the single source of
truth for structural validity; this module does not duplicate those rules.
It only adds the narrow, CLI-specific case-only topology-name collision
safety check, which is not a validator replacement.
"""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from network_lab_mcp import discovery, lab


class ConfigError(Exception):
    """A clear, user-facing error raised while mutating candidate configuration."""


@dataclass(frozen=True)
class CommitReport:
    """What a successful commit() actually did (input for CLI output).

    definition: (kind, name, "saved" | "deleted") if a definition file was
    actually written/removed, else None. settings_written: whether
    settings.yaml was actually rewritten. warnings: pre-existing,
    unrelated missing-definition references left untouched, as safe
    "<kind> '<name>'" strings (sorted, deduplicated)."""

    definition: Optional[tuple[str, str, str]]
    settings_written: bool
    warnings: tuple[str, ...]


class CommitValidationError(Exception):
    """Raised when commit() validation fails. No disk writes have occurred."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def find_case_only_collision(entered_name: str, existing_names: list[str]) -> Optional[str]:
    """Return an existing name that differs from `entered_name` only by
    letter case, or None.

    This is a narrow safety check, not fuzzy matching: any other difference
    (including '-' vs '_' or added/removed characters) never triggers it,
    and an exact match is not a collision (it is a normal existing-object
    selection).
    """
    if entered_name in existing_names:
        return None
    folded = entered_name.casefold()
    for existing in existing_names:
        if existing != entered_name and existing.casefold() == folded:
            return existing
    return None


@dataclass
class TopologyPlan:
    kind: str  # "select_existing" | "case_collision" | "create_new"
    name: str
    existing: Optional[str] = None


def new_topology_data(name: str) -> dict:
    return {"name": name, "description": "", "devices": {}, "links": []}


def new_access_info_data(name: str) -> dict:
    return {"name": name, "devices": {}}


def new_scenario_data(name: str) -> dict:
    return {"name": name, "description": "", "objectives": []}


def new_reference_data(name: str) -> dict:
    return {"name": name, "description": "", "guidance": []}


DEVICE_FIELD_ORDER = ("type", "address", "transport", "port", "username", "password", "jump_host")
JUMP_HOST_FIELD_ORDER = ("type", "address", "transport", "port", "username", "password")

# Which config mode "exit" moves up to. Only "global" is missing here --
# its exit/end is a guarded jump straight to EXEC, handled separately.
# Active running-config entry submodes (`config-running-<kind>-<name>`) ->
# the running-config kind they edit. Entered by selecting an entry from
# config-running; they edit metadata of that active entry only.
RUNNING_ENTRY_MODES = {
    "running_access_info": "access_info",
    "running_topology": "topology",
    "running_scenario": "scenario",
    "running_reference": "reference",
}

# Name-less running-config selection submodes (`config-running-<kind>`),
# mode -> definition kind. Deliberately separate from RUNNING_ENTRY_MODES.
RUNNING_COMMON_MODES = {
    "running_access_info_common": "access_info",
    "running_topology_common": "topology",
    "running_scenario_common": "scenario",
    "running_reference_common": "reference",
}

_EXIT_PARENT_MODE = {
    "running": "global",
    **{mode: "running" for mode in RUNNING_ENTRY_MODES},
    # Name-less selection submodes: NOT running-entry modes (no current
    # entry name, no description target).
    **{mode: "running" for mode in RUNNING_COMMON_MODES},
    "topology": "global",
    "access_info": "global",
    "scenario": "global",
    "reference": "global",
    "device": "topology",
    "access_device": "access_info",
    "access_jump_host": "access_info",
}

_DEFINITION_LOADERS: dict[str, tuple[Callable, Callable, Callable]] = {
    "topology": (lab.topology_exists, lab.load_topology, new_topology_data),
    "access_info": (lab.access_info_exists, lab.load_access_info, new_access_info_data),
    "scenario": (lab.scenario_exists, lab.load_scenario, new_scenario_data),
    "reference": (lab.reference_exists, lab.load_reference, new_reference_data),
}

_DEFINITION_VALIDATORS: dict[str, Callable] = {
    "topology": lab.validate_topology_data,
    "access_info": lab.validate_access_info_data,
    "scenario": lab.validate_scenario_data,
    "reference": lab.validate_reference_data,
}

_DEFINITION_WRITERS: dict[str, Callable] = {
    "topology": lab.write_topology,
    "access_info": lab.write_access_info,
    "scenario": lab.write_scenario,
    "reference": lab.write_reference,
}

# Every definition kind supports candidate deletion (`no <kind>
# <name>`, see remove_definition()/commit() below) -- one entry per kind,
# mirroring _DEFINITION_LOADERS/_DEFINITION_WRITERS exactly.
_DEFINITION_DELETERS: dict[str, Callable] = {
    "topology": lab.delete_topology,
    "access_info": lab.delete_access_info,
    "scenario": lab.delete_scenario,
    "reference": lab.delete_reference,
}

# Used by remove_definition() to check (before creating a deletion
# candidate) that `name` is an exact, existing, path-safe stored
# definition of the given kind -- the same check delete_*() itself
# re-applies at commit time (see lab.py's _stored_definition_is_deletable()).
_DEFINITION_DELETABILITY_CHECKS: dict[str, Callable] = {
    "topology": lab.topology_is_deletable,
    "access_info": lab.access_info_is_deletable,
    "scenario": lab.scenario_is_deletable,
    "reference": lab.reference_is_deletable,
}

# Human-readable kind labels for error messages, matching each kind's
# existing wording elsewhere in this module exactly (e.g.
# select_access_info()'s "Access information '<name>' does not exist.").
_DEFINITION_KIND_LABELS: dict[str, str] = {
    "topology": "Topology",
    "access_info": "Access information",
    "scenario": "Scenario",
    "reference": "Reference",
}


def _format_link(link: dict) -> str:
    def endpoint(side: str) -> str:
        interface = link.get(f"{side}_interface")
        return f"{link.get(side)} {interface}" if interface else str(link.get(side))

    return f"{endpoint('a')} <-> {endpoint('b')}"


def load_committed_definition(kind: str, name: str, lab_root: Path) -> Optional[dict]:
    """Return the current committed-on-disk data for a definition of the
    given kind/name, or None if it does not exist yet.

    Always re-reads disk fresh -- never the in-memory candidate -- so a
    definition-scoped `show running-config` (see cli/main.py) reflects the
    real committed state, including one committed moments ago by this same
    session's own commit()."""
    exists_fn, load_fn, _ = _DEFINITION_LOADERS[kind]
    if not exists_fn(name, lab_root):
        return None
    return load_fn(name, lab_root)


# Running-config entry descriptions live in settings.yaml under the optional
# `running_descriptions` mapping: {kind: {entry_name: text}}. They are
# metadata of the *active* entries only; normalized_settings() is the one
# place that drops entries that are no longer active, so a description can
# never outlive its running entry (and is never resurrected on re-select).
_RUNNING_DESCRIPTION_SINGLE_KEYS = {
    "access_info": "active_access_info",
    "topology": "active_topology",
    "scenario": "active_scenario",
}
_SELECTION_FIELDS = (
    ("access_info", "active_access_info"),
    ("topology", "active_topology"),
    ("scenario", "active_scenario"),
    ("reference", "active_references"),
)
RUNNING_DESCRIPTIONS_KEY = "running_descriptions"


def active_running_entries(settings: dict) -> dict[str, list[str]]:
    entries = {kind: [settings.get(key)] if settings.get(key) else [] for kind, key in _RUNNING_DESCRIPTION_SINGLE_KEYS.items()}
    entries["reference"] = list(settings.get("active_references") or [])
    return entries


def normalized_settings(settings: dict) -> dict:
    """Deep copy of `settings` with running-entry descriptions restricted to
    currently active entries (non-empty strings only); an empty
    `running_descriptions` is omitted entirely."""
    out = copy.deepcopy(settings)
    raw = out.pop(RUNNING_DESCRIPTIONS_KEY, None)
    if not isinstance(raw, dict):
        return out
    pruned: dict[str, dict[str, str]] = {}
    for kind, names in active_running_entries(out).items():
        entries = raw.get(kind)
        if not isinstance(entries, dict):
            continue
        kept = {n: t for n, t in entries.items() if n in names and isinstance(t, str) and t.strip()}
        if kept:
            pruned[kind] = kept
    if pruned:
        out[RUNNING_DESCRIPTIONS_KEY] = pruned
    return out


class CliSession:
    """Mutable candidate-configuration state for one CLI process lifetime.

    Not imported by cli/grammar.py; cli/main.py builds a read-only
    grammar.CliContext from this session's data on every parse/completion/
    help call instead.
    """

    def __init__(self, lab_root: Path):
        self.lab_root = lab_root
        self.mode = "exec"
        self.committed_settings: Optional[dict] = None
        self.settings_candidate: Optional[dict] = None
        self.definition_kind: Optional[str] = None  # "topology" | "access_info" | "scenario" | "reference"
        self.definition_name: Optional[str] = None
        self.definition_original: Optional[dict] = None  # None => new/unsaved
        self.definition_candidate: Optional[dict] = None
        self.current_device_name: Optional[str] = None
        self.current_jump_host_name: Optional[str] = None
        self.last_commit_report: Optional[CommitReport] = None
        # Discovery dirty tracking (in-process only, same lifetime as the
        # candidate itself): `_mutation_rev` counts manual candidate
        # mutations that actually changed something since the candidate
        # was last (re)loaded/cleared/committed; `_discovery_baseline_rev`
        # is its value right after the last Discovery rebuilt the
        # candidate. A re-Discovery is allowed only while the two match --
        # even if a manual edit was reverted to the same value.
        self._mutation_rev = 0
        self._discovery_baseline_rev = 0

    def _reset_mutation_tracking(self) -> None:
        self._mutation_rev = 0
        self._discovery_baseline_rev = 0

    # ---- scoped dirty state ----

    def settings_dirty(self) -> bool:
        return self.settings_candidate is not None and normalized_settings(self.settings_candidate) != normalized_settings(
            self.committed_settings
        )

    def definition_dirty(self) -> bool:
        if self.definition_kind is None:
            return False
        if self.definition_candidate is None:
            # A whole definition is prospectively deleted (currently only
            # topology definitions support this -- see
            # remove_topology_definition()). Always dirty: the final
            # state ("absent") always differs from a real committed
            # original -- remove_topology_definition() never leaves this
            # state set for a never-committed (definition_original is
            # None) candidate, which it discards outright instead.
            return True
        if self.definition_original is None:
            return True  # brand-new, never-committed definition
        return self.definition_candidate != self.definition_original

    def overall_dirty(self) -> bool:
        return self.settings_dirty() or self.definition_dirty()

    # ---- configure entry / full exit ----

    def enter_configure(self) -> None:
        self.committed_settings = lab.read_settings(self.lab_root)
        self.settings_candidate = copy.deepcopy(self.committed_settings)
        self.definition_kind = None
        self.definition_name = None
        self.definition_original = None
        self.definition_candidate = None
        self.current_device_name = None
        self.current_jump_host_name = None
        self.current_running_entry_name = None
        self._reset_mutation_tracking()
        self.mode = "global"

    def reset_to_exec(self) -> None:
        """Return to EXEC, discarding all candidate state. By construction,
        EXEC mode always implies overall_dirty() is False."""
        self.mode = "exec"
        self.committed_settings = None
        self.settings_candidate = None
        self.definition_kind = None
        self.definition_name = None
        self.definition_original = None
        self.definition_candidate = None
        self.current_device_name = None
        self.current_jump_host_name = None
        self.current_running_entry_name = None
        self._reset_mutation_tracking()

    def clear(self) -> None:
        """Discard every uncommitted change in the current configure
        session -- both the running-config candidate and the definition
        candidate -- restoring committed state. Never writes disk, never
        returns to EXEC. A brand-new (never-committed) definition candidate
        is discarded entirely, not "reset to empty"."""
        if self.committed_settings is not None:
            self.settings_candidate = copy.deepcopy(self.committed_settings)
        self._reset_mutation_tracking()
        if self.definition_kind is not None:
            if self.definition_original is None:
                self.definition_kind = None
                self.definition_name = None
                self.definition_candidate = None
                self.current_device_name = None
                self.current_jump_host_name = None
            else:
                self.definition_candidate = copy.deepcopy(self.definition_original)
                if self.current_device_name is not None:
                    devices = self.definition_candidate.get("devices") or {}
                    if self.current_device_name not in devices:
                        self.current_device_name = None
                if self.current_jump_host_name is not None:
                    jump_hosts = self.definition_candidate.get("jump_hosts") or {}
                    if self.current_jump_host_name not in jump_hosts:
                        self.current_jump_host_name = None
        self._reconcile_mode_after_clear()

    def _reconcile_mode_after_clear(self) -> None:
        """After clear(), fall back to the nearest still-valid parent mode
        instead of lingering in a submode whose target no longer exists."""
        self._reconcile_running_entry_mode()
        if self.definition_kind is None and self.mode in (
            "topology",
            "device",
            "access_info",
            "access_device",
            "access_jump_host",
            "scenario",
            "reference",
        ):
            self.mode = "global"
            self.current_device_name = None
            self.current_jump_host_name = None
            return
        if self.mode in ("device", "access_device") and self.current_device_name is None:
            self.mode = "topology" if self.mode == "device" else "access_info"
        if self.mode == "access_jump_host" and self.current_jump_host_name is None:
            self.mode = "access_info"

    def _reconcile_running_entry_mode(self) -> None:
        """A running-entry submode is valid only while its entry is active
        in the candidate running-config; otherwise fall back to
        config-running so a stale submode can never edit an inactive entry."""
        kind = RUNNING_ENTRY_MODES.get(self.mode)
        if kind is None:
            return
        if self.settings_candidate is None or self.current_running_entry_name not in active_running_entries(
            self.settings_candidate
        )[kind]:
            self.mode = "running"
            self.current_running_entry_name = None

    # ---- definition switching guard (shared by topology/access-info/scenario/reference) ----

    def can_switch_definition(self, kind: str, name: str) -> tuple[bool, Optional[str]]:
        if self.definition_kind is None:
            return True, None
        if (self.definition_kind, self.definition_name) == (kind, name):
            return True, None
        if self.definition_dirty():
            return False, "Uncommitted changes exist. Use 'commit' or 'clear' before switching."
        return True, None

    def _enter_definition(self, kind: str, name: str, mode: str) -> None:
        exists_fn, load_fn, new_data_fn = _DEFINITION_LOADERS[kind]
        if not (self.definition_kind == kind and self.definition_name == name and self.definition_candidate is not None):
            if exists_fn(name, self.lab_root):
                data = load_fn(name, self.lab_root)
                self.definition_original = copy.deepcopy(data)
                self.definition_candidate = copy.deepcopy(data)
            else:
                self.definition_original = None
                self.definition_candidate = new_data_fn(name)
            self.definition_kind = kind
            self.definition_name = name
            self._reset_mutation_tracking()
        self.current_device_name = None
        self.mode = mode

    # ---- topology definition (with the case-only collision safeguard) ----

    def plan_topology_definition(self, name: str) -> TopologyPlan:
        if self.definition_kind == "topology" and self.definition_name == name:
            return TopologyPlan("select_existing", name)
        existing_names = lab.list_topology_names(self.lab_root)
        if name in existing_names:
            return TopologyPlan("select_existing", name)
        collision = find_case_only_collision(name, existing_names)
        if collision is not None:
            return TopologyPlan("case_collision", name, collision)
        return TopologyPlan("create_new", name)

    def apply_topology_definition_plan(self, plan: TopologyPlan) -> None:
        if plan.kind == "case_collision":
            raise ConfigError("A case-only topology collision must be confirmed before it can be applied.")
        self._enter_definition("topology", plan.name, "topology")

    def remove_definition(self, kind: str, name: str) -> None:
        """`no <kind> <name>` (global configuration only): candidate
        deletion of one stored definition -- never an immediate
        filesystem operation (see commit()). Shared by all four
        definition kinds (topology/access_info/scenario/reference).
        Participates in the same "at most one dirty definition candidate
        at a time" rule as every other definition mutation
        (can_switch_definition()); deletion counts as a definition
        mutation just like editing, so a different dirty definition (of
        *any* kind) blocks it, and a pending deletion blocks
        editing/creating/deleting a *different* definition, exactly like
        the existing definition-switching guard. `topology test_lab` and
        `access-info test_lab` are different candidate identities
        (`definition_kind` + `definition_name`) even though the name
        string matches.

        The candidate represents deletion as `definition_candidate is
        None` while `definition_kind`/`definition_name`/
        `definition_original` stay set to the real committed definition
        being removed -- reusing `_enter_definition()`'s existing reload
        guard (`definition_candidate is not None`) means re-entering the
        *same* definition (e.g. `topology <name>`) automatically reloads
        and restores it, cancelling the pending deletion, with no
        changes needed there. `clear()`'s existing "restore from
        definition_original" branch cancels it the same way -- this
        never re-reads or re-renders a deleted definition's own content
        (private access-info fields included) beyond what `load_fn`
        itself needs to restore the candidate.

        Deleting the definition *currently open as a brand-new, never-
        committed candidate* (`definition_original is None`) is instead a
        net-zero cancellation: the whole candidate slot is discarded
        outright, exactly like `clear()` already treats that case, since
        there is nothing real to mark as prospectively absent."""
        if self.definition_kind == kind and self.definition_name == name:
            self._mutation_rev += 1
            if self.definition_original is None:
                self.definition_kind = None
                self.definition_name = None
                self.definition_candidate = None
                self.current_device_name = None
                self.current_jump_host_name = None
                return
            self.definition_candidate = None
            self.current_device_name = None
            self.current_jump_host_name = None
            return
        ok, message = self.can_switch_definition(kind, name)
        if not ok:
            raise ConfigError(message)
        if not _DEFINITION_DELETABILITY_CHECKS[kind](name, self.lab_root):
            raise ConfigError(f"{_DEFINITION_KIND_LABELS[kind]} '{name}' does not exist.")
        _, load_fn, _ = _DEFINITION_LOADERS[kind]
        self.definition_original = load_fn(name, self.lab_root)
        self.definition_candidate = None
        self._reset_mutation_tracking()
        self._mutation_rev += 1
        self.definition_kind = kind
        self.definition_name = name
        self.current_device_name = None
        self.current_jump_host_name = None

    def remove_topology_definition(self, name: str) -> None:
        """`no topology <name>` -- see remove_definition(), the shared
        implementation this delegates to. Kept as its own method since
        existing tests/callers already use this exact name."""
        self.remove_definition("topology", name)

    def check_discovery_allowed(self, target_name: str) -> tuple[bool, Optional[str]]:
        """Dirty-candidate preflight for `discover topology`. A different
        dirty definition blocks it (single dirty definition slot). The
        target topology itself blocks it when it carries manual edits --
        any manual mutation since the candidate was loaded or last
        rebuilt by Discovery, even one later reverted by hand. A
        candidate that only Discovery produced may be rebuilt again."""
        ok, message = self.can_switch_definition("topology", target_name)
        if not ok:
            return False, message
        if (self.definition_kind, self.definition_name) == ("topology", target_name):
            if self.definition_dirty() and self._mutation_rev != self._discovery_baseline_rev:
                return False, (
                    "Topology discovery rejected.\n"
                    "% Manual edits were made since the last discovery.\n"
                    "% Use 'clear' to reset the candidate before retrying."
                )
        return True, None

    def discovery_removals(self, result: "discovery.DiscoveryResult") -> tuple[list[str], list[str]]:
        """Committed devices and links of the target topology that the
        rebuilt candidate will not contain (sorted, for display before the
        candidate is replaced). Read-only; empty for a new topology."""
        committed = load_committed_definition("topology", result.default_topology_name, self.lab_root)
        if committed is None:
            return [], []
        new = discovery.build_discovery_candidate(result, {})
        removed_devices = sorted(set(committed.get("devices") or {}) - set(new["devices"]))
        new_keys = {discovery._topology_link_key(link) for link in new["links"]}
        removed_links = sorted(
            _format_link(link)
            for link in committed.get("links") or []
            if discovery._topology_link_key(link) not in new_keys
        )
        return removed_devices, removed_links

    def apply_discovery_result(self, result: "discovery.DiscoveryResult") -> None:
        """Replace the target topology's candidate with one rebuilt from
        this Discovery run alone (see discovery.build_discovery_candidate()):
        no existing device or link is merged in; only the name,
        description and unknown top-level metadata survive. The new
        candidate is built and validated *before* any session state is
        touched, so a failure leaves the previous candidate, mode and
        tracking untouched. Never committed and never selected as
        active_topology."""
        plan = self.plan_topology_definition(result.default_topology_name)
        if plan.kind == "case_collision":
            raise ConfigError("A case-only topology collision must be confirmed before it can be applied.")
        if self.definition_kind == "topology" and self.definition_name == plan.name and self.definition_candidate is not None:
            base = self.definition_candidate
        else:
            base = load_committed_definition("topology", plan.name, self.lab_root) or new_topology_data(plan.name)
        candidate = discovery.build_discovery_candidate(result, base)
        try:
            lab.validate_topology_data(plan.name, candidate)
        except lab.LabConfigError as exc:
            raise discovery.DiscoveryError(f"Discovered topology candidate is invalid: {exc}") from exc
        self.apply_topology_definition_plan(plan)
        self.definition_candidate = candidate
        self._discovery_baseline_rev = self._mutation_rev

    def enter_access_info_definition(self, name: str) -> None:
        self._enter_definition("access_info", name, "access_info")

    def enter_scenario_definition(self, name: str) -> None:
        self._enter_definition("scenario", name, "scenario")

    def enter_reference_definition(self, name: str) -> None:
        self._enter_definition("reference", name, "reference")

    def replace_definition_candidate(self, data: dict) -> None:
        """Install `data` (typically the result of an external-editor round
        trip) as the definition candidate, after validating it with the
        same SSOT validator commit() will use -- an invalid edit never
        reaches the candidate."""
        _DEFINITION_VALIDATORS[self.definition_kind](self.definition_name, data)
        if data != self.definition_candidate:
            self._mutation_rev += 1
        self.definition_candidate = data

    # ---- topology-mode mutation ----

    def set_topology_description(self, text: str) -> None:
        if self.definition_candidate.get("description") != text or "description" not in self.definition_candidate:
            self._mutation_rev += 1
        self.definition_candidate["description"] = text

    # ---- navigation: `root` jumps straight to global config, preserving
    # candidate state (never commits, never clears) ----

    def go_to_global(self) -> None:
        self.current_running_entry_name = None
        self.current_device_name = None
        self.current_jump_host_name = None
        self.mode = "global"

    # ---- device sub-editing, shared by topology (safe fields only) and
    # access-info (private connection fields) ----

    def enter_topology_device(self, name: str) -> None:
        devices = self.definition_candidate.setdefault("devices", {})
        if name not in devices:
            devices[name] = {}
            self._mutation_rev += 1
        self.current_device_name = name
        self.mode = "device"

    def enter_access_info_device(self, name: str) -> None:
        devices = self.definition_candidate.setdefault("devices", {})
        if name not in devices:
            devices[name] = {}
            self._mutation_rev += 1
        self.current_device_name = name
        self.mode = "access_device"

    def _device(self) -> dict:
        return self.definition_candidate["devices"][self.current_device_name]

    def set_device_field(self, field_name: str, value) -> None:
        device = self._device()
        if field_name not in device or device[field_name] != value:
            self._mutation_rev += 1
        device[field_name] = value

    def clear_device_field(self, field_name: str) -> None:
        if field_name in self._device():
            self._mutation_rev += 1
        self._device().pop(field_name, None)

    def remove_device(self, name: str) -> None:
        """`no device <name>`: remove the whole device object from the
        candidate only. Never cascades to topology, tmux, Discovery, or any
        other definition -- access-info and topology are separate models."""
        devices = self.definition_candidate.get("devices") or {}
        if name not in devices:
            raise ConfigError(f"Device '{name}' does not exist.")
        del devices[name]
        self._mutation_rev += 1

    # ---- jump-host sub-editing (access-info only: a reusable single-hop
    # OpenSSH ProxyJump endpoint, referenced by name from a device's own
    # optional 'jump_host' field) ----

    def enter_access_info_jump_host(self, name: str) -> None:
        jump_hosts = self.definition_candidate.setdefault("jump_hosts", {})
        if name not in jump_hosts:
            jump_hosts[name] = {}
            self._mutation_rev += 1
        self.current_jump_host_name = name
        self.mode = "access_jump_host"

    def _jump_host(self) -> dict:
        return self.definition_candidate["jump_hosts"][self.current_jump_host_name]

    def set_jump_host_field(self, field_name: str, value) -> None:
        jump_host = self._jump_host()
        if field_name not in jump_host or jump_host[field_name] != value:
            self._mutation_rev += 1
        jump_host[field_name] = value

    def clear_jump_host_field(self, field_name: str) -> None:
        if field_name in self._jump_host():
            self._mutation_rev += 1
        self._jump_host().pop(field_name, None)

    def remove_jump_host(self, name: str) -> None:
        """`no jump-host <name>`: remove the whole jump-host object from the
        candidate only. Deliberately does not cascade -- a device still
        referencing this jump host is left as-is (a dangling reference for
        commit's existing validate_device_jump_host_references() to catch);
        fixing that reference is the operator's explicit next step, never
        an automatic side effect of this command."""
        jump_hosts = self.definition_candidate.get("jump_hosts") or {}
        if name not in jump_hosts:
            raise ConfigError(f"Jump host '{name}' does not exist.")
        del jump_hosts[name]
        self._mutation_rev += 1

    # ---- running-config selection (topology/scenario/references MCP uses) ----

    def _definition_will_exist(self, kind: str, exists_fn: Callable, name: str) -> bool:
        """True if `name` is already committed, or is the definition
        currently being created/edited in this same configure session --
        selecting a definition you are actively authoring (not yet
        committed) is allowed, since running-config and definition editing
        are independent candidate scopes that can be open at once."""
        return bool(exists_fn(name, self.lab_root) or (self.definition_kind == kind and self.definition_name == name))

    def select_access_info(self, name: str) -> None:
        if not self._definition_will_exist("access_info", lab.access_info_exists, name):
            raise ConfigError(f"Access information '{name}' does not exist.")
        self.settings_candidate["active_access_info"] = name

    def clear_access_info_selection(self) -> None:
        """`no access-info`: remove the selection from the candidate
        (omission, not a sentinel value like "none"/"disabled") -- a
        legitimate fail-closed state once committed, not an error."""
        self.settings_candidate.pop("active_access_info", None)

    def select_topology(self, name: str) -> None:
        if not self._definition_will_exist("topology", lab.topology_exists, name):
            raise ConfigError(f"Topology '{name}' does not exist.")
        self.settings_candidate["active_topology"] = name

    def set_scenario(self, name: str) -> None:
        if not self._definition_will_exist("scenario", lab.scenario_exists, name):
            raise ConfigError(f"Scenario '{name}' does not exist.")
        self.settings_candidate["active_scenario"] = name

    def select_reference_idempotent(self, name: str) -> None:
        """Select `name` among the references unless it already is (what
        `config-running# reference <name>` does before entering its
        submode): a re-selection is a no-op, new ones use add_reference()."""
        if name not in (self.settings_candidate.get("active_references") or []):
            self.add_reference(name)

    def add_reference(self, name: str) -> None:
        refs = self.settings_candidate.setdefault("active_references", [])
        if name in refs:
            raise ConfigError(f"Reference '{name}' is already selected.")
        if not self._definition_will_exist("reference", lab.reference_exists, name):
            raise ConfigError(f"Reference '{name}' does not exist.")
        if self._is_pending_whole_deletion("reference", name):
            raise ConfigError(
                f"Reference '{name}' is pending deletion in this session. "
                f"Use 'clear' to cancel the deletion before selecting it."
            )
        refs.append(name)
        self._stabilize_reference_order()

    def remove_reference(self, name: str) -> None:
        refs = self.settings_candidate.setdefault("active_references", [])
        if name not in refs:
            raise ConfigError(f"Reference '{name}' is not currently selected.")
        refs.remove(name)
        self._stabilize_reference_order()

    def _stabilize_reference_order(self) -> None:
        """Order of `active_references` is only ever (a) the committed order
        for references that were already active, followed by (b) newly
        activated ones in activation order -- there is no command to
        reorder. Re-deriving it after every add/remove makes an
        unselect-then-reselect sequence an exact net-zero (identical list),
        so the order-sensitive dirty check, the set-based delta renderer and
        the commit write all agree."""
        refs = self.settings_candidate.get("active_references")
        if not isinstance(refs, list):
            return
        committed = list((self.committed_settings or {}).get("active_references") or [])
        stable = [r for r in committed if r in refs] + [r for r in refs if r not in committed]
        refs[:] = stable

    def _is_pending_whole_deletion(self, kind: str, name: str) -> bool:
        """True while `no <kind> <name>` is a pending (uncommitted) deletion
        candidate. Cancelled by `clear` or by re-entering the definition."""
        return (
            self.definition_kind == kind
            and self.definition_name == name
            and self.definition_candidate is None
            and self.definition_original is not None
        )

    # ---- running-config entry descriptions (metadata of an ACTIVE entry;
    # never a selection, never written to a definition) ----

    def _require_active_running_entry(self, kind: str, name: str) -> None:
        if name not in active_running_entries(self.settings_candidate)[kind]:
            label = {"access_info": "Access-info", "topology": "Topology", "scenario": "Scenario", "reference": "Reference"}[kind]
            raise ConfigError(f"{label} '{name}' is not active in running-config.")

    def set_running_description(self, kind: str, name: str, text: str) -> None:
        self._require_active_running_entry(kind, name)
        descriptions = self.settings_candidate.setdefault(RUNNING_DESCRIPTIONS_KEY, {})
        descriptions.setdefault(kind, {})[name] = text

    def clear_running_description(self, kind: str, name: str) -> None:
        self._require_active_running_entry(kind, name)
        (self.settings_candidate.get(RUNNING_DESCRIPTIONS_KEY) or {}).get(kind, {}).pop(name, None)

    def enter_running_entry(self, kind: str, name: str) -> None:
        """Enter the submode of an entry that is active in the candidate
        (callers select/activate it first)."""
        self._require_active_running_entry(kind, name)
        self.current_running_entry_name = name
        self.mode = f"running_{kind}"

    def enter_running_common(self, kind: str) -> None:
        """Enter the name-less selection submode of `kind`. Entry alone
        never touches the candidate."""
        self.current_running_entry_name = None
        self.mode = f"running_{kind}_common"

    def enter_running_access_info_common(self) -> None:
        self.enter_running_common("access_info")

    def clear_topology_selection(self) -> None:
        """Candidate-only unselect (omission). The committed running-config
        must always have an active topology: a candidate without one is
        allowed to exist, but commit() rejects it."""
        self.settings_candidate.pop("active_topology", None)

    def clear_scenario_selection(self) -> None:
        """Candidate-only unselect; commit() rejects a missing scenario."""
        self.settings_candidate.pop("active_scenario", None)

    def set_current_running_description(self, text: str) -> None:
        self._reconcile_running_entry_mode()
        kind = RUNNING_ENTRY_MODES.get(self.mode)
        if kind is None:
            raise ConfigError("The running-config entry is no longer active.")
        self.set_running_description(kind, self.current_running_entry_name, text)

    def clear_current_running_description(self) -> None:
        self._reconcile_running_entry_mode()
        kind = RUNNING_ENTRY_MODES.get(self.mode)
        if kind is None:
            raise ConfigError("The running-config entry is no longer active.")
        self.clear_running_description(kind, self.current_running_entry_name)

    # ---- commit ----

    def _running_config_findings(self, settings: Optional[dict], exists: Callable[[str, Optional[str]], bool]) -> Counter:
        """Missing-definition findings for one running-config selection
        mapping, as a multiset keyed by a semantically stable identity:
        (source kind, source name, source field, target kind, target name).
        Never keyed by list index, so reordering a selection does not look
        like a new finding; the multiset count is the occurrence count.

        `exists(kind, name)` decides whether a target will exist -- the
        committed disk state for the baseline, the effective post-commit
        state for the candidate."""
        findings: Counter = Counter()
        settings = settings or {}
        for kind, field in _SELECTION_FIELDS:
            raw = settings.get(field)
            names = raw if isinstance(raw, list) else [raw]
            for name in names:
                if name and not exists(kind, name):
                    findings[("running-config", "", field, kind, name)] += 1
        return findings

    def commit(self) -> bool:
        """Validate and persist the candidate. Returns True if anything was
        written, False for a no-op ("No changes to commit."). Raises
        CommitValidationError (no disk writes at all) on validation failure.

        Validation runs in two independent stages:

        A. Changed-object strict validation: the definition being
           created/edited is always run through its formal validator; a
           pre-existing defect in it is never excused by stage B.
        B. Baseline/candidate comparison of running-config reference
           findings. A finding that is new, or whose occurrence count grew
           (e.g. a deletion that breaks an active selection, or a newly
           selected missing definition), fails the commit. A finding that
           is unchanged from the committed baseline is unrelated to this
           change: it is reported as a warning in `last_commit_report` and
           never blocks the save or is repaired.

        Definition files are written before settings.yaml, since the
        running-config selection may point at a definition just created or
        edited in this same commit."""
        self.last_commit_report = None
        if not self.overall_dirty():
            return False

        errors: list[str] = []
        definition_being_written = self.definition_kind is not None and self.definition_dirty()
        definition_being_deleted = definition_being_written and self.definition_candidate is None

        if definition_being_written and not definition_being_deleted:
            try:
                _DEFINITION_VALIDATORS[self.definition_kind](self.definition_name, self.definition_candidate)
            except lab.LabConfigError as exc:
                errors.append(str(exc))

        def _baseline_exists(kind: str, name: str) -> bool:
            return bool(_DEFINITION_LOADERS[kind][0](name, self.lab_root))

        def _effective_exists(kind: str, name: str) -> bool:
            if self.definition_kind == kind and self.definition_name == name and definition_being_written:
                return not definition_being_deleted
            return _baseline_exists(kind, name)

        settings = self.settings_candidate

        # Existing fail-closed invariants: an unselected topology/scenario
        # is still rejected (unchanged behavior).
        if not settings.get("active_topology"):
            errors.append("Running configuration is missing a valid active topology.")
        if not settings.get("active_scenario"):
            errors.append("Running configuration is missing a valid active scenario.")

        baseline = self._running_config_findings(self.committed_settings, _baseline_exists)
        candidate = self._running_config_findings(settings, _effective_exists)

        persistent: set[tuple] = set()
        for identity, count in candidate.items():
            if count <= baseline.get(identity, 0):
                persistent.add(identity)
                continue
            kind, name = identity[3], identity[4]
            label = _DEFINITION_KIND_LABELS[kind]
            if definition_being_deleted and self.definition_kind == kind and self.definition_name == name:
                errors.append(f"Cannot remove {label.lower()} '{name}' because it is active in running-config.")
                if kind == "reference":
                    errors.append(
                        f"Deselect it with 'no reference {name}' under 'running-config', then commit again."
                    )
            else:
                errors.append(f"{label} '{name}' does not exist.")

        if definition_being_deleted and not _DEFINITION_DELETABILITY_CHECKS[self.definition_kind](
            self.definition_name, self.lab_root
        ):
            # Checked up front, before any write: a deletion that would be
            # refused by delete_*() must never be preceded by a settings write.
            errors.append(f"{_DEFINITION_KIND_LABELS[self.definition_kind]} '{self.definition_name}' cannot be deleted.")

        if errors:
            raise CommitValidationError(list(dict.fromkeys(errors)))

        # Write order (the two files are not one transaction; each write is
        # individually atomic). A deletion writes settings.yaml FIRST: the
        # validated candidate no longer selects the definition, so a failure
        # after the settings write leaves only an unreferenced file (retry
        # finishes the delete), never a persisted selection of a deleted
        # definition. A create/edit writes the definition FIRST, since the
        # new settings may select it.
        written_target = None
        settings_written = False

        def _write_settings() -> bool:
            if not self.settings_dirty():
                return False
            self.settings_candidate = normalized_settings(self.settings_candidate)
            lab.write_settings(self.settings_candidate, self.lab_root)
            self.committed_settings = copy.deepcopy(self.settings_candidate)
            return True

        if definition_being_deleted:
            settings_written = _write_settings()
            _DEFINITION_DELETERS[self.definition_kind](self.definition_name, self.lab_root)
            written_target = (self.definition_kind, self.definition_name, "deleted")
            self.definition_kind = None
            self.definition_name = None
            self.definition_original = None
            self.definition_candidate = None
            self._reset_mutation_tracking()
        else:
            if definition_being_written:
                _DEFINITION_WRITERS[self.definition_kind](self.definition_name, self.definition_candidate, self.lab_root)
                self.definition_original = copy.deepcopy(self.definition_candidate)
                written_target = (self.definition_kind, self.definition_name, "saved")
            self._reset_mutation_tracking()
            settings_written = _write_settings()

        # Sorted + deduplicated (identity is unique per source/field/target),
        # so warnings are deterministic. Target type and name only: no
        # definition content is ever echoed.
        warning_targets = sorted({(identity[3], identity[4]) for identity in persistent})
        self.last_commit_report = CommitReport(
            definition=written_target,
            settings_written=settings_written,
            warnings=tuple(f"{kind.replace('_', '-')} '{name}'" for kind, name in warning_targets),
        )
        return True
