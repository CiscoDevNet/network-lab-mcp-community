"""Command grammar single source of truth for the Network Lab CLI.

For each CLI mode (EXEC, global configuration, running-config selection,
topology/access-info/scenario/reference definition editing, and the nested
topology-device / access-info-device submodes) this module builds one trie
of fixed keywords and argument slots. The same trie is used for parsing,
unique fixed-keyword abbreviation resolution, Tab/Ctrl-I completion,
context-sensitive `?` help, dynamic candidate lookup, `<cr>` eligibility, and
invalid/incomplete/ambiguous command reporting. There is no separate parser
table, completion table, or help table that could drift out of sync with
this one.

Fixed CLI keywords (e.g. "configure", "topology", "transport") are matched
case-insensitively and support unique-prefix abbreviation, like IOS XR.
Object identifiers (topology/scenario/reference/access-info/device names)
are matched case-sensitively and are never abbreviated; that is enforced by
never attempting keyword-style matching against them at all -- an identifier
argument accepts whatever token the operator typed and defers existence
checks to the caller (see cli/config.py).

This module owns no mutable candidate/session state. Dynamic completion
providers are pure functions of an explicit, read-only CliContext supplied
by the caller (cli/main.py) on every call, so this module never imports
cli/config.py and cli/config.py never needs to import this module's runtime
state -- there is no grammar/config circular dependency. It does import
network_lab_mcp.lab for the device.type enum (DEVICE_TYPES /
normalize_device_type()), which is the single validation primitive for that
enum -- this grammar never maintains its own separate copy of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from network_lab_mcp import lab

# --------------------------------------------------------------------------
# Read-only runtime context for dynamic providers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CliContext:
    """Read-only snapshot of CLI state used only for dynamic completion/help.

    Built fresh by cli/main.py before every parse/completion/help call.
    Dynamic providers must treat every field as read-only.
    """

    topology_names: tuple[str, ...] = ()
    scenario_names: tuple[str, ...] = ()
    reference_names: tuple[str, ...] = ()
    access_info_names: tuple[str, ...] = ()
    candidate_reference_names: tuple[str, ...] = ()
    topology_candidate_device_names: tuple[str, ...] = ()
    access_info_candidate_device_names: tuple[str, ...] = ()
    access_info_candidate_jump_host_names: tuple[str, ...] = ()
    # `show logging <device-id> <log-file>`: device IDs that have at least
    # one terminal log, and each one's own log filenames -- keyed so the
    # third-level argument's provider (which needs to know *which* device
    # was already typed) can look up just that device's files.
    log_device_ids: tuple[str, ...] = ()
    log_files_by_device: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # `show running-config reference [<name>]` (EXEC only): committed
    # active_references, in committed order -- never candidate state and
    # never every stored reference file.
    committed_active_reference_names: tuple[str, ...] = ()
    # `no <kind> <name>` (global configuration only): one field
    # per definition kind, candidate-aware -- every stored name of that
    # kind when no dirty definition candidate is open; only the one
    # already-dirty identity of that *same* kind if an edit/pending-
    # deletion of it is open (the one same-identity operation that
    # remains legal); nothing at all if a *different* kind (or a
    # different name of this kind) is dirty, or if the one open
    # definition is already pending deletion (see cli/main.py's
    # build_context()). Execution (CliSession.remove_definition()) is the
    # authoritative enforcement of this either way.
    no_topology_candidate_names: tuple[str, ...] = ()
    no_access_info_candidate_names: tuple[str, ...] = ()
    no_scenario_candidate_names: tuple[str, ...] = ()
    no_reference_candidate_names: tuple[str, ...] = ()
    # `config-running# <kind> <name>` selector arguments: the
    # stored names of that kind that are actually valid to *select* right
    # now -- every stored name, except one currently pending whole-
    # definition deletion in the (separate) definition-editing candidate
    # scope, which would otherwise let running-config reference a
    # definition about to become absent. Deliberately a separate field
    # from e.g. `topology_names` (used by global `topology <name>`
    # editing, where offering the pending-deleted name back is exactly
    # how its deletion gets cancelled -- see provide_no_topology_names()'s
    # docstring) rather than filtering that shared field itself.
    running_topology_names: tuple[str, ...] = ()
    running_access_info_names: tuple[str, ...] = ()
    running_scenario_names: tuple[str, ...] = ()
    running_reference_names: tuple[str, ...] = ()
    # `terminal monitor <device-id>` (EXEC only): every
    # device eligible to be monitored right now -- the committed active
    # topology's own devices, union'd with any device that already has an
    # existing production terminal session (so a session the AI opened
    # for a device outside the *current* topology selection, or one whose
    # topology selection changed since, stays a valid target), union'd
    # with any device that currently has an active Discovery bootstrap
    # session (a device being discovered for the first time may
    # not yet be in the committed topology at all -- see cli/main.py's
    # _monitor_terminal_target_names()). Deliberately committed-only for
    # the topology part: an uncommitted topology candidate's devices are
    # never offered here.
    monitor_terminal_device_ids: tuple[str, ...] = ()


# A provider receives the already-committed raw tokens of the command so
# far (e.g. for `show logging R1 <partial>`, `committed` is
# ("show", "logging", "R1")) in addition to the context and the partial
# token being completed -- needed by a provider whose candidates depend on
# an earlier argument in the *same* command (see provide_log_files) rather
# than on session-wide context. Existing providers ignore it.
Provider = Callable[[CliContext, str, tuple[str, ...]], list[str]]


@dataclass(frozen=True)
class ValidationOutcome:
    ok: bool
    normalized: Optional[str] = None
    error: Optional[str] = None


Validator = Callable[[str], ValidationOutcome]


def _ok(normalized: str) -> ValidationOutcome:
    return ValidationOutcome(True, normalized, None)


def _fail(message: str) -> ValidationOutcome:
    return ValidationOutcome(False, None, message)


def validate_freeform(value: str) -> ValidationOutcome:
    """Accept any token as-is. Used for object identifiers and free-form
    device fields (address/username/description); existence and semantic
    checks are the caller's responsibility, not the grammar's."""
    return _ok(value)


def validate_description(value: str) -> ValidationOutcome:
    """Running-config entry description text: required and non-empty after
    stripping surrounding whitespace (human-readable metadata only)."""
    text = value.strip()
    if not text:
        return _fail("Description must not be empty.")
    return _ok(text)


def validate_device_type(value: str) -> ValidationOutcome:
    """Reuses network_lab_mcp.lab.normalize_device_type() -- the single
    validation primitive for the fixed device-type enum, also applied to
    topology and access-info YAML on load/write, so this grammar never
    maintains its own separate copy of the allowed values or matching
    rules."""
    try:
        return _ok(lab.normalize_device_type(value))
    except lab.LabConfigError as exc:
        return _fail(str(exc))


def validate_transport(value: str) -> ValidationOutcome:
    lowered = value.lower()
    if lowered in ("ssh", "telnet"):
        return _ok(lowered)
    return _fail("Invalid transport. Expected ssh or telnet.")


def validate_jump_host_type(value: str) -> ValidationOutcome:
    """Jump hosts are always generic endpoints for native OpenSSH ProxyJump,
    never a network-device type. Reuses lab.normalize_device_type() for the
    case-insensitive/abbreviation matching (so 'HOST'/'Host'/'h' all still
    resolve), then narrows the accepted result to exactly 'host'."""
    try:
        normalized = lab.normalize_device_type(value)
    except lab.LabConfigError as exc:
        return _fail(str(exc))
    if normalized != lab.JUMP_HOST_TYPE:
        return _fail(f"Invalid jump-host type. Expected: {lab.JUMP_HOST_TYPE}.")
    return _ok(normalized)


def validate_jump_host_transport(value: str) -> ValidationOutcome:
    """Single-hop OpenSSH ProxyJump is SSH-only."""
    if value.lower() == "ssh":
        return _ok("ssh")
    return _fail("Invalid transport. Jump hosts support ssh only (required by OpenSSH ProxyJump).")


def validate_port(value: str) -> ValidationOutcome:
    if not value.isdigit():
        return _fail("Port must be between 1 and 65535.")
    port = int(value)
    if not (1 <= port <= 65535):
        return _fail("Port must be between 1 and 65535.")
    return _ok(str(port))


# --------------------------------------------------------------------------
# Dynamic providers (pure, read-only: provider(context, prefix) -> candidates)
# --------------------------------------------------------------------------


def provide_topology_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.topology_names if n.startswith(prefix)]


def provide_no_topology_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.no_topology_candidate_names if n.startswith(prefix)]


def provide_no_access_info_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.no_access_info_candidate_names if n.startswith(prefix)]


def provide_no_scenario_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.no_scenario_candidate_names if n.startswith(prefix)]


def provide_no_reference_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.no_reference_candidate_names if n.startswith(prefix)]


def provide_running_topology_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.running_topology_names if n.startswith(prefix)]


def provide_running_access_info_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.running_access_info_names if n.startswith(prefix)]


def provide_running_scenario_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.running_scenario_names if n.startswith(prefix)]


def provide_running_reference_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.running_reference_names if n.startswith(prefix)]


def provide_scenario_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.scenario_names if n.startswith(prefix)]


def provide_reference_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.reference_names if n.startswith(prefix)]


def provide_access_info_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.access_info_names if n.startswith(prefix)]


def provide_candidate_reference_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.candidate_reference_names if n.startswith(prefix)]


def provide_topology_device_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.topology_candidate_device_names if n.startswith(prefix)]


def provide_access_info_device_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.access_info_candidate_device_names if n.startswith(prefix)]


def provide_transport_values(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    lowered = prefix.lower()
    return [v for v in ("ssh", "telnet") if v.startswith(lowered)]


def provide_device_types(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    lowered = prefix.lower()
    return [v for v in lab.DEVICE_TYPES if v.startswith(lowered)]


def provide_jump_host_type_values(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    lowered = prefix.lower()
    return [lab.JUMP_HOST_TYPE] if lab.JUMP_HOST_TYPE.startswith(lowered) else []


def provide_jump_host_transport_values(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    lowered = prefix.lower()
    return ["ssh"] if "ssh".startswith(lowered) else []


def provide_access_info_jump_host_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.access_info_candidate_jump_host_names if n.startswith(prefix)]


def provide_log_device_ids(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.log_device_ids if n.startswith(prefix)]


def provide_monitor_terminal_device_ids(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.monitor_terminal_device_ids if n.startswith(prefix)]


def provide_log_files(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    """The log filenames available for `show logging <device-id> <file>`
    depend on which device-id was already typed earlier in this same
    command -- unlike every other provider here, this one reads
    `committed` (the raw tokens so far: ("show", "logging", "<device-id>"))
    instead of, or in addition to, session-wide context."""
    if not committed:
        return []
    device_id = committed[-1]
    return [n for n in ctx.log_files_by_device.get(device_id, ()) if n.startswith(prefix)]


def provide_committed_active_reference_names(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    return [n for n in ctx.committed_active_reference_names if n.startswith(prefix)]


# `help <topic>` is Network Lab MCP's own Quick Start/usage help, distinct
# from the IOS XR-style `?` context-sensitive syntax help -- see cli/main.py
# render_quick_start()/render_help_*() for the actual topic content. This
# closed enum is grammar-only (no other module needs to share it, unlike
# device.type), so it is not sourced from network_lab_mcp.lab.
HELP_TOPICS: dict[str, str] = {
    "claude": "Show Claude Code integration help",
    "workflow": "Show the recommended Network Lab workflow",
    "editor": "Show external YAML editor usage",
    "cli": "Show CLI usage information",
}


def validate_help_topic(value: str) -> ValidationOutcome:
    lowered = value.lower()
    if lowered in HELP_TOPICS:
        return _ok(lowered)
    matches = [key for key in HELP_TOPICS if key.startswith(lowered)]
    if len(matches) == 1:
        return _ok(matches[0])
    if len(matches) > 1:
        return _fail(f"Ambiguous help topic '{value}'. Matches: {', '.join(sorted(matches))}.")
    return _fail(f"Invalid help topic '{value}'. Expected one of: {', '.join(sorted(HELP_TOPICS))}.")


def provide_help_topics(ctx: CliContext, prefix: str, committed: tuple[str, ...] = ()) -> list[str]:
    lowered = prefix.lower()
    return [t for t in HELP_TOPICS if t.startswith(lowered)]


# --------------------------------------------------------------------------
# Grammar tree
# --------------------------------------------------------------------------


@dataclass
class Argument:
    name: str
    description: str
    validate: Validator = validate_freeform
    provider: Optional[Provider] = None
    rest_of_line: bool = False
    hint: str = ""
    sensitive: bool = False
    enumerate_when_empty: bool = False
    value_help: dict[str, str] = field(default_factory=dict)
    # A "select or create" identifier (topology/access-info/scenario/
    # reference/device names): bare `?` lists existing candidates *and* a
    # creation hint, instead of just a generic <name> placeholder.
    creatable: bool = False
    existing_label: str = ""
    create_label: str = ""

    def display_hint(self) -> str:
        return self.hint or f"<{self.name}>"


@dataclass
class CommandSpec:
    action: str
    summary: str


@dataclass
class Node:
    description: str = ""
    literal_children: dict[str, "Node"] = field(default_factory=dict)
    argument: Optional[Argument] = None
    argument_child: Optional["Node"] = None
    command: Optional[CommandSpec] = None
    # Set on the node reached after a `rest_of_line` argument: every further
    # token is still part of that free-form text, so help keeps showing the
    # argument and the node's own command makes the line executable.
    free_text_argument: Optional[Argument] = None

    def add_literal(self, keyword: str, description: str = "") -> "Node":
        key = keyword.lower()
        node = self.literal_children.get(key)
        if node is None:
            node = Node()
            self.literal_children[key] = node
        if description:
            node.description = description
        return node

    def add_argument(self, argument: Argument) -> "Node":
        self.argument = argument
        node = Node()
        if argument.rest_of_line:
            node.free_text_argument = argument
        self.argument_child = node
        return node

    def set_command(self, action: str, summary: str) -> None:
        self.command = CommandSpec(action, summary)


def _add_show_subtree(
    root: Node,
    mode: str,
    *,
    running_config_description: str,
    include_configuration: bool,
    configuration_description: str = "Show candidate configuration",
    include_version: bool = False,
    include_logging: bool = False,
    include_running_config_definition_views: bool = False,
    bare_show: bool = False,
    show_keyword_description: str = "Show information",
) -> None:
    """`show running-config` and `show configuration` are scoped to the
    *current CLI context*: EXEC/global/running-config mode show the MCP
    running-config selection; a topology/access-info/scenario/reference
    (or its nested device/jump-host submode) shows that same object's own
    committed/uncommitted state instead (see cli/main.py's context-aware
    renderers). Only EXEC gets `show version` -- it is software-level
    information, not part of any configuration context. Every configuration
    mode (never EXEC) additionally makes bare `show` itself a complete
    command with the same meaning as `show configuration` -- the same
    "a node can carry both its own command and further children" mechanism
    parse()/help() already use for bare `help`."""
    show = root.add_literal("show", show_keyword_description)
    running = show.add_literal("running-config", running_config_description)
    running.set_command(f"{mode}.show_running_config", running_config_description)
    if include_running_config_definition_views:
        _add_running_config_definition_views_subtree(running, mode)
    if include_version:
        version = show.add_literal("version", "Show Network Lab MCP version information")
        version.set_command(f"{mode}.show_version", "Show Network Lab MCP version information")
    if include_configuration:
        candidate = show.add_literal("configuration", configuration_description)
        candidate.set_command(f"{mode}.show_configuration", configuration_description)
    if bare_show:
        show.set_command(f"{mode}.show_configuration", configuration_description)
    if include_logging:
        _add_logging_subtree(show, mode)


def _add_running_config_definition_views_subtree(running_node: Node, mode: str) -> None:
    """`show running-config <definition-type>` (EXEC only): a read-only
    dereference of one committed active-definition selection -- never
    candidate state, never a directory listing. `access-info`/`topology`/
    `scenario` are single-selection (no further argument: there is at
    most one active definition of each, so there is nothing to select
    between). `reference` is multi-select (an ordered list), so it is
    itself a complete command (all active references, in committed
    order) *and* accepts a further `<name>` naming one of them -- the
    same "node carries both a command and children" mechanism used by
    bare `show`/`help`/`show logging`."""
    for kind in ("access-info", "topology", "scenario"):
        action = kind.replace("-", "_")
        node = running_node.add_literal(kind, f"Committed active {kind} definition")
        node.set_command(f"{mode}.show_running_config_{action}", f"Committed active {kind} definition")

    reference_node = running_node.add_literal("reference", "Committed active reference definition(s)")
    reference_node.set_command(
        f"{mode}.show_running_config_reference", "All committed active reference definitions, in order"
    )
    reference_arg = Argument(
        "name",
        "Committed active reference name",
        provider=provide_committed_active_reference_names,
        hint="<name>",
        enumerate_when_empty=True,
    )
    reference_next = reference_node.add_argument(reference_arg)
    reference_next.set_command(
        f"{mode}.show_running_config_reference_name", "One committed active reference definition"
    )


def _add_logging_subtree(show_node: Node, mode: str) -> None:
    """`show logging` (EXEC only): bare (the original, flat,
    per-file listing across every device), `summary` (a per-device
    eligible-log-count table), `<device-id>` (one device's
    logs), or `<device-id> <log-file>` (that log's contents). Every level
    is itself a complete command (`<cr>`) as well as accepting a further,
    more specific token -- the same "node carries both a command and
    children" mechanism used by bare `show`/`help`. "summary" is a fixed
    keyword living alongside the dynamic <device-id> argument at the very
    same node -- see complete()/help()'s generic support (also reused
    for `delete logging all`/`directory`)
    for a node combining literal children with a further dynamic
    argument."""
    logging_node = show_node.add_literal("logging", "Display terminal session logs")
    logging_node.set_command(f"{mode}.show_logging", "Display terminal session logs for all devices")

    summary_node = logging_node.add_literal("summary", "Show terminal log summary")
    summary_node.set_command(f"{mode}.show_logging_summary", "Show terminal log summary")

    device_arg = Argument(
        "device_id",
        "Device terminal logs",
        provider=provide_log_device_ids,
        hint="<device-id>",
        enumerate_when_empty=True,
    )
    device_next = logging_node.add_argument(device_arg)
    device_next.set_command(f"{mode}.show_logging_device", "Display terminal logs for one device")

    file_arg = Argument(
        "log_file",
        "Terminal log file",
        provider=provide_log_files,
        hint="<log-file>",
        enumerate_when_empty=True,
    )
    file_next = device_next.add_argument(file_arg)
    file_next.set_command(f"{mode}.show_logging_device_file", "Display the contents of one terminal log file")


def _add_delete_subtree(root: Node) -> None:
    """`delete logging` (EXEC only): reuses the exact same
    dynamic providers as `show logging` (provide_log_device_ids/
    provide_log_files) so what is deletable never drifts from what `show
    logging` displays -- no second log-discovery model. Unlike `show
    logging`, bare `delete logging` and `delete logging <device>` are
    deliberately NOT executable (no set_command on those nodes): only
    `delete logging all` (and its `directory` child), `delete logging
    <device> all`, `delete logging <device> directory`, and `delete
    logging <device> <log-file>` are complete commands.

    "all"/"directory" are fixed keywords living alongside the dynamic
    <device-id>/<log-file> argument at the very same node -- see
    complete()/help()'s generic support for a node
    that combines literal children with a further dynamic argument; that
    support already handles any number of literal children, so adding
    "directory" here needed no further grammar core changes. `all` is
    additionally both an executable command *and* the parent of its own
    "directory" child -- the same "node carries both a command and
    further children" mechanism bare `show`/`help` already use."""
    delete_node = root.add_literal("delete", "Delete stored information")
    logging_node = delete_node.add_literal("logging", "Delete terminal logs")

    all_node = logging_node.add_literal("all", "Delete all terminal logs")
    all_node.set_command("exec.delete_logging_all", "Delete all terminal logs")

    all_directory_node = all_node.add_literal(
        "directory", "Delete all terminal logs and device log directories"
    )
    all_directory_node.set_command(
        "exec.delete_logging_all_directory", "Delete all terminal logs and device log directories"
    )

    device_arg = Argument(
        "device_id",
        "Device logging directory",
        provider=provide_log_device_ids,
        hint="<device-id>",
        enumerate_when_empty=True,
    )
    device_next = logging_node.add_argument(device_arg)

    device_all_node = device_next.add_literal("all", "Delete all terminal logs for this device")
    device_all_node.set_command("exec.delete_logging_device_all", "Delete all terminal logs for this device")

    device_directory_node = device_next.add_literal(
        "directory", "Delete terminal logs and device log directory"
    )
    device_directory_node.set_command(
        "exec.delete_logging_device_directory", "Delete terminal logs and device log directory"
    )

    file_arg = Argument(
        "log_file",
        "Terminal log",
        provider=provide_log_files,
        hint="<log-file>",
        enumerate_when_empty=True,
    )
    file_next = device_next.add_argument(file_arg)
    file_next.set_command("exec.delete_logging_device_file", "Delete this terminal log")


