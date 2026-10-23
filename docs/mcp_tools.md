# MCP Tools

For a shorter, CLI-native summary of this same integration (from the
operator's side), run `help claude` in `./run_cli.sh`.

Network Lab MCP exposes exactly seven tools over stdio. All lab-data tools
reload their inputs from disk on every call (see
[architecture.md](architecture.md#lab-yaml-reload-policy)). Tool results are
returned as structured JSON.

**Committed-state boundary**: these tools only ever read *committed*
`lab/settings.yaml`, `lab/topologies/*.yaml`, and (for `terminal_open()`
only) `lab/access-info/*.yaml` — the same files the human CLI (`./run_cli.sh`)
writes on a successful `commit`. Uncommitted candidate configuration in a CLI
session is never visible here; a successful commit becomes visible on the
very next call to any of these tools, with no MCP server restart required.
See [architecture.md](architecture.md#committed-only-mcp-boundary) and
[cli_reference.md](cli_reference.md).

**Private access-info boundary**: `lab/access-info/*.yaml` (device
address/transport/port/username/password) is never returned by any tool
below. `get_active_topology()` returns only the safe logical topology;
`terminal_open()` resolves access-info internally to open a session, but
returns only the device name, session name, reuse flag, and transport —
never the address, username, or password.

**Active-topology membership**: `terminal_open()`, `terminal_send()`, and
`terminal_read()` all require the device to currently be present in the
active topology (reloaded from disk on every call). `terminal_list()` and
`terminal_close()` are deliberately unrestricted — a managed session left
over from before the active topology changed to no longer include that
device stays visible and closeable, even though `terminal_send()`/
`terminal_read()` will refuse to operate on it. See "Managed sessions vs.
the active topology" below.

## get_active_topology()

**Purpose**: answer "where should I work?" — return the currently active lab
topology.

**Arguments**: none.

**Return value**:

```json
{
  "active_topology": "sample_lab",
  "topology": {
    "name": "sample_lab",
    "description": "...",
    "devices": { "R1": { "type": "iosxr" } },
    "links": []
  }
}
```

A device entry may also carry an `interfaces` mapping (interface name ->
`{ "ipv4_address": "...", "vrf": "..." }`) when Layer-3 enrichment data has
been committed for it — see ["Topology discovery"](architecture.md#topology-discovery)
in the architecture doc. A device entry never includes `address`,
`transport`, `port`, `username`, or `password` — topology only ever holds
safe logical data. Private connection data lives in a separate access-info
definition that this tool never reads or returns; see `terminal_open()`
below for how that gets resolved when a session is actually opened.

**Usage example**: call this first, before touching any device, to learn
which devices and links exist in the active topology.

**Important behavior**: `lab/settings.yaml` and the active topology YAML are
both re-read from disk on every call. If you edit `lab/settings.yaml` (or
commit a running-config change from the CLI) to point at a different
topology, the next call reflects that immediately.

**Error behavior**: raises a tool error (visible to the caller, not a crash)
when `lab/settings.yaml` is missing, the active topology name is invalid, the
referenced topology file does not exist or is not valid YAML, or the
topology's device names cannot be safely mapped to terminal sessions.

## get_execution_instructions()

**Purpose**: answer "what rules must I follow, what must I accomplish, and
what reusable knowledge is available?" — return principles, the active
scenario, and the active references together.

**Arguments** (all optional; with none, behavior is unchanged):

- `inspect_scenarios` (list of strings): stored scenario names whose full content to return.
- `inspect_references` (list of strings): stored reference names whose full content to return.

Names must come from the catalogs below (exact stored definition names, never
paths); duplicates are dropped and requested order is kept. Inspection is
read-only and never activates a definition.

**Return value**:

```json
{
  "principles": { "workspace_principles": [...], "general_operating_principles": [...], "prohibited_actions": [...] },
  "scenario": { "name": "getting_started", "content": { "...": "..." } },
  "references": [ { "name": "cisco_platform_guidance", "content": { "...": "..." } } ],
  "available_scenarios": [ { "name": "getting_started", "description": "..." } ],
  "available_references": [ { "name": "cisco_platform_guidance", "description": "..." } ],
  "inspected_scenarios": [],
  "inspected_references": []
}
```

`available_*` are metadata-only catalogs of every stored definition, sorted
by name (`description` is `null` when a definition has none); access-info is
never included. `inspected_*` hold full content only for what was requested.
Definition changes are not made through MCP: the AI proposes refined or new
scenarios/references and a human reviews and commits them in the CLI.

**Usage example**: call this alongside `get_active_topology()` at the start
of a task to learn how to behave and what the task requires.

**Important behavior**: `lab/settings.yaml`, `lab/principles.yaml`, the
active scenario file, and every active reference file are all re-read from
disk on every call.

**Error behavior**: raises a tool error when `lab/settings.yaml` is missing,
the active scenario or any active reference name is invalid, or the
corresponding file does not exist or is not valid YAML, or when a requested
`inspect_*` name is not an existing stored definition.

## terminal_open()

**Purpose**: open (or reuse) a terminal session for a device in the active
topology, launching `ssh` or `telnet` inside the dedicated tmux environment.

**Arguments**:

- `device` (string, required): the device name as it appears in the active
  topology's `devices` mapping.

**Return value**:

```json
{
  "device": "R1",
  "session_name": "network-lab-device-R1",
  "reused": false,
  "transport": "ssh"
}
```

**Usage example**: `terminal_open(device="R1")`, then use `terminal_read()`
to see the login prompt (or, if authentication already completed
automatically, the device's own exec prompt).

**Important behavior**: the active topology is reloaded from disk before
opening the session, so a device added to `lab/topologies/<active>.yaml` (or
a topology switch in `lab/settings.yaml`) is picked up without restarting the
server. `device` only needs to name a device that exists in the active
topology — Claude never supplies (or sees) an address, username, or
password. Internally, Network Lab MCP resolves the device's private
connection data from running-config's *selected* access-info definition
only (`active_access_info`) — see
[architecture.md](architecture.md#device-access-resolution); no other
committed access-info file is ever searched. If the resolved device
references a `jump_host` (within that same access-info definition),
Network Lab MCP connects via native OpenSSH ProxyJump instead of directly
— see [architecture.md](architecture.md#single-hop-openssh-proxyjump) —
transparently to Claude, which still only ever sees the one resulting
terminal session.

Once the session exists, this tool also **completes target authentication
automatically** using the resolved access-info's own private credentials,
for either transport: an SSH password prompt from the target device itself
(never a jump host's own prompt — that hop must use non-interactive
key/agent authentication), or a classic-IOS-style Telnet `Username:`/
`Password:` sequence. Key/agent SSH authentication that succeeds without
ever showing a password prompt is unaffected. A host-key confirmation
prompt, or any other interactive step this cannot safely resolve on its own,
is still left for `terminal_read()`/`terminal_send()` to handle. No
credential value is ever returned by this tool or any other, and this
automated authentication is fully idempotent — calling `terminal_open()`
again against an already-authenticated session sends nothing further. See
["Managed-terminal private authentication"](architecture.md#managed-terminal-private-authentication)
for the full design.

This active-topology-membership restriction is a separate code path from
Discovery: `discover topology`'s private bootstrap connectivity (which does
automate login, only for its own temporary sessions) never requires
active-topology membership and is never reachable through this tool — see
[architecture.md](architecture.md#discovery-bootstrap-session-namespace).

**Error behavior**: raises a tool error (fail closed, never a silent guess)
when:

- the device is not present in the active topology;
- no access-info is selected in running-config (`% No access-info is
  selected in running-config.`);
- the selected access-info definition does not exist (`% Selected
  access-info '<name>' does not exist.`);
- the device is not present in the selected access-info definition (`%
  Device '<device>' is not present in access-info '<name>'.`) — there is
  no fallback search through any other access-info file;
- the topology and resolved access-info both specify `type` and, once
  normalized, they disagree (`% Device type mismatch for '<device>' between
  topology and access information.`);
- the device references a `jump_host` that does not exist in the same
  access-info definition;
- authentication definitively fails or is rejected (either transport);
- the device's transport is unsupported, the required `ssh`/`telnet` binary
  is unavailable, or the device name cannot be mapped to a valid session
  name.

No error message from this tool ever includes a credential value.

## terminal_send()

**Purpose**: send input to a device's open terminal session.

**Arguments**:

- `device` (string, required)
- `text` (string, optional): literal text to send (not interpreted as tmux
  key names).
- `keys` (list of strings, optional): special keys to send, e.g. `"C-c"`,
  `"Tab"`, `"Space"`, `"Up"`, `"Enter"`, `"M-x"`.
- `enter` (boolean, optional, default `false`): send Enter last.

**Return value**:

```json
{ "device": "R1", "session_name": "network-lab-device-R1" }
```

**Usage example**:

```
terminal_send(device="R1", text="show version", enter=true)
```

**Important behavior — deterministic execution order**. When more than one
of `text`, `keys`, and `enter=true` is supplied, they are always applied in
this fixed order, with no deduplication:

1. If `text` is given, it is sent literally first.
2. If `keys` are given, they are sent next, in the supplied order.
3. If `enter` is `true`, Enter is sent last.

Examples:

| Call | Effect |
|------|--------|
| `text="show version", enter=true` | send `"show version"`, then Enter |
| `keys=["C-c"]` | send only Ctrl-C |
| `text="something", keys=["Tab"], enter=true` | send `"something"`, then Tab, then Enter |
| `keys=["Enter"], enter=true` | Enter is sent **twice** — nothing is deduplicated |

`text` may contain credentials (e.g. a password prompt response). It is
delivered to the pane without ever passing through a subprocess's own argv
or appearing in an error message, and this MCP server does not separately
log it or echo it back in the tool's response. It is not hidden from the
device's own terminal transcript, though: this project intentionally
records the raw pane output via tmux `pipe-pane` (see [`show
logging`](cli_reference.md#show-logging)), and if the remote endpoint
echoes back what it received, that echo may naturally appear in that
transcript like any other terminal output.

**Important behavior — active-topology membership**: the device must still
be present in the active topology, reloaded from disk on every call. An
already-open session does not stay usable if the active topology changes to
no longer include this device — use `terminal_close()` and reopen it (or
switch the active topology back) instead.

**Error behavior**: raises a tool error when the device is not present in
the active topology, the device's session does not exist (call
`terminal_open()` first), or an unsupported key name is given in `keys`.

## terminal_read()

**Purpose**: capture recent terminal output for a device, so the caller can
see the current prompt, command output, a password/interactive prompt,
paging state, configuration-mode/administrative-mode transitions, or
unexpected errors.

**Arguments**:

- `device` (string, required)
- `lines` (integer, optional, default `100`): how many recent lines of
  content to return.

**Return value**:

```json
{ "device": "R1", "session_name": "network-lab-device-R1", "content": "..." }
```

**Usage example**: `terminal_read(device="R1", lines=200)` after sending a
command that produces a lot of output.

**Important behavior**: this tool does not attempt to parse or classify
device prompts (IOS XR or otherwise) — it hands back raw recent pane
content and leaves interpretation to the caller. The underlying tmux session
retains a much larger scrollback buffer than the default `lines`, so a
larger `lines` value can be requested when needed without losing history.

**Important behavior — active-topology membership**: same rule as
`terminal_send()` above — the device must still be present in the active
topology, reloaded from disk on every call.

**Error behavior**: raises a tool error when the device is not present in
the active topology, or the device's session does not exist.

## terminal_list()

**Purpose**: list every currently managed production terminal session.

**Arguments**: none.

**Return value**:

```json
{
  "sessions": [
    { "device": "R1", "session_name": "network-lab-device-R1", "state": "running" }
  ]
}
```

**Usage example**: call this to see which devices already have an open
session before deciding whether to call `terminal_open()` again, or to find
a stale session that needs to be closed with `terminal_close()`.

**Important behavior**: this tool lists **every** managed production
session, regardless of whether each device is still present in the active
topology — including a stale session left over from before the active
topology changed. That session remains visible and closeable via
`terminal_close()` even though `terminal_send()`/`terminal_read()` will
refuse to use it (see "Active-topology membership" above). Local validation
sessions (`network-lab-validation-*`) are never exposed as topology devices
by this tool, regardless of what a device happens to be named —
classification is by structural namespace prefix, not by searching for the
substring `"validation"`. A legitimate device named `validation-router`
(session `network-lab-device-validation-router`) is listed normally.

**Error behavior**: does not raise on an empty session list; returns
`{"sessions": []}`.

## terminal_close()

**Purpose**: close a device's managed production terminal session.

**Arguments**:

- `device` (string, required)

**Return value**:

```json
{ "device": "R1", "session_name": "network-lab-device-R1", "closed": true }
```

`closed` is `false` (not an error) if there was no session to close.

**Usage example**: `terminal_close(device="R1")` once a task involving that
device is finished, or to clean up a stale session for a device no longer in
the active topology.

**Important behavior**: this tool always derives a production session name
(`network-lab-device-<device>`) and can never target a validation-only
session, even if a caller-supplied name happens to look similar to a
validation identifier. Deliberately does **not** require the device to
still be present in the active topology, so a stale session left over from
before the active topology changed can always be closed.

**Error behavior**: raises a tool error only when the device name itself
cannot be mapped to a valid session name; a missing session is reported as
`closed: false`, not as an error.

## Managed sessions vs. the active topology

| Tool | Requires active-topology membership? |
|---|---|
| `terminal_open()` | Yes |
| `terminal_send()` | Yes |
| `terminal_read()` | Yes |
| `terminal_list()` | No — lists every managed session |
| `terminal_close()` | No — can always close a stale session |

Managed sessions are persistent (they live in tmux, independent of the MCP
server process) and survive a running-config change that removes a device
from the active topology. `terminal_open()`, `terminal_send()`, and
`terminal_read()` all re-verify active-topology membership on every call so
a stale session cannot silently continue being driven after the topology
changed out from under it. `terminal_list()`/`terminal_close()` stay
unrestricted on purpose: a stale session must remain discoverable and
closeable, or it could never be cleaned up.
