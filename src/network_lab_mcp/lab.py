# SPDX-License-Identifier: Apache-2.0
"""Loading of Network Lab MCP lab data: running-config selection, topology,
access-info, principles, scenarios, and references.

Lab YAML is intentionally re-read from disk on every call instead of being
cached at process startup, so that editing lab/settings.yaml (or any
committed definition file) takes effect on the next tool call without
restarting the MCP server.

Five kinds of data are deliberately kept separate (see README.md and
docs/architecture.md for the full model):

- running-config (lab/settings.yaml): which topology/scenario/references MCP
  currently uses. A *selection*, not a definition.
- access-info (lab/access-info/*.yaml): private device connection data
  (address/transport/port/username/password). Never exposed to Claude.
- topology (lab/topologies/*.yaml): safe logical topology (devices, device
  type, links). Exposed to Claude via get_active_topology().
- scenario (lab/scenarios/*.yaml): what Claude should do.
- reference (lab/references/*.yaml): reusable knowledge for Claude.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Any, Optional

import yaml

from network_lab_mcp import terminal


class LabConfigError(Exception):
    """A clear, user-facing error in the lab configuration or lab data."""


def find_lab_root() -> Path:
    """Resolve the lab/ directory owned by this repository checkout.

    Only a local editable installation is supported, so the lab root is
    always the sibling lab/ directory of the source checkout providing this
    module. This must not depend on the caller's current working directory,
    since Claude Code is normally started from a separate task workspace.
    """
    package_dir = Path(__file__).resolve().parent
    repo_root = package_dir.parent.parent
    lab_root = repo_root / "lab"
    if not lab_root.is_dir():
        raise LabConfigError(
            f"Lab root not found at '{lab_root}'. Network Lab MCP requires a local "
            "editable installation ('pip install -e .') where the repository "
            "checkout owns the lab/ directory. Non-editable/wheel installation "
            "is not a supported configuration."
        )
    return lab_root


def _load_yaml(path: Path, what: str) -> Any:
    if not path.is_file():
        raise LabConfigError(f"{what} not found at '{path}'.")
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise LabConfigError(f"{what} at '{path}' is not valid YAML: {exc}") from exc
    if data is None:
        raise LabConfigError(f"{what} at '{path}' is empty.")
    return data


# --------------------------------------------------------------------------
# running-config (lab/settings.yaml): definition selection used by MCP
# --------------------------------------------------------------------------


def read_settings(lab_root: Path | None = None) -> dict:
    """Load the committed running-config (lab/settings.yaml) from disk."""
    lab_root = lab_root or find_lab_root()
    settings_path = lab_root / "settings.yaml"
    example_path = lab_root / "settings.example.yaml"
    if not settings_path.is_file():
        raise LabConfigError(
            f"Local settings file not found at '{settings_path}'. Create it once "
            f"by copying the tracked template, e.g.: cp {example_path} {settings_path}"
        )
    settings = _load_yaml(settings_path, "Settings")
    if not isinstance(settings, dict):
        raise LabConfigError(f"Settings at '{settings_path}' must be a YAML mapping.")
    return settings


def get_active_topology_name(settings: dict) -> str:
    name = settings.get("active_topology")
    if not name or not isinstance(name, str):
        raise LabConfigError("Settings is missing a valid 'active_topology' value.")
    return name


def get_active_scenario_name(settings: dict) -> str:
    name = settings.get("active_scenario")
    if not name or not isinstance(name, str):
        raise LabConfigError("Settings is missing a valid 'active_scenario' value.")
    return name


def get_active_reference_names(settings: dict) -> list[str]:
    names = settings.get("active_references") or []
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise LabConfigError("Settings 'active_references' must be a list of strings.")
    return names


def get_active_access_info_name(settings: dict) -> Optional[str]:
    """Return the selected access-info name, or None.

    Unlike active_topology/active_scenario, no access-info selection is a
    legitimate, fail-closed state (see terminal_open()/get_device() below),
    not an error -- a settings.yaml written before this field existed is
    still valid and simply has no access-info selected."""
    name = settings.get("active_access_info")
    if not name:
        return None
    if not isinstance(name, str):
        raise LabConfigError("Settings 'active_access_info' must be a string.")
    return name


# --------------------------------------------------------------------------
# device.type: the fixed enum shared by topology and access-info
# --------------------------------------------------------------------------


DEVICE_TYPES: dict[str, str] = {
    "iosxr": "Cisco IOS XR",
    "iosxe": "Cisco IOS XE",
    # Classic Cisco IOS -- explicitly its own type, never a
    # compatibility label under `iosxe`. `ios` is the canonical name; do
    # not add variants like `classic-ios`/`ios15`/`cisco-ios`.
    "ios": "Cisco IOS",
    "nxos": "Cisco NX-OS",
    # Not a network device topology discovery runs CDP/LLDP against. `host`
    # is a normal registered topology node -- discovery is intentionally
    # skipped for it, not an error/"unsupported type" case.
    "host": "Generic host / endpoint",
}


def normalize_device_type(value: str) -> str:
    """Resolve `value` against the fixed DEVICE_TYPES enum and return the
    canonical lowercase keyword.

    Accepts an exact case-insensitive match or an unambiguous prefix
    abbreviation (mirroring fixed-keyword abbreviation elsewhere in the
    grammar); raises LabConfigError for an unknown or ambiguous value. This
    is the single validation primitive for `device.type`, shared by the
    CLI's grammar-level `type` argument, topology YAML validation, and
    access-info YAML validation -- none of them duplicates this logic.
    Topology discovery dispatches platform-specific CDP/LLDP commands and
    parsers based on this field, so an unrecognized value must never reach a
    CLI candidate or committed YAML."""
    lowered = value.lower()
    if lowered in DEVICE_TYPES:
        return lowered
    matches = [key for key in DEVICE_TYPES if key.startswith(lowered)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise LabConfigError(f"Ambiguous device type '{value}'. Matches: {', '.join(sorted(matches))}.")
    raise LabConfigError(f"Invalid device type '{value}'. Expected one of: {', '.join(sorted(DEVICE_TYPES))}.")


def validate_device_types(context_label: str, devices: dict) -> None:
    """Ensure each device's optional 'type' field, when present, is one of
    DEVICE_TYPES. A missing/empty 'type' is not itself an error here.

    Shared by topology and access-info validation (`context_label` is only
    used in error messages), so the enum's matching rules are never
    duplicated between the two."""
    for device_name, device in devices.items():
        raw_type = (device or {}).get("type")
        if raw_type in (None, ""):
            continue
        if not isinstance(raw_type, str):
            raise LabConfigError(f"{context_label} device '{device_name}' has an invalid 'type' value.")
        try:
            normalize_device_type(raw_type)
        except LabConfigError as exc:
            raise LabConfigError(f"{context_label} device '{device_name}': {exc}") from exc


# --------------------------------------------------------------------------
# topology (lab/topologies/*.yaml): safe logical topology, exposed to Claude
# --------------------------------------------------------------------------


# Private device-access fields that must live in access-info, never in
# topology, since topology is exposed to Claude via get_active_topology().
TOPOLOGY_ACCESS_FIELDS = ("address", "transport", "port", "username", "password")


def validate_topology_device_names(topology_name: str, devices: dict) -> None:
    """Ensure every device name maps to a safe and unambiguous terminal session name."""
    if not isinstance(devices, dict):
        raise LabConfigError(
            f"Topology '{topology_name}' has an invalid 'devices' section; expected a mapping."
        )
    seen_sessions: dict[str, str] = {}
    for device_name in devices:
        if not isinstance(device_name, str) or not device_name.strip():
            raise LabConfigError(f"Topology '{topology_name}' has a device with an empty or invalid name.")
        try:
            session_name = terminal.derive_production_session_name(device_name)
        except terminal.TerminalError as exc:
            raise LabConfigError(
                f"Topology '{topology_name}' device '{device_name}' cannot be mapped "
                f"to a safe terminal session name: {exc}"
            ) from exc
        collision = seen_sessions.get(session_name)
        if collision is not None and collision != device_name:
            raise LabConfigError(
                f"Topology '{topology_name}' devices '{collision}' and '{device_name}' "
                f"both resolve to the terminal session '{session_name}'."
            )
        seen_sessions[session_name] = device_name


def validate_topology_no_access_fields(topology_name: str, devices: dict) -> None:
    """Reject private device-access fields in topology data.

    Topology is safe logical data exposed to Claude via
    get_active_topology(); address/transport/port/username/password belong
    in access-info instead, which MCP never exposes."""
    for device_name, device in devices.items():
        present = sorted(field for field in TOPOLOGY_ACCESS_FIELDS if (device or {}).get(field) not in (None, ""))
        if present:
            raise LabConfigError(
                f"Topology '{topology_name}' device '{device_name}' contains private access field(s) "
                f"{', '.join(present)}; these belong in an access-info definition, not topology."
            )


def validate_topology_links(topology_name: str, devices: dict, links: Any) -> None:
    """Validate the optional `links` list against the schema already used
    by the topology renderer and Discovery's link builder (each entry: 'a'
    /'b' endpoint device IDs, plus an optional 'a_interface'/'b_interface').

    `links` omitted entirely is valid (None is not the same as an invalid
    container type). When present it must be a list; each entry must be a
    mapping whose 'a'/'b' are non-empty strings naming an existing device
    in this same topology, and whose 'a_interface'/'b_interface' -- when
    present -- are non-empty strings. An exact duplicate link (the same
    unordered pair of (device, interface) endpoints) is rejected; Discovery
    reconciliation already avoids creating one, but a hand-edited or
    external-editor duplicate is equally invalid."""
    if links is None:
        return
    if not isinstance(links, list):
        raise LabConfigError(f"Topology '{topology_name}' has an invalid 'links' section; expected a list.")
    seen_keys = set()
    for index, link in enumerate(links):
        if not isinstance(link, dict):
            raise LabConfigError(f"Topology '{topology_name}' link #{index + 1} must be a mapping.")
        endpoint_keys = {}
        for side in ("a", "b"):
            value = link.get(side)
            if not isinstance(value, str) or not value.strip():
                raise LabConfigError(
                    f"Topology '{topology_name}' link #{index + 1} is missing a non-empty '{side}'."
                )
            if value not in devices:
                raise LabConfigError(
                    f"Topology '{topology_name}' link #{index + 1} references unknown device "
                    f"'{value}' in '{side}'."
                )
            interface_field = f"{side}_interface"
            interface_value = link.get(interface_field)
            if interface_value is not None and (
                not isinstance(interface_value, str) or not interface_value.strip()
            ):
                raise LabConfigError(
                    f"Topology '{topology_name}' link #{index + 1} has an invalid '{interface_field}'; "
                    "expected a non-empty string."
                )
            endpoint_keys[side] = (value, interface_value)
        key = tuple(sorted((endpoint_keys["a"], endpoint_keys["b"])))
        if key in seen_keys:
            raise LabConfigError(f"Topology '{topology_name}' link #{index + 1} duplicates an earlier link.")
        seen_keys.add(key)


_INTERFACE_L3_FIELDS = ("ipv4_address", "vrf")


def validate_topology_interfaces(topology_name: str, devices: dict) -> None:
    """Validate each device's optional 'interfaces' mapping (L3
    enrichment): a stable, directly observed IPv4 address + VRF per
    interface -- never operational state (up/down), never a prefix length
    (deliberately out of scope), and never link/connectivity
    data (links remain the sole source of connectivity). Omitted entirely,
    or an empty mapping, is valid -- existing topology files with no
    'interfaces' key at all need no migration."""
    for device_name, device in devices.items():
        interfaces = (device or {}).get("interfaces")
        if interfaces is None:
            continue
        if not isinstance(interfaces, dict):
            raise LabConfigError(
                f"Topology '{topology_name}' device '{device_name}' has an invalid 'interfaces' "
                "section; expected a mapping."
            )
        for interface_name, fields in interfaces.items():
            if not isinstance(interface_name, str) or not interface_name.strip():
                raise LabConfigError(
                    f"Topology '{topology_name}' device '{device_name}' has an interface with an "
                    "empty or invalid name."
                )
            if not isinstance(fields, dict):
                raise LabConfigError(
                    f"Topology '{topology_name}' device '{device_name}' interface '{interface_name}' "
                    "must be a mapping."
                )
            unknown = sorted(set(fields) - set(_INTERFACE_L3_FIELDS))
            if unknown:
                raise LabConfigError(
                    f"Topology '{topology_name}' device '{device_name}' interface '{interface_name}' "
                    f"has unsupported field(s): {', '.join(unknown)}."
                )
            for key in _INTERFACE_L3_FIELDS:
                value = fields.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise LabConfigError(
                        f"Topology '{topology_name}' device '{device_name}' interface '{interface_name}' "
                        f"is missing a non-empty '{key}'."
                    )
            ipv4_address = fields["ipv4_address"]
            try:
                ipaddress.IPv4Address(ipv4_address)
            except ValueError as exc:
                raise LabConfigError(
                    f"Topology '{topology_name}' device '{device_name}' interface '{interface_name}' has "
                    f"an invalid 'ipv4_address' value '{ipv4_address}': {exc}"
                ) from exc


def validate_topology_data(name: str, data: Any) -> None:
    """Validate an in-memory topology mapping using the same rules `load_topology()`
    applies to a freshly loaded file.

    This is the single validation primitive shared by the MCP load path, the
    CLI commit path, and tests -- neither of the other callers
    duplicates these rules.
    """
    if not isinstance(data, dict):
        raise LabConfigError(f"Topology '{name}' data must be a YAML mapping.")
    devices = data.get("devices") or {}
    validate_topology_device_names(name, devices)
    validate_topology_no_access_fields(name, devices)
    validate_device_types(f"Topology '{name}'", devices)
    validate_topology_links(name, devices, data.get("links"))
    validate_topology_interfaces(name, devices)


def load_topology(name: str, lab_root: Path | None = None) -> dict:
    lab_root = lab_root or find_lab_root()
    path = lab_root / "topologies" / f"{name}.yaml"
    topology = _load_yaml(path, f"Topology '{name}'")
    validate_topology_data(name, topology)
    return topology


def write_topology(name: str, data: dict, lab_root: Path | None = None) -> None:
    """Persist a topology mapping to lab/topologies/<name>.yaml atomically.

    Validates with the same `validate_topology_data()` primitive used to load
    topologies, so an invalid candidate (including one still carrying
    private access fields) can never reach disk.
    """
    lab_root = lab_root or find_lab_root()
    validate_topology_data(name, data)
    _atomic_write_yaml(lab_root / "topologies" / f"{name}.yaml", data)


def list_topology_names(lab_root: Path | None = None) -> list[str]:
    """List the topology names available on disk (exact stored/file case)."""
    lab_root = lab_root or find_lab_root()
    return _list_yaml_stems(lab_root / "topologies")


def topology_exists(name: str, lab_root: Path | None = None) -> bool:
    lab_root = lab_root or find_lab_root()
    return (lab_root / "topologies" / f"{name}.yaml").is_file()


def _stored_definition_is_deletable(name: str, directory: Path, list_names: list[str]) -> bool:
    """True if `name` is an exact, enumerated stored-definition name (from
    the caller's own list_*_names(), the same SSOT each definition kind's
    own dynamic completion already reads) whose file is a regular,
    non-symlink file directly confined under `directory`. Shared by every
    kind's `no <kind> <name>` candidate-creation check (cli/config.py) and
    delete_*() below, so a symlinked or path-unsafe entry is rejected as
    early as candidate creation, not only at commit -- mirroring the same
    exact-enumeration-match / path-confinement discipline already used
    for terminal log deletion (terminal.py). A small shared primitive,
    not a generic definition framework: each kind still has its own named
    `<kind>_is_deletable()` / `delete_<kind>()` pair below."""
    if name not in list_names:
        return False
    path = directory / f"{name}.yaml"
    resolved_dir = directory.resolve()
    resolved_path = path.resolve()
    return resolved_path.parent == resolved_dir and not path.is_symlink() and path.is_file()


def _delete_stored_definition(name: str, directory: Path, list_names: list[str], kind_label: str) -> None:
    if not _stored_definition_is_deletable(name, directory, list_names):
        raise LabConfigError(f"{kind_label} '{name}' does not exist.")
    (directory / f"{name}.yaml").unlink()


def topology_is_deletable(name: str, lab_root: Path | None = None) -> bool:
    lab_root = lab_root or find_lab_root()
    return _stored_definition_is_deletable(name, lab_root / "topologies", list_topology_names(lab_root))


def delete_topology(name: str, lab_root: Path | None = None) -> None:
    """Permanently remove a committed topology definition file. Used only
    by the CLI's `no topology <name>` candidate-deletion commit path
    (cli/config.py's CliSession.commit()) -- never recursive."""
    lab_root = lab_root or find_lab_root()
    _delete_stored_definition(name, lab_root / "topologies", list_topology_names(lab_root), "Topology")


# --------------------------------------------------------------------------
# access-info (lab/access-info/*.yaml): private device access, never exposed
# --------------------------------------------------------------------------


# Single-hop OpenSSH ProxyJump only (see terminal.py): a jump host is always
# a generic endpoint, never a network device, and ProxyJump is SSH-only.
JUMP_HOST_TYPE = "host"


def validate_jump_hosts(context_label: str, jump_hosts: dict) -> None:
    """Validate a jump_hosts mapping: each entry must resolve (through the
    shared device.type SSOT, so abbreviations/case are handled consistently)
    to exactly 'host' -- never a network-device type -- and, if it sets a
    transport, that transport must be 'ssh', since native OpenSSH ProxyJump
    is SSH-only. Reuses normalize_device_type() rather than maintaining a
    separate jump-host type enum."""
    if not isinstance(jump_hosts, dict):
        raise LabConfigError(f"{context_label} has an invalid 'jump_hosts' section; expected a mapping.")
    for jump_host_name, jump_host in jump_hosts.items():
        if not isinstance(jump_host_name, str) or not jump_host_name.strip():
            raise LabConfigError(f"{context_label} has a jump host with an empty or invalid name.")
        jump_host = jump_host or {}
        raw_type = jump_host.get("type")
        if raw_type not in (None, ""):
            try:
                normalized_type = normalize_device_type(str(raw_type))
            except LabConfigError as exc:
                raise LabConfigError(f"{context_label} jump host '{jump_host_name}': {exc}") from exc
            if normalized_type != JUMP_HOST_TYPE:
                raise LabConfigError(
                    f"{context_label} jump host '{jump_host_name}' must have type '{JUMP_HOST_TYPE}', "
                    f"not '{normalized_type}'."
                )
        raw_transport = jump_host.get("transport")
        if raw_transport not in (None, "") and str(raw_transport).lower() != "ssh":
            raise LabConfigError(
                f"{context_label} jump host '{jump_host_name}' must use transport 'ssh' for ProxyJump."
            )


def validate_device_jump_host_references(context_label: str, devices: dict, jump_hosts: dict) -> None:
    """A device's optional 'jump_host' must name an existing jump host, and
    single-hop OpenSSH ProxyJump requires the device's own transport to be
    ssh too -- a telnet device can never use a jump host."""
    for device_name, device in devices.items():
        device = device or {}
        jump_host_ref = device.get("jump_host")
        if not jump_host_ref:
            continue
        if jump_host_ref not in jump_hosts:
            raise LabConfigError(
                f"{context_label} device '{device_name}' references unknown jump host '{jump_host_ref}'."
            )
        raw_transport = device.get("transport")
        if raw_transport and str(raw_transport).lower() != "ssh":
            raise LabConfigError(
                f"{context_label} device '{device_name}' uses jump_host but transport is not 'ssh'."
            )


def validate_access_info_data(name: str, data: Any) -> None:
    """Validate an in-memory access-info mapping. Device names only need
    basic sanity here (non-empty strings) -- unlike topology, access-info
    device keys do not by themselves create terminal sessions, so they are
    not required to pass the topology session-name-collision check. The
    device.type enum is still validated through the same SSOT as topology.

    Also validates the optional single-hop jump_hosts mapping and any
    device.jump_host reference into it (see validate_jump_hosts() /
    validate_device_jump_host_references())."""
    if not isinstance(data, dict):
        raise LabConfigError(f"Access information '{name}' data must be a YAML mapping.")
    devices = data.get("devices") or {}
    if not isinstance(devices, dict):
        raise LabConfigError(f"Access information '{name}' has an invalid 'devices' section; expected a mapping.")
    for device_name in devices:
        if not isinstance(device_name, str) or not device_name.strip():
            raise LabConfigError(f"Access information '{name}' has a device with an empty or invalid name.")
    validate_device_types(f"Access information '{name}'", devices)
    jump_hosts = data.get("jump_hosts") or {}
    validate_jump_hosts(f"Access information '{name}'", jump_hosts)
    validate_device_jump_host_references(f"Access information '{name}'", devices, jump_hosts)


def load_access_info(name: str, lab_root: Path | None = None) -> dict:
    lab_root = lab_root or find_lab_root()
    path = lab_root / "access-info" / f"{name}.yaml"
    data = _load_yaml(path, f"Access information '{name}'")
    validate_access_info_data(name, data)
    return data


def write_access_info(name: str, data: dict, lab_root: Path | None = None) -> None:
    """Persist an access-info mapping to lab/access-info/<name>.yaml atomically."""
    lab_root = lab_root or find_lab_root()
    validate_access_info_data(name, data)
    _atomic_write_yaml(lab_root / "access-info" / f"{name}.yaml", data)


def list_access_info_names(lab_root: Path | None = None) -> list[str]:
    lab_root = lab_root or find_lab_root()
    return _list_yaml_stems(lab_root / "access-info")


def access_info_exists(name: str, lab_root: Path | None = None) -> bool:
    lab_root = lab_root or find_lab_root()
    return (lab_root / "access-info" / f"{name}.yaml").is_file()


def access_info_is_deletable(name: str, lab_root: Path | None = None) -> bool:
    lab_root = lab_root or find_lab_root()
    return _stored_definition_is_deletable(name, lab_root / "access-info", list_access_info_names(lab_root))


def delete_access_info(name: str, lab_root: Path | None = None) -> None:
    """Permanently remove a committed access-info definition file. Used
    only by the CLI's `no access-info <name>` candidate-deletion commit
    path (cli/config.py's CliSession.commit()) -- never recursive. Only
    the definition *name* is ever used here; its contents (which may
    include plaintext credentials) are never read, rendered, or logged
    by this function."""
    lab_root = lab_root or find_lab_root()
    _delete_stored_definition(
        name, lab_root / "access-info", list_access_info_names(lab_root), "Access information"
    )




# --------------------------------------------------------------------------
# scenario / reference: schema intentionally not fixed yet (see
# docs/scenario_format.md) -- minimal "valid YAML mapping" validation only
# --------------------------------------------------------------------------


def _validate_mapping(kind: str, name: str, data: Any) -> None:
    if not isinstance(data, dict):
        raise LabConfigError(f"{kind} '{name}' data must be a YAML mapping.")


def validate_scenario_data(name: str, data: Any) -> None:
    _validate_mapping("Scenario", name, data)


def validate_reference_data(name: str, data: Any) -> None:
    _validate_mapping("Reference", name, data)


def load_principles(lab_root: Path | None = None) -> Any:
    lab_root = lab_root or find_lab_root()
    return _load_yaml(lab_root / "principles.yaml", "Principles")


def load_scenario(name: str, lab_root: Path | None = None) -> Any:
    lab_root = lab_root or find_lab_root()
    data = _load_yaml(lab_root / "scenarios" / f"{name}.yaml", f"Scenario '{name}'")
    _validate_mapping("Scenario", name, data)
    return data


def write_scenario(name: str, data: dict, lab_root: Path | None = None) -> None:
    lab_root = lab_root or find_lab_root()
    validate_scenario_data(name, data)
    _atomic_write_yaml(lab_root / "scenarios" / f"{name}.yaml", data)


def list_scenario_names(lab_root: Path | None = None) -> list[str]:
    lab_root = lab_root or find_lab_root()
    return _list_yaml_stems(lab_root / "scenarios")


def scenario_exists(name: str, lab_root: Path | None = None) -> bool:
    lab_root = lab_root or find_lab_root()
    return (lab_root / "scenarios" / f"{name}.yaml").is_file()


def scenario_is_deletable(name: str, lab_root: Path | None = None) -> bool:
    lab_root = lab_root or find_lab_root()
    return _stored_definition_is_deletable(name, lab_root / "scenarios", list_scenario_names(lab_root))


def delete_scenario(name: str, lab_root: Path | None = None) -> None:
    """Permanently remove a committed scenario definition file. Used only
    by the CLI's `no scenario <name>` candidate-deletion commit path
    (cli/config.py's CliSession.commit()) -- never recursive."""
    lab_root = lab_root or find_lab_root()
    _delete_stored_definition(name, lab_root / "scenarios", list_scenario_names(lab_root), "Scenario")


def load_reference(name: str, lab_root: Path | None = None) -> Any:
    lab_root = lab_root or find_lab_root()
    data = _load_yaml(lab_root / "references" / f"{name}.yaml", f"Reference '{name}'")
    _validate_mapping("Reference", name, data)
    return data


def load_references(names: list[str], lab_root: Path | None = None) -> list[Any]:
    lab_root = lab_root or find_lab_root()
    return [load_reference(name, lab_root) for name in names]


def write_reference(name: str, data: dict, lab_root: Path | None = None) -> None:
    lab_root = lab_root or find_lab_root()
    validate_reference_data(name, data)
    _atomic_write_yaml(lab_root / "references" / f"{name}.yaml", data)


def list_reference_names(lab_root: Path | None = None) -> list[str]:
    lab_root = lab_root or find_lab_root()
    return _list_yaml_stems(lab_root / "references")


def reference_exists(name: str, lab_root: Path | None = None) -> bool:
    lab_root = lab_root or find_lab_root()
    return (lab_root / "references" / f"{name}.yaml").is_file()


def reference_is_deletable(name: str, lab_root: Path | None = None) -> bool:
    lab_root = lab_root or find_lab_root()
    return _stored_definition_is_deletable(name, lab_root / "references", list_reference_names(lab_root))


def delete_reference(name: str, lab_root: Path | None = None) -> None:
    """Permanently remove a committed reference definition file. Used only
    by the CLI's `no reference <name>` candidate-deletion commit path
    (cli/config.py's CliSession.commit()) -- never recursive. Distinct
    from running-config's own `no reference <name>` (removes the name
    from the active reference *selection*, never deletes the stored
    file)."""
    lab_root = lab_root or find_lab_root()
    _delete_stored_definition(name, lab_root / "references", list_reference_names(lab_root), "Reference")


# --------------------------------------------------------------------------
# MCP-facing reads: committed running-config selection only, never candidate
# state, never access-info
# --------------------------------------------------------------------------


def get_active_topology() -> dict:
    """Reload the running-config selection and the active topology from disk
    and return them together. Never includes access-info: this is the safe
    logical topology Claude is allowed to see."""
    lab_root = find_lab_root()
    settings = read_settings(lab_root)
    topology_name = get_active_topology_name(settings)
    topology = load_topology(topology_name, lab_root)
    return {"active_topology": topology_name, "topology": topology}


def _catalog(directory: Path, names: list[str]) -> list[dict]:
    """Metadata-only catalog ([{name, description}], sorted by name) of the
    stored definitions in `directory`. Filename stem is the identity (same
    SSOT as list_*_names()); `description` is the definition's own string
    `description` field, or None when absent/unreadable -- a legacy or even
    malformed file never breaks the catalog. Symlinked/path-unsafe entries
    are left out. Never includes bodies."""
    entries = []
    for name in names:
        if not _stored_definition_is_deletable(name, directory, names):
            continue
        description = None
        try:
            data = yaml.safe_load((directory / f"{name}.yaml").read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("description"), str) and data["description"].strip():
            description = data["description"].strip()
        entries.append({"name": name, "description": description})
    return entries


def _inspect_definitions(requested: list[str] | None, directory: Path, names: list[str], load, kind_label: str) -> list[dict]:
    """Read-only bodies of explicitly requested stored definitions: only
    exact enumerated names (never paths) that are regular, non-symlink,
    confined files; duplicates dropped, requested order kept; any
    unknown/unsafe/malformed name fails the whole call closed."""
    out: list[dict] = []
    seen: set[str] = set()
    for name in requested or []:
        if name in seen:
            continue
        seen.add(name)
        if not _stored_definition_is_deletable(name, directory, names):
            raise LabConfigError(f"{kind_label} '{name}' does not exist.")
        out.append({"name": name, "content": load(name)})
    return out


def get_execution_instructions(
    inspect_scenarios: list[str] | None = None, inspect_references: list[str] | None = None
) -> dict:
    """Reload principles, the active scenario, and active references from
    disk, plus metadata-only catalogs of every stored scenario/reference.
    `inspect_scenarios`/`inspect_references` optionally request the full
    content of specific stored (possibly non-active) definitions, read-only:
    nothing is activated, written, or persisted."""
    lab_root = find_lab_root()
    settings = read_settings(lab_root)
    scenario_name = get_active_scenario_name(settings)
    reference_names = get_active_reference_names(settings)
    principles = load_principles(lab_root)
    scenario = load_scenario(scenario_name, lab_root)
    references = load_references(reference_names, lab_root)
    scenario_names = list_scenario_names(lab_root)
    reference_list = list_reference_names(lab_root)
    return {
        "principles": principles,
        "scenario": {"name": scenario_name, "content": scenario},
        "references": [
            {"name": name, "content": content} for name, content in zip(reference_names, references)
        ],
        "available_scenarios": _catalog(lab_root / "scenarios", scenario_names),
        "available_references": _catalog(lab_root / "references", reference_list),
        "inspected_scenarios": _inspect_definitions(
            inspect_scenarios, lab_root / "scenarios", scenario_names, lambda n: load_scenario(n, lab_root), "Scenario"
        ),
        "inspected_references": _inspect_definitions(
            inspect_references, lab_root / "references", reference_list, lambda n: load_reference(n, lab_root), "Reference"
        ),
    }


def verify_device_in_active_topology(device_name: str) -> None:
    """Fail closed (LabConfigError) unless `device_name` is present in the
    currently active topology -- reloaded from disk on every call, exactly
    like get_active_topology()/get_device().

    Used by terminal_send()/terminal_read() so that actively sending to or
    reading from a device stays scoped to the active topology for the
    whole lifetime of a session, not just at the moment terminal_open()
    created it: an already-open session survives an active-topology
    change (tmux sessions are persistent), so without this check
    terminal_open()'s own active-topology gate would be trivially bypassed
    simply by reusing a session opened before the topology changed.

    Deliberately does not affect terminal_list() (which continues to show
    every managed session, so a session for a device no longer in the
    active topology remains visible) or terminal_close() (which can always
    close such a session) -- otherwise a stale session could become
    impossible to discover or clean up."""
    active = get_active_topology()
    topology_devices = active["topology"].get("devices") or {}
    if device_name not in topology_devices:
        raise LabConfigError(
            f"Device '{device_name}' is not present in active topology '{active['active_topology']}'."
        )


def get_device(device_name: str) -> tuple[str, dict]:
    """Verify `device_name` exists in the active topology, then resolve its
    private access information from the *selected* access-info definition
    only (running-config's `active_access_info` -- see
    get_active_access_info_name()).

    Returns (topology_name, resolved_access_dict). `resolved_access_dict` is
    never topology data itself, since terminal connectivity needs
    address/transport/username/password, which topology no longer carries;
    if the device references a jump host, the resolved jump host's own
    connection data is attached under the 'jump_host_config' key (kept
    entirely separate from the device's own credentials -- see
    terminal._build_transport_command()).

    Raises LabConfigError (fail closed, never a silent guess) when:
    - the device is not present in the active topology;
    - no access-info is selected in running-config;
    - the selected access-info definition does not exist;
    - the device is not present in the selected access-info definition
      (no fallback search through any other access-info file);
    - the topology's and access-info's device.type disagree once both are
      normalized through the shared DEVICE_TYPES SSOT;
    - the device references a jump host that does not exist in the same
      access-info definition (structurally impossible for a *committed*
      file, since validate_access_info_data() already rejects that, but a
      defensive check costs nothing).

    Reads `settings.yaml` exactly once and resolves both the active
    topology name and the active access-info name from that single
    snapshot -- never two independent reads (one via a helper, one
    directly), which could otherwise straddle a concurrent human-CLI
    `commit` and resolve a topology from one running-config selection and
    access-info from a different, later one.
    """
    lab_root = find_lab_root()
    settings = read_settings(lab_root)
    topology_name = get_active_topology_name(settings)
    topology = load_topology(topology_name, lab_root)
    topology_devices = topology.get("devices") or {}
    topology_device = topology_devices.get(device_name)
    if topology_device is None:
        raise LabConfigError(f"Device '{device_name}' is not present in active topology '{topology_name}'.")

    access_info_name = get_active_access_info_name(settings)
    if not access_info_name:
        raise LabConfigError("No access-info is selected in running-config.")
    if not access_info_exists(access_info_name, lab_root):
        raise LabConfigError(f"Selected access-info '{access_info_name}' does not exist.")
    access_data = load_access_info(access_info_name, lab_root)
    access_devices = access_data.get("devices") or {}
    access_device = access_devices.get(device_name)
    if access_device is None:
        raise LabConfigError(
            f"Device '{device_name}' is not present in access-info '{access_info_name}'."
        )
    access_device = dict(access_device)

    topology_type = (topology_device or {}).get("type")
    access_type = access_device.get("type")
    if topology_type and access_type:
        if normalize_device_type(str(topology_type)) != normalize_device_type(str(access_type)):
            raise LabConfigError(
                f"Device type mismatch for '{device_name}' between topology and access information."
            )

    jump_host_ref = access_device.get("jump_host")
    if jump_host_ref:
        jump_hosts = access_data.get("jump_hosts") or {}
        jump_host = jump_hosts.get(jump_host_ref)
        if jump_host is None:
            raise LabConfigError(
                f"Device '{device_name}' references unknown jump host '{jump_host_ref}'."
            )
        access_device["jump_host_config"] = dict(jump_host)

    return topology_name, access_device


# --------------------------------------------------------------------------
# Generic listing / atomic write helpers
# --------------------------------------------------------------------------


def _list_yaml_stems(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.yaml"))


def _atomic_write_yaml(path: Path, data: Any) -> None:
    """Write YAML atomically: write to a sibling temp file, flush, then replace.

    This avoids ever leaving a partially written committed YAML file behind,
    and avoids touching the target file at all when the caller decides not to
    write (see the write_*() callers in cli/config.py, which only call this
    when a scope is actually dirty).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, default_flow_style=False, allow_unicode=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def write_settings(settings: dict, lab_root: Path | None = None) -> None:
    """Persist a running-config selection mapping to lab/settings.yaml atomically."""
    lab_root = lab_root or find_lab_root()
    _atomic_write_yaml(lab_root / "settings.yaml", settings)