def _add_help_subtree(root: Node, mode: str) -> None:
    """`help` (bare) is Network Lab MCP's own Quick Start; `help <topic>`
    drills into one of HELP_TOPICS. Both are ordinary grammar nodes -- one
    node carries its own command (the bare case) and also an argument (the
    topic case), which parse()/help() already support without any change:
    a node can be a complete command *and* accept a further token."""
    help_node = root.add_literal("help", "Display Network Lab MCP quick start help")
    help_node.set_command(f"{mode}.help", "Display Network Lab MCP quick start help")
    topic_arg = Argument(
        "topic",
        "Help topic",
        validate=validate_help_topic,
        provider=provide_help_topics,
        hint="<topic>",
        enumerate_when_empty=True,
        value_help=dict(HELP_TOPICS),
    )
    topic_next = help_node.add_argument(topic_arg)
    topic_next.set_command(f"{mode}.help_topic", "Display help for a specific topic")


def _add_common_subtree(
    root: Node,
    mode: str,
    *,
    include_root: bool = True,
    exit_description: str = "Exit from this submode",
) -> None:
    """`commit` deliberately never changes `mode` (see cli/main.py::h_commit)
    -- it only saves the candidate. Navigation is `root` (jump straight to
    global configuration mode, preserving candidate state), `exit` (one
    level up), and `end` (guarded jump to EXEC) -- none of the three ever
    commits or clears. `root` is omitted at global configuration mode
    itself (already the configuration root; IOS XR-style submode-only
    visibility) via `include_root=False`."""
    clear = root.add_literal("clear", "Clear the uncommitted configuration")
    clear.set_command(f"{mode}.clear", "Clear the uncommitted configuration")
    commit = root.add_literal("commit", "Commit configuration changes")
    commit.set_command(f"{mode}.commit", "Commit configuration changes")
    if include_root:
        root_node = root.add_literal("root", "Exit to the global configuration mode")
        root_node.set_command(f"{mode}.root", "Exit to the global configuration mode")
    end = root.add_literal("end", "Exit from configure mode")
    end.set_command(f"{mode}.end", "Exit from configure mode")
    exit_node = root.add_literal("exit", exit_description)
    exit_node.set_command(f"{mode}.exit", exit_description)
    _add_help_subtree(root, mode)


def _build_exec_root() -> Node:
    root = Node()
    configure = root.add_literal("configure", "Enter configuration mode")
    configure.set_command("exec.configure", "Enter configuration mode")
    terminal_alias = configure.add_literal("terminal", "Enter configuration mode")
    terminal_alias.set_command("exec.configure", "Enter configuration mode")

    _add_show_subtree(
        root,
        "exec",
        running_config_description="Show committed MCP definition selection",
        include_configuration=False,
        include_version=True,
        include_logging=True,
        include_running_config_definition_views=True,
    )
    _add_delete_subtree(root)

    # `terminal monitor <device-id>` (EXEC only): a read-only
    # human observation view of the same production tmux session
    # terminal_open()/terminal_send()/terminal_read() use -- never
    # creatable (observing never creates a session), dynamically
    # enumerated from the effective, committed-only eligible target set
    # (see CliContext.monitor_terminal_device_ids's own docstring).
    # Execution (cli/main.py's h_monitor_terminal) re-derives and
    # re-validates that same set independently at run time; this
    # provider only ever drives Tab/`?`. Cisco IOS/IOS XE/IOS XR word
    # order -- `terminal <subcommand>` -- rather than `monitor terminal`;
    # the top-level `terminal` node is distinct from `configure`'s own
    # `terminal` child (the `configure terminal` alias) a few lines above,
    # since each literal lives in its own parent node's namespace.
    terminal_node = root.add_literal("terminal", "Monitor a live terminal session")
    monitor_terminal_node = terminal_node.add_literal("monitor", "Monitor a device's live terminal session")
    monitor_terminal_arg = Argument(
        "device_id",
        "Device to monitor",
        provider=provide_monitor_terminal_device_ids,
        hint="<device-id>",
        enumerate_when_empty=True,
    )
    monitor_terminal_next = monitor_terminal_node.add_argument(monitor_terminal_arg)
    monitor_terminal_next.set_command("exec.monitor_terminal", "Monitor a device's live terminal session")

    _add_help_subtree(root, "exec")

    exit_node = root.add_literal("exit", "Exit the CLI")
    exit_node.set_command("exec.exit", "Exit the CLI")

    quit_node = root.add_literal("quit", "Exit the CLI")
    quit_node.set_command("exec.quit", "Exit the CLI")

    return root


def _build_global_root() -> Node:
    root = Node()

    running_config_node = root.add_literal("running-config", "Configure definitions used by MCP")
    running_config_node.set_command("global.running_config", "Configure definitions used by MCP")

    discover_node = root.add_literal("discover", "Discover topology from active access information")
    discover_topology_node = discover_node.add_literal(
        "topology", "Discover topology from active access information"
    )
    discover_topology_node.set_command(
        "global.discover_topology", "Discover topology using LLDP/CDP"
    )
    discover_topology_l3_node = discover_topology_node.add_literal(
        "l3", "Include Layer 3 interface information"
    )
    discover_topology_l3_node.set_command(
        "global.discover_topology_l3", "Discover topology using LLDP/CDP plus Layer 3 interface information"
    )

    access_info_arg = Argument(
        "name",
        "Access-info definition name",
        provider=provide_access_info_names,
        hint="<name>",
        creatable=True,
        existing_label="Existing access-info definition",
        create_label="Create or edit access-info definition",
    )
    access_info_node = root.add_literal("access-info", "Create or edit an access-info definition")
    access_info_next = access_info_node.add_argument(access_info_arg)
    access_info_next.set_command("global.access_info", "Create or edit an access-info definition")

    topology_arg = Argument(
        "name",
        "Topology definition name",
        provider=provide_topology_names,
        hint="<name>",
        creatable=True,
        existing_label="Existing topology definition",
        create_label="Create or edit topology definition",
    )
    topology_node = root.add_literal("topology", "Create or edit a topology definition")
    topology_next = topology_node.add_argument(topology_arg)
    topology_next.set_command("global.topology", "Create or edit a topology definition")

    scenario_arg = Argument(
        "name",
        "Scenario definition name",
        provider=provide_scenario_names,
        hint="<name>",
        creatable=True,
        existing_label="Existing scenario definition",
        create_label="Create or edit scenario definition",
    )
    scenario_node = root.add_literal("scenario", "Create or edit a scenario definition")
    scenario_next = scenario_node.add_argument(scenario_arg)
    scenario_next.set_command("global.scenario", "Create or edit a scenario definition")

    reference_arg = Argument(
        "name",
        "Reference definition name",
        provider=provide_reference_names,
        hint="<name>",
        creatable=True,
        existing_label="Existing reference definition",
        create_label="Create or edit reference definition",
    )
    reference_node = root.add_literal("reference", "Create or edit a reference definition")
    reference_next = reference_node.add_argument(reference_arg)
    reference_next.set_command("global.reference", "Create or edit a reference definition")

    # `no <kind> <name>`: candidate deletion of a STORED
    # definition -- distinct from `config-running# no ...`, which (where
    # it exists at all) unsets/deactivates a *running-config selection*
    # instead, a completely separate candidate scope; see
    # cli/config.py's CliSession module docstring and
    # _build_running_root() below. Never creatable: deletion only ever
    # targets a name the dynamic, candidate-aware provider above already
    # lists as legal.
    no_node = root.add_literal("no", "Remove a stored definition")

    no_access_info_arg = Argument(
        "name",
        "Existing access-info definition",
        provider=provide_no_access_info_names,
        hint="<name>",
        enumerate_when_empty=True,
    )
    no_access_info_node = no_node.add_literal("access-info", "Remove an access-info definition")
    no_access_info_next = no_access_info_node.add_argument(no_access_info_arg)
    no_access_info_next.set_command("global.no_access_info", "Remove an access-info definition")

    no_topology_arg = Argument(
        "name",
        "Existing topology definition",
        provider=provide_no_topology_names,
        hint="<name>",
        enumerate_when_empty=True,
    )
    no_topology_node = no_node.add_literal("topology", "Remove a topology definition")
    no_topology_next = no_topology_node.add_argument(no_topology_arg)
    no_topology_next.set_command("global.no_topology", "Remove a topology definition")

    no_scenario_arg = Argument(
        "name",
        "Existing scenario definition",
        provider=provide_no_scenario_names,
        hint="<name>",
        enumerate_when_empty=True,
    )
    no_scenario_node = no_node.add_literal("scenario", "Remove a scenario definition")
    no_scenario_next = no_scenario_node.add_argument(no_scenario_arg)
    no_scenario_next.set_command("global.no_scenario", "Remove a scenario definition")

    no_reference_arg = Argument(
        "name",
        "Existing reference definition",
        provider=provide_no_reference_names,
        hint="<name>",
        enumerate_when_empty=True,
    )
    no_reference_node = no_node.add_literal("reference", "Remove a reference definition")
    no_reference_next = no_reference_node.add_argument(no_reference_arg)
    no_reference_next.set_command("global.no_reference", "Remove a reference definition")

    _add_show_subtree(
        root,
        "global",
        running_config_description="Contents of running configuration",
        include_configuration=True,
        configuration_description="Contents of uncommitted configuration",
        bare_show=True,
        show_keyword_description="Show contents of configuration",
    )
    _add_common_subtree(root, "global", include_root=False, exit_description="Exit from configure mode")
    return root


def _build_running_root() -> Node:
    root = Node()

    # Running-config selectors name an EXISTING stored definition to
    # activate for MCP -- never creatable (selecting never creates a
    # definition), and dynamically enumerated (`enumerate_when_empty`)
    # from the effective, candidate-aware set of names that are actually
    # legal to select right now (provide_running_*_names() /
    # CliContext.running_*_names -- excludes a definition currently
    # pending whole-definition deletion in the definition-editing
    # candidate scope; see that field's own docstring). Execution
    # (select_access_info()/select_topology()/set_scenario()/
    # add_reference()) is still the authoritative validation either way.
    access_info_arg = Argument(
        "name",
        "Access-info definition name",
        provider=provide_running_access_info_names,
        hint="<name>",
        enumerate_when_empty=True,
    )
    access_info_node = root.add_literal("access-info", "Select access-info for MCP")
    access_info_next = access_info_node.add_argument(access_info_arg)
    access_info_next.set_command("running.access_info", "Select access-info for MCP")

    topology_arg = Argument(
        "name",
        "Topology definition name",
        provider=provide_running_topology_names,
        hint="<name>",
        enumerate_when_empty=True,
    )
    topology_node = root.add_literal("topology", "Select topology for MCP")
    topology_next = topology_node.add_argument(topology_arg)
    topology_next.set_command("running.topology", "Select topology for MCP")

    scenario_arg = Argument(
        "name",
        "Scenario definition name",
        provider=provide_running_scenario_names,
        hint="<name>",
        enumerate_when_empty=True,
    )
    scenario_node = root.add_literal("scenario", "Select scenario for MCP")
    scenario_next = scenario_node.add_argument(scenario_arg)
    scenario_next.set_command("running.scenario", "Select scenario for MCP")

    reference_arg = Argument(
        "name",
        "Reference definition name",
        provider=provide_running_reference_names,
        hint="<name>",
        enumerate_when_empty=True,
    )
    reference_node = root.add_literal("reference", "Activate reference for MCP")
    reference_next = reference_node.add_argument(reference_arg)
    reference_next.set_command("running.reference_add", "Activate reference for MCP")

    no_node = root.add_literal("no", "Negate a running configuration item")
    no_access_info_node = no_node.add_literal("access-info", "Remove the access information selection")
    no_access_info_node.set_command("running.access_info_remove", "Remove the access information selection")
    no_reference_node = no_node.add_literal("reference", "Remove a reference used by MCP")
    no_reference_arg = Argument(
        "name",
        "Currently selected reference name",
        provider=provide_candidate_reference_names,
        hint="<name>",
    )
    no_reference_next = no_reference_node.add_argument(no_reference_arg)
    no_reference_next.set_command("running.reference_remove", "Remove a reference used by MCP")

    _add_show_subtree(
        root,
        "running",
        running_config_description="Contents of running configuration",
        include_configuration=True,
        configuration_description="Contents of uncommitted configuration",
        bare_show=True,
        show_keyword_description="Show contents of configuration",
    )
    _add_common_subtree(root, "running")
    return root


def _build_running_entry_root(mode: str) -> Node:
    """Active running-config entry submode (`config-running-<kind>-<name>`),
    entered by selecting the entry from config-running. Edits metadata of
    that one active entry only -- never the underlying definition, which is
    the separate definition editor (`config-<kind>-<name>`). Like every
    other submode root, parent-level commands (`access-info <name>` etc.)
    are not visible here: `exit` back to config-running to switch."""
    root = Node()
    text_arg = Argument(
        "text",
        "Free-form running-config entry description",
        validate=validate_description,
        rest_of_line=True,
        hint="<text>",
    )
    description_node = root.add_literal("description", "Set the running-config entry description")
    description_node.add_argument(text_arg).set_command(f"{mode}.description", "Set the running-config entry description")

    no_node = root.add_literal("no", "Negate a running configuration item")
    no_node.add_literal("description", "Remove the running-config entry description").set_command(
        f"{mode}.clear_description", "Remove the running-config entry description"
    )

    _add_show_subtree(
        root,
        mode,
        running_config_description="Contents of running configuration",
        include_configuration=True,
        configuration_description="Contents of uncommitted configuration",
        bare_show=True,
        show_keyword_description="Show contents of configuration",
    )
    _add_common_subtree(root, mode)
    return root


def _build_topology_root() -> Node:
    root = Node()

    description_arg = Argument(
        "text",
        "Free-form topology description",
        rest_of_line=True,
        hint="<text>",
    )
    description_node = root.add_literal("description", "Set the topology description")
    description_next = description_node.add_argument(description_arg)
    description_next.set_command("topology.description", "Set the topology description")

    device_arg = Argument(
        "name",
        "Device name",
        provider=provide_topology_device_names,
        hint="<name>",
        creatable=True,
        existing_label="Existing device",
        create_label="Create or edit device",
    )
    device_node = root.add_literal("device", "Create or edit a device")
    device_next = device_node.add_argument(device_arg)
    device_next.set_command("topology.device", "Create or edit a device")

    edit_node = root.add_literal("edit", "Edit this topology in an external editor")
    edit_node.set_command("topology.edit", "Edit this topology in an external editor")

    _add_show_subtree(
        root,
        "topology",
        running_config_description="Contents of committed topology configuration",
        include_configuration=True,
        configuration_description="Contents of uncommitted topology configuration",
        bare_show=True,
        show_keyword_description="Show contents of configuration",
    )
    _add_common_subtree(root, "topology")
    return root


def _build_device_root() -> Node:
    """Topology's nested device submode: safe logical metadata only. Private
    access fields (address/transport/port/username/password) live in
    access-info's own device submode (_build_access_device_root) instead."""
    root = Node()

    type_arg = Argument(
        "value",
        "Set the device type",
        validate=validate_device_type,
        provider=provide_device_types,
        hint="<iosxr|iosxe|ios|nxos|host>",
        enumerate_when_empty=True,
        value_help=dict(lab.DEVICE_TYPES),
    )
    type_node = root.add_literal("type", "Set the device type")
    type_next = type_node.add_argument(type_arg)
    type_next.set_command("device.set_type", "Set the device type")

    _add_show_subtree(
        root,
        "device",
        running_config_description="Contents of committed device configuration",
        include_configuration=True,
        configuration_description="Contents of uncommitted device configuration",
        bare_show=True,
        show_keyword_description="Show contents of configuration",
    )
    _add_common_subtree(root, "device")
    return root


def _build_access_info_root() -> Node:
    root = Node()

    device_arg = Argument(
        "name",
        "Device name",
        provider=provide_access_info_device_names,
        hint="<name>",
        creatable=True,
        existing_label="Existing device",
        create_label="Create or edit device",
    )
    device_node = root.add_literal("device", "Create or edit a device")
    device_next = device_node.add_argument(device_arg)
    device_next.set_command("access_info.device", "Create or edit a device")

    jump_host_arg = Argument(
        "name",
        "Jump host name",
        provider=provide_access_info_jump_host_names,
        hint="<name>",
        creatable=True,
        existing_label="Existing jump host",
        create_label="Create or edit jump host",
    )
    jump_host_node = root.add_literal("jump-host", "Create or edit a jump host")
    jump_host_next = jump_host_node.add_argument(jump_host_arg)
    jump_host_next.set_command("access_info.jump_host", "Create or edit a jump host")

    # `no device <name>` / `no jump-host <name>`: remove the whole object
    # from the candidate. Unlike the create/edit arguments above, these are
    # deliberately not `creatable` -- deletion only ever targets an object
    # already present in the *current candidate* (reusing the exact same
    # candidate-sourced providers), never one that does not yet exist.
    no_node = root.add_literal("no", "Remove a device or jump host")

    no_device_arg = Argument(
        "name",
        "Existing device name",
        provider=provide_access_info_device_names,
        hint="<name>",
        enumerate_when_empty=True,
    )
    no_device_node = no_node.add_literal("device", "Remove a device")
    no_device_next = no_device_node.add_argument(no_device_arg)
    no_device_next.set_command("access_info.remove_device", "Remove a device")

    no_jump_host_arg = Argument(
        "name",
        "Existing jump host name",
        provider=provide_access_info_jump_host_names,
        hint="<name>",
        enumerate_when_empty=True,
    )
    no_jump_host_node = no_node.add_literal("jump-host", "Remove a jump host")
    no_jump_host_next = no_jump_host_node.add_argument(no_jump_host_arg)
    no_jump_host_next.set_command("access_info.remove_jump_host", "Remove a jump host")

    _add_show_subtree(
        root,
        "access_info",
        running_config_description="Contents of committed access information",
        include_configuration=True,
        configuration_description="Contents of uncommitted access information",
        bare_show=True,
        show_keyword_description="Show contents of configuration",
    )
    _add_common_subtree(root, "access_info")
    return root


def _build_access_device_root() -> Node:
    """access-info's nested device submode: private connection fields."""
    root = Node()

    def add_field(
        keyword: str,
        description: str,
        action: str,
        *,
        validate: Validator = validate_freeform,
        provider: Optional[Provider] = None,
        hint: str = "<value>",
        sensitive: bool = False,
        enumerate_when_empty: bool = False,
        value_help: Optional[dict[str, str]] = None,
    ) -> None:
        argument = Argument(
            "value",
            description,
            validate=validate,
            provider=provider,
            hint=hint,
            sensitive=sensitive,
            enumerate_when_empty=enumerate_when_empty,
            value_help=value_help or {},
        )
        node = root.add_literal(keyword, description)
        next_node = node.add_argument(argument)
        next_node.set_command(action, description)

    add_field(
        "type",
        "Set the device type",
        "access_device.set_type",
        validate=validate_device_type,
        provider=provide_device_types,
        hint="<iosxr|iosxe|ios|nxos|host>",
        enumerate_when_empty=True,
        value_help=dict(lab.DEVICE_TYPES),
    )
    add_field("address", "Set the device management address", "access_device.set_address")
    add_field(
        "transport",
        "Set the device transport",
        "access_device.set_transport",
        validate=validate_transport,
        provider=provide_transport_values,
        hint="<ssh|telnet>",
        enumerate_when_empty=True,
        value_help={"ssh": "Use SSH transport", "telnet": "Use Telnet transport"},
    )
    add_field(
        "port",
        "Set the device port",
        "access_device.set_port",
        validate=validate_port,
        hint="<1-65535>",
    )
    add_field("username", "Set the device username", "access_device.set_username")
    add_field(
        "password",
        "Set the device password",
        "access_device.set_password",
        sensitive=True,
        hint="<password>",
    )
    add_field(
        "jump-host",
        "Reference a jump host for this device (single-hop OpenSSH ProxyJump)",
        "access_device.set_jump_host",
        provider=provide_access_info_jump_host_names,
        hint="<name>",
    )

    no_node = root.add_literal("no", "Negate a device field")
    for keyword, action, description in (
        ("type", "access_device.clear_type", "Clear the device type"),
        ("address", "access_device.clear_address", "Clear the device address"),
        ("transport", "access_device.clear_transport", "Clear the device transport"),
        ("username", "access_device.clear_username", "Clear the device username"),
        ("password", "access_device.clear_password", "Clear the device password"),
        ("port", "access_device.clear_port", "Clear the device port"),
        ("jump-host", "access_device.clear_jump_host", "Clear the device jump-host reference"),
    ):
        field_node = no_node.add_literal(keyword, description)
        field_node.set_command(action, description)

    _add_show_subtree(
        root,
        "access_device",
        running_config_description="Contents of committed device configuration",
        include_configuration=True,
        configuration_description="Contents of uncommitted device configuration",
        bare_show=True,
        show_keyword_description="Show contents of configuration",
    )
    _add_common_subtree(root, "access_device")
    return root


def _build_access_jump_host_root() -> Node:
    """access-info's nested jump-host submode: a reusable single-hop
    OpenSSH ProxyJump endpoint. Deliberately narrower than the device
    submode above: `type` only ever resolves to 'host' and `transport`
    only ever resolves to 'ssh' (see validate_jump_host_type()/
    validate_jump_host_transport()) -- both still reuse the shared
    device.type SSOT rather than inventing a separate enum."""
    root = Node()

    def add_field(
        keyword: str,
        description: str,
        action: str,
        *,
        validate: Validator = validate_freeform,
        provider: Optional[Provider] = None,
        hint: str = "<value>",
        sensitive: bool = False,
        enumerate_when_empty: bool = False,
        value_help: Optional[dict[str, str]] = None,
    ) -> None:
        argument = Argument(
            "value",
            description,
            validate=validate,
            provider=provider,
            hint=hint,
            sensitive=sensitive,
            enumerate_when_empty=enumerate_when_empty,
            value_help=value_help or {},
        )
        node = root.add_literal(keyword, description)
        next_node = node.add_argument(argument)
        next_node.set_command(action, description)

    add_field(
        "type",
        "Set the jump-host type",
        "access_jump_host.set_type",
        validate=validate_jump_host_type,
        provider=provide_jump_host_type_values,
        hint="<host>",
        enumerate_when_empty=True,
        value_help={lab.JUMP_HOST_TYPE: lab.DEVICE_TYPES[lab.JUMP_HOST_TYPE]},
    )
    add_field("address", "Set the jump-host management address", "access_jump_host.set_address")
    add_field(
        "transport",
        "Set the jump-host transport",
        "access_jump_host.set_transport",
        validate=validate_jump_host_transport,
        provider=provide_jump_host_transport_values,
        hint="<ssh>",
        enumerate_when_empty=True,
        value_help={"ssh": "Use SSH transport (required for ProxyJump)"},
    )
    add_field(
        "port",
        "Set the jump-host port",
        "access_jump_host.set_port",
        validate=validate_port,
        hint="<1-65535>",
    )
    add_field("username", "Set the jump-host username", "access_jump_host.set_username")
    add_field(
        "password",
        "Set the jump-host password",
        "access_jump_host.set_password",
        sensitive=True,
        hint="<password>",
    )

    no_node = root.add_literal("no", "Negate a jump-host field")
    for keyword, action, description in (
        ("type", "access_jump_host.clear_type", "Clear the jump-host type"),
        ("address", "access_jump_host.clear_address", "Clear the jump-host address"),
        ("transport", "access_jump_host.clear_transport", "Clear the jump-host transport"),
        ("username", "access_jump_host.clear_username", "Clear the jump-host username"),
        ("password", "access_jump_host.clear_password", "Clear the jump-host password"),
        ("port", "access_jump_host.clear_port", "Clear the jump-host port"),
    ):
        field_node = no_node.add_literal(keyword, description)
        field_node.set_command(action, description)

    _add_show_subtree(
        root,
        "access_jump_host",
        running_config_description="Contents of committed jump-host configuration",
        include_configuration=True,
        configuration_description="Contents of uncommitted jump-host configuration",
        bare_show=True,
        show_keyword_description="Show contents of configuration",
    )
    _add_common_subtree(root, "access_jump_host")
    return root


def _build_scenario_root() -> Node:
    root = Node()
    edit_node = root.add_literal("edit", "Edit this scenario in an external editor")
    edit_node.set_command("scenario.edit", "Edit this scenario in an external editor")
    _add_show_subtree(
        root,
        "scenario",
        running_config_description="Contents of committed scenario configuration",
        include_configuration=True,
        configuration_description="Contents of uncommitted scenario configuration",
        bare_show=True,
        show_keyword_description="Show contents of configuration",
    )
    _add_common_subtree(root, "scenario")
    return root


def _build_reference_root() -> Node:
    root = Node()
    edit_node = root.add_literal("edit", "Edit this reference in an external editor")
    edit_node.set_command("reference.edit", "Edit this reference in an external editor")
    _add_show_subtree(
        root,
        "reference",
        running_config_description="Contents of committed reference configuration",
        include_configuration=True,
        configuration_description="Contents of uncommitted reference configuration",
        bare_show=True,
        show_keyword_description="Show contents of configuration",
    )
    _add_common_subtree(root, "reference")
    return root


MODE_ROOTS: dict[str, Node] = {
    "exec": _build_exec_root(),
    "global": _build_global_root(),
    "running": _build_running_root(),
    "running_access_info": _build_running_entry_root("running_access_info"),
    "running_topology": _build_running_entry_root("running_topology"),
    "running_scenario": _build_running_entry_root("running_scenario"),
    "running_reference": _build_running_entry_root("running_reference"),
    "topology": _build_topology_root(),
    "device": _build_device_root(),
    "access_info": _build_access_info_root(),
    "access_device": _build_access_device_root(),
    "access_jump_host": _build_access_jump_host_root(),
    "scenario": _build_scenario_root(),
    "reference": _build_reference_root(),
}


# --------------------------------------------------------------------------
# Tokenization
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\S+")


def tokenize(line: str) -> list[tuple[str, int, int]]:
    """Split a line into (text, start, end) tokens. No quoting is supported;
    a free-form rest-of-line argument (e.g. `description`) is handled
    separately by consuming raw text instead of re-joining tokens."""
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(line)]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


@dataclass
class ParseError:
    kind: str  # "unknown" | "ambiguous" | "incomplete" | "invalid"
    token: str = ""
    span: Optional[tuple[int, int]] = None
    detail: str = ""


@dataclass
class ParseResult:
    ok: bool
    action: Optional[str] = None
    args: Optional[dict[str, str]] = None
    error: Optional[ParseError] = None


def _match_literal(node: Node, token: str) -> tuple[Optional[Node], list[str]]:
    """Resolve `token` against `node`'s fixed keywords: exact case-insensitive
    match wins immediately; otherwise a *unique* case-insensitive prefix
    match wins. Returns (matched_child, []) on a unique match, or
    (None, matches) where `matches` is empty (no match) or has 2+ entries
    (ambiguous)."""
    lowered = token.lower()
    if lowered in node.literal_children:
        return node.literal_children[lowered], []
    matches = [kw for kw in node.literal_children if kw.startswith(lowered)]
    if len(matches) == 1:
        return node.literal_children[matches[0]], []
    return None, matches


def parse(mode: str, line: str) -> ParseResult:
    tokens = tokenize(line)
    node = MODE_ROOTS[mode]
    args: dict[str, str] = {}

    i = 0
    while i < len(tokens):
        text, start, end = tokens[i]

        if node.argument is not None and not node.literal_children:
            argument = node.argument
            if argument.rest_of_line:
                value = line[start:]
                outcome = argument.validate(value)
                if not outcome.ok:
                    return ParseResult(False, error=ParseError("invalid", text, (start, len(line)), outcome.error or ""))
                args[argument.name] = outcome.normalized if outcome.normalized is not None else value
                node = node.argument_child
                i = len(tokens)
                break
            outcome = argument.validate(text)
            if not outcome.ok:
                return ParseResult(False, error=ParseError("invalid", text, (start, end), outcome.error or ""))
            args[argument.name] = outcome.normalized if outcome.normalized is not None else text
            node = node.argument_child
            i += 1
            continue

        if node.literal_children:
            child, matches = _match_literal(node, text)
            if child is not None:
                node = child
                i += 1
                continue
            if matches:
                return ParseResult(False, error=ParseError("ambiguous", text, (start, end)))
            if node.argument is not None:
                argument = node.argument
                outcome = argument.validate(text)
                if not outcome.ok:
                    return ParseResult(False, error=ParseError("invalid", text, (start, end), outcome.error or ""))
                args[argument.name] = outcome.normalized if outcome.normalized is not None else text
                node = node.argument_child
                i += 1
                continue
            if i == 0:
                return ParseResult(False, error=ParseError("unknown", text, (start, end)))
            return ParseResult(False, error=ParseError("invalid", text, (start, end)))

        # Dead end: this node accepts neither a further keyword nor an
        # argument, so any extra token here is invalid input.
        return ParseResult(False, error=ParseError("invalid", text, (start, end)))

    if node.command is not None:
        return ParseResult(True, action=node.command.action, args=args)
    return ParseResult(False, error=ParseError("incomplete"))


# --------------------------------------------------------------------------
# Completion and context-sensitive help
# --------------------------------------------------------------------------


@dataclass
class CompletionResult:
    candidates: list[str]
    replace_prefix: str


@dataclass
class HelpLine:
    token: str
    description: str


@dataclass
class HelpResult:
    lines: list[HelpLine]
    show_cr: bool
    partial: str


def split_for_completion(text_before_cursor: str) -> tuple[list[str], str]:
    """Split cursor-relative text into already-committed tokens and the
    partial token currently being typed (empty string if the cursor sits
    right after whitespace, i.e. at the start of a new token)."""
    tokens = tokenize(text_before_cursor)
    if text_before_cursor == "" or text_before_cursor[-1].isspace():
        return [t[0] for t in tokens], ""
    if not tokens:
        return [], ""
    return [t[0] for t in tokens[:-1]], tokens[-1][0]


def _walk_committed(mode: str, tokens: list[str]) -> Optional[Node]:
    """Walk already-committed tokens through the trie using the same
    resolution rules as parse(). Returns None if any committed token fails
    to resolve (broken prefix -> no completion/help context)."""
    node = MODE_ROOTS[mode]
    for text in tokens:
        if node.free_text_argument is not None:
            continue  # further words of the free-form text
        if node.argument is not None and not node.literal_children:
            node = node.argument_child
            continue
        if node.literal_children:
            child, matches = _match_literal(node, text)
            if child is not None:
                node = child
                continue
            if node.argument is not None:
                node = node.argument_child
                continue
            return None
        return None
    return node


def complete(mode: str, text_before_cursor: str, ctx: CliContext) -> CompletionResult:
    committed, partial = split_for_completion(text_before_cursor)
    node = _walk_committed(mode, committed)
    if node is None:
        return CompletionResult([], partial)

    if node.argument is not None and not node.literal_children:
        argument = node.argument
        if argument.sensitive or argument.provider is None:
            return CompletionResult([], partial)
        return CompletionResult(sorted(argument.provider(ctx, partial, tuple(committed))), partial)

    if node.literal_children:
        lowered = partial.lower()
        matches = [kw for kw in node.literal_children if kw.startswith(lowered)]
        # A node can combine fixed keywords with a further dynamic argument
        # at the same level (e.g. "delete logging" -> "all" | <device-id>).
        # No pre-existing node does this (every node so far is either a
        # pure keyword dispatcher or a pure argument slot), so this is
        # purely additive -- it only ever changes behavior for a node that
        # actually has both.
        argument = node.argument
        if argument is not None and not argument.sensitive and argument.provider is not None:
            matches = matches + sorted(argument.provider(ctx, partial, tuple(committed)))
        return CompletionResult(matches, partial)

    return CompletionResult([], partial)


def help(mode: str, text_before_cursor: str, ctx: CliContext) -> HelpResult:
    committed, partial = split_for_completion(text_before_cursor)
    node = _walk_committed(mode, committed)
    if node is None:
        return HelpResult([], False, partial)

    if node.free_text_argument is not None:
        # At least one word of free-form text is already entered, so the
        # command is executable: argument help plus <cr>.
        argument = node.free_text_argument
        return HelpResult([HelpLine(argument.display_hint(), argument.description)], node.command is not None, partial)

    if node.argument is not None and not node.literal_children:
        argument = node.argument
        lines: list[HelpLine] = []
        if argument.sensitive:
            lines.append(HelpLine(argument.display_hint(), argument.description))
        elif argument.enumerate_when_empty and partial == "" and argument.value_help:
            for value, description in argument.value_help.items():
                lines.append(HelpLine(value, description))
        elif argument.enumerate_when_empty and partial == "" and argument.provider is not None:
            # No static value_help (a fixed enum) -- enumerate dynamically
            # instead, e.g. `show logging ?` listing currently-known device
            # IDs. Unlike the `creatable` branch below, this never appends
            # a "create" hint: these are select-only identifiers.
            for value in sorted(argument.provider(ctx, "", tuple(committed))):
                lines.append(HelpLine(value, argument.description))
        elif argument.creatable and partial == "" and argument.provider is not None:
            for value in sorted(argument.provider(ctx, "", tuple(committed))):
                lines.append(HelpLine(value, argument.existing_label or argument.description))
            lines.append(HelpLine(argument.display_hint(), argument.create_label or argument.description))
        elif partial != "" and argument.provider is not None:
            label = argument.existing_label if argument.creatable else argument.description
            for value in sorted(argument.provider(ctx, partial, tuple(committed))):
                lines.append(HelpLine(value, label))
            # IOS XR distinguishes `token?` (help for the token just
            # typed) from `token ?` (help for what may follow it). A
            # *select-existing* identifier (never `creatable`, e.g. an
            # active reference name) is itself a complete command the
            # instant it exactly matches one of the argument's own known
            # candidates -- not merely a matching prefix (`ios?`) and not
            # a value the provider doesn't recognize at all (an inactive
            # stored reference). A `creatable` identifier (topology/
            # access-info/scenario/reference/device/jump-host name) never
            # needs that existence check at all: `creatable` itself already
            # means "any syntactically valid value here is also a complete
            # command" (it would create one) -- exactly what parse() does
            # (validate_freeform accepts any token, and every creatable
            # argument's own node is already terminal), so whatever partial
            # token is currently typed is Enter-able right now regardless
            # of whether it happens to match an existing name. This is a
            # property of the `creatable` flag, not of any one command --
            # no topology/access-info/scenario/reference-specific check.
            if argument.creatable or partial in argument.provider(ctx, "", tuple(committed)):
                show_cr = node.argument_child.command is not None
                return HelpResult(lines, show_cr, partial)
        else:
            lines.append(HelpLine(argument.display_hint(), argument.description))
            if argument.rest_of_line and partial != "" and argument.validate(partial).ok:
                # `description test?`: valid free-form text typed, so executable.
                return HelpResult(lines, node.argument_child.command is not None, partial)
        # True only for a node that is itself a complete command *and* takes
        # a further argument (currently just "help", e.g. `help ?` shows the
        # topic list plus <cr> since bare `help` is already valid); every
        # pre-existing argument (type/transport/topology name/...) has no
        # command of its own on this node, so this stays False for them.
        show_cr = partial == "" and node.command is not None
        return HelpResult(lines, show_cr, partial)

    lines = []
    lowered = partial.lower()
    # Same `token?` vs `token ?` distinction as above, for fixed keywords:
    # an exact (case-insensitive) match of one child keyword -- not merely
    # a matching prefix (`acc?`) -- is itself complete help for that one
    # keyword, plus `<cr>` if that keyword can itself end the command
    # (mirrors _match_literal()'s own "exact match wins" parse-time rule).
    if partial != "" and lowered in node.literal_children:
        matched_child = node.literal_children[lowered]
        lines.append(HelpLine(lowered, matched_child.description))
        show_cr = matched_child.command is not None
        return HelpResult(lines, show_cr, partial)

    # A node can combine fixed keywords with a further dynamic argument at
    # the same level (e.g. "delete logging" -> "all" | <device-id>). The
    # fixed keyword's exact match always wins (handled just above, mirroring
    # parse()'s own precedence -- see the "if node.argument is not None"
    # fallback inside parse()'s literal-children branch). Short of that, an
    # exact non-creatable dynamic-argument match is itself complete help for
    # that value plus `<cr>` if it can end the command -- the same "token?"
    # rule the pure-argument branch above already applies for its own node.
    # No pre-existing node combines both, so all of this is purely additive.
    argument = node.argument
    if (
        argument is not None
        and not argument.sensitive
        and not argument.creatable
        and argument.provider is not None
        and partial != ""
        and partial in argument.provider(ctx, "", tuple(committed))
    ):
        show_cr = node.argument_child.command is not None
        return HelpResult([HelpLine(partial, argument.description)], show_cr, partial)

    for keyword, child in node.literal_children.items():
        if keyword.startswith(lowered):
            lines.append(HelpLine(keyword, child.description))

    if argument is not None and not argument.sensitive and argument.provider is not None:
        if partial == "" and argument.enumerate_when_empty:
            for value in sorted(argument.provider(ctx, "", tuple(committed))):
                lines.append(HelpLine(value, argument.description))
        elif partial != "":
            label = argument.existing_label if argument.creatable else argument.description
            for value in sorted(argument.provider(ctx, partial, tuple(committed))):
                lines.append(HelpLine(value, label))

    show_cr = partial == "" and node.command is not None
    return HelpResult(lines, show_cr, partial)


def is_bare_no_context(mode: str, text_before_cursor: str) -> bool:
    """True exactly for the spaced ``no ?`` help context in `mode`: the
    committed text resolves to exactly the "no" keyword and nothing has
    been typed after it yet (`no ?`, not the attached `no?` -- see the
    `token?` vs `token ?` distinction documented throughout help() above).

    Presentation-only query, used by cli/main.py to gate an explanatory
    help footer onto one exact help context. Adds no trie node, and does
    not affect parse()/complete()/help()'s own candidate output -- `no`'s
    real children (e.g. access-info/reference in "running" mode) remain
    exactly as returned by help() itself."""
    committed, partial = split_for_completion(text_before_cursor)
    if partial != "" or len(committed) != 1:
        return False
    root = MODE_ROOTS.get(mode)
    if root is None:
        return False
    no_node = root.literal_children.get("no")
    if no_node is None:
        return False
    return _walk_committed(mode, committed) is no_node
