# Architecture

## Design goals

Network Lab MCP provides an "AI Network Engineer Workspace" designed to
maximize the reasoning capabilities of AI in network lab environments.

Network Lab MCP is **not** a network engineering reasoning engine. It is a
thin layer that gives an MCP-capable AI client (for example Claude Code or
Codex CLI) two things:

1. **Lab knowledge** — where the work happens, how to behave, what to
   accomplish, and what reusable knowledge already exists.
2. **Terminal access** — a real, general-purpose way to interact with lab
   devices, close to how a human operator would use a terminal, without
   ever handing the AI client the private connection details.

All actual network engineering judgment — what command to run next, how to
interpret output, when a task is done — is left to the AI client. This drives
every other design choice in this document:

- **Fail closed, never guess.** A missing selection, an unresolvable device,
  a type mismatch, an ambiguous Discovery neighbor — each is a clear,
  sanitized error rather than an inferred default.
- **Human-observable terminals.** Sessions are driven the way a human
  operator would drive one (read, decide, send), in a dedicated tmux
  environment a human can watch live.
- **Private data stays private.** Access-info (credentials, addresses) is
  structurally separate from topology (safe logical data) and never crosses
  the MCP boundary.
- **Candidate before commit.** The human configuration CLI never writes
  disk, and Claude Code never sees a change, until an explicit commit
  succeeds.
- **Discovery informs, never decides.** Topology discovery only ever
  produces a candidate for human review — it never auto-commits and never
  auto-selects the result as the active topology.
- **Minimal surface.** Exactly seven MCP tools; no batch/parallel tool, no
  generic automation framework, no second persistence layer alongside lab
  YAML and tmux.

## System overview

```
Claude Code                          Human Operator
    |                                     |
Network Lab MCP (this repository)   ./run_cli.sh (this repository)
    |                                     |
dedicated tmux environment           Candidate configuration -> commit
(socket: network-lab-mcp)                 |
    |                                Committed lab YAML
ssh / telnet                              |  (running-config, topology,
    |                                     |   access-info, scenario, reference)
Lab Devices                          (read by Network Lab MCP above)
```

Two processes share one lab root:

- **The MCP server** (`network-lab-mcp`, `src/network_lab_mcp/mcp_server.py`)
  — a stdio MCP server exposing exactly seven tools to an MCP client. It
  holds essentially no state of its own: lab YAML on disk is the single
  source of truth for configuration, and tmux is the single source of truth
  for terminal session lifetime.
- **The human configuration CLI** (`./run_cli.sh`,
  `src/network_lab_mcp/cli/`) — an IOS XR-compatible CLI a human operator
  uses to create and edit lab definitions and the running-config selection,
  through a candidate/commit model. It is a separate process that never
  speaks the MCP stdio protocol, and it can also give a human a live,
  read-only view of a terminal session the AI is driving (`monitor
  terminal`).

Both read and write the same committed lab YAML under `lab/`; a successful
`commit` from the CLI is visible to the MCP server on its very next tool
call, with no server restart.

## Configuration model

Five kinds of lab data are deliberately kept separate (see README.md's
[How it works](../README.md#how-it-works) for the reader-facing summary):

```
Human
  |
  +--> running-config
  |      access-info / topology / scenario / references selected for MCP
  |      (lab/settings.yaml -- a SELECTION, not a definition)
  |
  +--> access-info
  |      private device access (address/transport/port/username/password)
  |      (lab/access-info/*.yaml -- never exposed to Claude)
  |
  +--> topology
  |      safe logical model (devices, device type, links)
  |      (lab/topologies/*.yaml -- exposed to Claude)
  |
  +--> scenario
  |      what Claude should do (lab/scenarios/*.yaml)
  |
  +--> reference
  |      reusable knowledge (lab/references/*.yaml)
  |
  v
Network Lab MCP
  |
  +--> safe topology
  +--> scenario
  +--> references
  |
  v
Claude Code
```

`get_active_topology()` answers "where should I work?" `get_execution_instructions()`
answers "what rules must I follow, what must I accomplish, and what reusable
knowledge is available?" combining principles, the active scenario, and active
references into one structured result. Neither tool tells Claude Code *how*
to solve the problem, and neither ever returns access-info.

The Workspace is deliberately **not** managed by the MCP server. Claude Code
uses its own current working directory (the task workspace it was started
from) to store evidence, configurations, and reports, organized however the
task requires. Network Lab MCP has no `runtime/` directory and keeps no
records of what Claude Code produces.

### running-config cardinality

`running-config` answers *which* committed definitions are currently
active -- never their content. Each definition type has a different
cardinality, and the CLI's `show running-config <definition-type>`
views (see [cli_reference.md](cli_reference.md#show-running-config-definition-type))
follow it exactly:

| Definition type | Cardinality | `show running-config <type>` |
|---|---|---|
| `access_info` | zero or one | no `<name>`; zero is a legitimate, non-error state |
| `topology` | exactly one, in a valid running state | no `<name>`; missing/invalid is a broken state (fails) |
| `scenario` | exactly one, in a valid running state | no `<name>`; missing/invalid is a broken state (fails) |
| `references` | ordered list -- zero, one, or many | multi-select: bare = all, in committed order; `<name>` = one *active* member only |

These views are a read-only dereference of committed state, never a
general definition-name browser: `access-info`/`topology`/`scenario`
never take a `<name>` (there is at most one active definition to
dereference), and `reference <name>` only accepts a name that is
currently in committed `active_references` -- a reference that exists on
disk but isn't selected is rejected, exactly like it isn't part of
running-config anywhere else in this project.

## MCP interface

```
Claude Code
    |
Network Lab MCP
    |  get_active_topology()
    |  get_execution_instructions()
    |  terminal_open() / terminal_send() / terminal_read()
    |  terminal_list() / terminal_close()
    v
dedicated tmux environment (socket: network-lab-mcp)
    v
ssh / telnet
    v
Lab Devices
```

The MCP server itself is a thin translation layer: lab tools read YAML from
disk and return it as structured data; terminal tools translate a logical
device name into tmux commands, privately resolving access-info along the
way (see "Device access" below). See [mcp_tools.md](mcp_tools.md) for the
full per-tool contract, including arguments, return shapes, and error
behavior.

### Lab YAML reload policy

None of `get_active_topology()`, `get_execution_instructions()`, or
`terminal_open()` cache lab YAML at server startup. Each reads
`lab/settings.yaml` and whatever it references (including, for
`terminal_open()`, every `lab/access-info/*.yaml` file) fresh from disk on
every call. This means editing lab YAML, or committing a change from the
human CLI, takes effect on the very next tool call, without restarting the
MCP server.

### Managed sessions vs. the active topology

`terminal_open()`, `terminal_send()`, and `terminal_read()` all require the
device to currently be present in the active topology (verified fresh on
every call via `lab.verify_device_in_active_topology()`); `terminal_list()`
and `terminal_close()` are deliberately unrestricted. This matters because
managed sessions are persistent (tmux, not the MCP server, is their source
of truth) and outlive a running-config change: without this check, a
session opened while a device was in the active topology could keep being
driven after that device left it, silently bypassing `terminal_open()`'s
own membership gate. `terminal_list()` stays unrestricted so a stale
session remains discoverable, and `terminal_close()` stays unrestricted so
a stale session is never unclosable. See [mcp_tools.md](mcp_tools.md#managed-sessions-vs-the-active-topology)
for the full per-tool table.

## Device access resolution

`terminal_open(device)` receives only a logical device name from Claude —
never an address, username, or password, since topology no longer carries
private access fields at all.

```
Claude / terminal_open("R1")
  |
  v
MCP reads committed running-config, resolves active topology
  |
  v
MCP verifies R1 exists in active topology
  |
  v
MCP private access resolver
  |
  +--> resolve running-config's selected access-info (active_access_info)
  |
  +--> none selected           -> fail closed
  +--> selected file missing   -> fail closed
  |
  +--> load ONLY that access-info definition, look up R1
  |
  +--> not present in it       -> fail closed  (no other file is searched)
  |
  +--> compare topology/access-info device type
  |       mismatch -> fail closed
  |
  +--> device has a jump_host reference?
  |       yes -> resolve it within the same access-info definition
  |              -> attach as jump_host_config (fails closed if the
  |                 referenced jump host is somehow absent)
  |       no  -> nothing to attach
  |
  v
tmux / native OpenSSH (direct, or -J ProxyJump if jump_host_config is set) / telnet
  |
  v
Device (optionally via one jump host)
```

This is implemented by `lab.get_device()`: it reads
`get_active_access_info_name(settings)` (running-config's
`active_access_info` — a missing field means "none selected", not an
error), loads *only* that one access-info definition, looks up the device
in it, and (if the device references a `jump_host`) resolves that jump
host within the same definition. No credential value ever appears in a
"no access-info selected", "does not exist", "not present", "type
mismatch", or "unknown jump host" error.

### Access-info lookup is scoped to one selected definition

Only the definition named by running-config's `active_access_info` is ever
read for device resolution — there is no fallback search across every
committed `lab/access-info/*.yaml` file. The same device ID may safely
appear in other, unselected access-info files:

```
lab/access-info/lab_a.yaml   R1
lab/access-info/lab_b.yaml   R1
```

With `active_access_info: lab_a`, `terminal_open("R1")` resolves `lab_a`'s
`R1` only; selecting `lab_b` instead resolves `lab_b`'s `R1` instead.
Selecting a different access-info in running-config (`cli/config.py`'s
`select_access_info()`/`clear_access_info_selection()`) is what changes
which one resolves a given device — never a matching topology filename,
edit recency, or alphabetical order.

### Single-hop OpenSSH ProxyJump

A device's optional `jump_host` field names one jump host, within the same
access-info definition, that `terminal.py` connects through using native
OpenSSH's own `-J` option (`ssh -J jump-user@jump-host:jump-port -p
port user@target`) instead of a direct connection —
`terminal._build_transport_command()` builds this argv (list-based, never
`shell=True`, never a password) purely from the resolved device/jump-host
dicts, with no shell-hop automation: OpenSSH itself tunnels the second SSH
connection through the first, and the tmux pane still only ever shows the
one resulting interactive session that `terminal_read()`/`terminal_send()`
already know how to drive. `lab.validate_access_info_data()` (via
`validate_jump_hosts()`/`validate_device_jump_host_references()`) is the
single validation primitive enforcing "jump host type is always `host`",
"jump host transport is always `ssh`", "a device using `jump_host` must
itself use `transport: ssh`", and "the referenced jump host exists" —
applied identically whether the data came from a CLI commit or a manually
edited file. Nesting one jump host behind another is not represented in
the schema at all, so multi-hop chains cannot occur even by mistake.

### Topology/access-info device.type consistency

Topology and access-info are independent, separately authored definitions,
so nothing stops them from disagreeing about the same device's platform.
`terminal_open()` performs a lightweight runtime check when resolving a
device — not a general cross-file consistency framework — and fails closed
if both sides specify `type` and, once normalized through the shared
`DEVICE_TYPES` enum, they disagree (`% Device type mismatch for '<device>'
between topology and access information.`). If either side leaves `type`
unset (already allowed), there is nothing to compare and resolution
proceeds normally. Credential values are never included in this error.

## Terminal architecture

Terminal sessions are managed with tmux, using a dedicated named socket
(`tmux -L network-lab-mcp ...`) so that Network Lab MCP never interferes with
a user's own interactive tmux sessions. tmux itself — not any structure
inside the MCP server — is the source of truth for which sessions exist and
whether they are still running. The MCP server does not keep a session
database; a restarted server rediscovers existing sessions by asking tmux.

SSH and Telnet are launched as the operating system's own `ssh`/`telnet`
binaries, inside a managed tmux pane. Network Lab MCP does not use a Python
SSH library and does not use `pexpect` as its core mechanism, and it does not
attempt to parse device prompts (IOS XR or otherwise). Instead, Claude Code
reads the pane with `terminal_read()`, decides what a prompt means, and
responds with `terminal_send()` — the same loop a human operator would use.

### Structurally separate session namespaces

Three structurally distinct namespaces exist, chosen by the type of caller
(production tool, Discovery, or internal validation), never by
pattern-matching on a device's name:

```
Production:  network-lab-device-<device-id>
Discovery:   network-lab-discovery-<device-id>
Validation:  network-lab-validation-<validation-id>
```

`derive_production_session_name()`, `derive_discovery_session_name()`, and
`derive_validation_session_name()` are the only functions that produce
these names, and each only ever produces a name in its own namespace.
Because the three prefixes are fixed and distinct strings, a session name
can belong to at most one namespace — there is no ambiguity to resolve at
runtime. Concretely, a topology device literally named `validation-router`
maps to the production session `network-lab-device-validation-router`; it
is not, and cannot become, a validation-namespace or Discovery-namespace
session.

The public tools (`terminal_open`, `terminal_send`, `terminal_read`,
`terminal_list`, `terminal_close`) only ever derive and act on production
session names. Discovery's bootstrap connectivity reuses the exact same
`_build_transport_command()` (direct SSH and single-hop ProxyJump alike)
and session primitives as production, in its own structurally separate
namespace, so a Discovery session can never collide with, appear in, or be
closed by any public `terminal_*` tool call. A small set of internal
validation helpers (`open_validation_session`, `send_to_validation`,
`read_validation`, `list_validation_sessions`, `close_validation_session`
in `terminal.py`) only ever derive and act on validation session names, and
exist purely to exercise the shared session-management primitives locally
against a safe local command (e.g. `cat`) rather than `ssh`/`telnet` — this
is internal only, adding no public "shell" transport and no new MCP tool.

Because Discovery must work before a topology fully exists yet (its whole
point is to help build one), its bootstrap session is opened directly
against a selected access-info device by logical ID, with no
active-topology membership check at all — unlike `terminal_open()`, which
requires the device to be in the active topology. This is intentional and
does not weaken `terminal_open()`'s own restriction, which is a completely
separate code path. The one difference passed to `_build_transport_command()`
for Discovery is `accept_new_host_keys=True` (`ssh -o
StrictHostKeyChecking=accept-new`): Discovery is unattended, with no human
to answer an interactive host-key confirmation prompt, so it avoids that
prompt outright for a genuinely new host key, rather than automating the
confirmation. It never bypasses a *changed*-host-key failure
(`StrictHostKeyChecking=no`/`UserKnownHostsFile=/dev/null` are never used)
— that remains a hard failure the operator must resolve themselves (e.g.
`ssh-keygen -R <address>`) after independently verifying the new
fingerprint really is the expected device.

### Concurrency model

The core invariant: **different devices execute concurrently; the same
device's operations are serialized.**

```
R1 ---------------->
R2 ---------------->        different devices: parallel
R3 ---------------->

R1 open/send/read/close      same device: serialized through one lock
```

**MCP boundary.** The public MCP interface stays exactly the seven tools
listed above -- there is no `terminal_send_parallel()` or batch API.
Parallelism is a property of the existing tools' execution, not a new tool.
The MCP SDK in use (`mcp` 2.2.0) already supports this with no change to
`mcp_server.py`'s dispatch model: every incoming `tools/call` request (other
than the connection handshake) is spawned as its own `anyio` task rather
than awaited in place, and each `@mcp.tool()` function here is a plain
synchronous `def`, which the framework invokes via
`anyio.to_thread.run_sync()` -- offloaded to a real worker thread, never
blocking the event loop other requests share. Two different-device tool
calls issued back-to-back by an MCP client can therefore already overlap.

**terminal.py serialization.** `terminal.py` keeps no session registry of
its own (tmux remains the sole source of truth), so most operations are
naturally device-isolated -- every tmux command is scoped to one session
name derived from the device. Two real races are closed by one
`threading.Lock` per underlying tmux session name (`terminal._session_lock()`,
a small process-lifetime registry keyed by the already-validated session
name -- never a single lock shared by every device, which would serialize
all devices and defeat the point): same-device `open`/`send`/`read`/`close`
interleaving (e.g. two concurrent `terminal_open()` calls for the same
device both seeing no existing session and both trying to create it), and a
rare cross-device race on the very first session the tmux server ever
creates. Each public per-session operation (`open_device_terminal`,
`send_to_device`, `read_device`, `close_device_terminal`, and the private
Discovery bootstrap equivalents) holds that one lock for its whole body.
The cross-device first-bootstrap race is handled separately, by making
`_ensure_tmux_environment()` tolerate losing that race rather than by a
second lock. `terminal_list()` stays unlocked: it is a read-only query
that tmux itself answers atomically.

**Discovery's own per-device collection also runs concurrently** (bounded
`concurrent.futures.ThreadPoolExecutor`, `DISCOVERY_MAX_WORKERS = 8` — a
small internal constant, not a CLI/config knob), while a single device's
own command sequence stays strictly ordered within its own worker.
Aggregation and error reporting are always by original target order, never
by whichever thread happened to finish first, so scheduler order can never
change which device's result lands where or which device's failure is the
one reported. Any collection failure — expected or an unexpected worker
exception (converted to a bounded error, never a leaked raw traceback) —
still fails the whole operation with zero candidate mutation.

### Live read-only human monitoring

```
                        AI / MCP
                           |
                    managed terminal
                           |
                           v
                  network-lab-device-R1
                           |
                           |
                           +------+
                                  |
                                  v
                         terminal monitor R1
                                  |
                                  +----> read-only observation
                                  |            |
                                  |            v
                                  |     incremental stream delta
                                  |            |
                                  |            v
                                  |   local terminal scrollback
                                  |            +
                                  |      live bottom status
                                  |            |
                                  |          Human
                           +------+
                           |
                  network-lab-discovery-R1
                           ^
                           |
                    Discovery engine
```

`terminal monitor <device-id>` (EXEC only) gives a human a live view of
whichever Network Lab MCP terminal session currently has priority for that
device, without ever becoming a second writer to it. It is device-oriented,
not tmux-session-oriented: source priority is

    managed session  >  Discovery session  >  waiting

It is built entirely on `terminal.capture_device_terminal_view()` -- a
small, pure observation function alongside the existing production API,
which tries the normal managed session
(`derive_production_session_name()`) first and only falls back to a
Discovery bootstrap session (`derive_discovery_session_name()`) for the
same device if no managed session currently exists, via one shared
private helper (`_observe_named_session()`) for both -- no second
observation implementation. Both paths use only `_pane_state()`/
`_capture_pane()` (has-session/list-panes/capture-pane equivalents), never
`send-keys`/`new-session`/`kill-session`; observing a Discovery session
never creates one (only `discover_topology()` does that) and never delays
its cleanup. It is deliberately not wrapped in the per-device lock the
concurrency model above uses: every call it makes is already a plain read,
the only "race" it could have (a session disappearing between its own two
tmux calls, in either namespace) is exactly the WAITING/fallback transition
it is designed to tolerate rather than prevent, and since `monitor
terminal` normally runs in a separate `./run_cli.sh` process with its own
empty, process-local lock registry, taking that lock here could not
provide real cross-process exclusion anyway.

The monitor models exactly three states -- `waiting` (neither session
exists), `active` (the selected session/pane is alive), `ended` (pane
exists but its process exited, via tmux's `remain-on-exit`) -- deliberately
nothing richer (no attempt to infer router/BGP/SSH-auth state from tmux),
plus which session `status` describes (`source`: `"managed"`,
`"discovery"`, or `"none"`). Priority is re-evaluated fresh on every
observation, so a managed session appearing while Discovery is being shown
(or disappearing and falling back to a still-active Discovery session)
needs no monitor restart -- the very next refresh reflects it. Monitor
lifetime is independent of session lifetime by design: it starts in
`waiting` if opened before either session exists, survives disappearance/
`ended` in either namespace without exiting, and automatically resumes
live display once a session (managed or Discovery) exists again; only the
human quitting (`q`/`Q`/Ctrl-C) ends it.

**Scrollback-preserving UI.** The monitor's `prompt_toolkit` `Application`
is `full_screen=False` -- it never switches to the alternate screen
buffer, so everything it prints stays in the terminal emulator's own
normal scrollback exactly like ordinary command output, both during and
after the run. A background `asyncio` task (`cli/main.py`'s
`_monitor_poll_loop()`) polls `capture_device_terminal_view()`, and prints
any new activity via `run_in_terminal()` (the same "print permanently above
a live area" mechanism the CLI's own `?`/Tab key bindings already use) --
never a manual `termios`/`tty`/`fcntl` clear/redraw. Only a small 3-line
status block at the bottom (`Monitoring terminal <device> | Read-only |
Source: ... | Status: ... | q: quit`, width-adaptive via
`shutil.get_terminal_size()`) is continuously redrawn in place; on exit,
`prompt_toolkit`'s own `renderer.erase()` removes just that live area,
leaving everything already printed untouched.

**Incremental stream (`cli/main.py`'s `_monitor_stream_step()` /
`_MonitorStreamCursor`).** Each poll calls `capture_device_terminal_view()`
with `lines=terminal.HISTORY_LIMIT` (its full-history mode -- tmux's
`capture-pane -S -` already captures up to the 20000-line history-limit
regardless of the `lines` argument, so this costs no extra tmux call
compared to a small on-screen window; only how much of that same captured
text is returned changes). The cursor tracks, per monitor run, how many
lines of the current source's output have already been streamed, and diffs
by list position (never by string search), so: repeated identical lines
(e.g. duplicate routes) are never collapsed; output that arrives in a burst
larger than the pane's own visible height is never lost merely because the
visible pane scrolled, since the full history buffer -- not just the screen
-- is what gets compared; and an unchanged poll appends nothing. A session
being recreated under the same name is detected without any extra tmux
identity query (no `pane_id`/`session_id` lookup): a fresh pane's history
never shares the previously seen prefix, so the same position-based
integrity check that powers normal incremental diffing also catches
recreation, treated identically to an explicit source switch -- both start
a bounded initial-context window (`terminal.DEFAULT_READ_LINES`, matching
the "visible pane" convention) rather than replaying the entire history,
and both print a small one-line `[monitor] ...` marker (`started`/
`switched to ...`/`ended; waiting`/`resumed`) so scrollback stays legible
without being flooded on every ordinary poll. The one honestly-acknowledged
limitation: if a single session's own output exceeds the 20000-line
history-limit, tmux itself starts evicting its oldest lines, which this
cursor cannot distinguish from a genuine recreation -- handled the same
safe way (a bounded fresh context, never a crash or silently dropped
correctness), just occasionally reprinting a small amount of already-seen
tail content in that rare case.

Multiple monitors -- of the same or different devices, from separate CLI
processes -- are fully independent: tmux remains the only session state,
so there is no monitor registry, daemon, or IPC layer to keep in sync, and
each monitor's `_MonitorStreamCursor` is local to its own process/run.
`terminal monitor <device-id>`'s target eligibility (the committed active
topology's devices, union'd with devices that already have an existing
managed *or* Discovery session) is likewise read fresh each time, so a
device being discovered for the first time -- not yet in any committed
topology -- is still a valid target the moment its Discovery session
exists.

The local terminal scrollback this produces is a presentation convenience
for the human, not a second source of truth: tmux remains the session SSOT,
and the persistent per-session log file (`logs/terminal/<device-id>/*.log`)
remains the durable historical-evidence SSOT, unaffected by any of this --
the monitor never creates a log, never touches `pipe-pane`, and never
writes captured pane text anywhere but the local terminal.

### Persistent terminal session logging

Every device session -- production and Discovery's private bootstrap
sessions alike -- is logged via tmux's own `pipe-pane` mechanism
(`tmux pipe-pane -o -t <session> 'cat >> <logfile>'`), started right after
the session is first created (never on reuse, since the pipe stays
attached for the pane's whole lifetime). This is the only logging
mechanism: nothing here re-renders or duplicates pane content into a
second application log, and `terminal_send()`'s payload is never logged
separately from what the pane/log itself already shows. tmux's pane
remains the runtime session source of truth and `terminal_read()` is
completely unaffected -- the log is a separate, write-only, persistent
historical record at `logs/terminal/<device-id>/<session-start>.log`
(`YYYYMMDDTHHMMSS`), which is gitignored. `show logging` (EXEC only, see
[cli_reference.md](cli_reference.md#show-logging)) is the only reader of
these files.

### Terminal log deletion

`delete logging all` / `delete logging <device-id> all` / `delete
logging <device-id> <log-file>` (files only) and `delete logging all
directory` / `delete logging <device-id> directory` (files, then the
now-empty device directory itself) (EXEC only) are the only writers
besides the logging mechanism itself. Every one of them, and every path
through `terminal.py`'s deletion backend, is built on the exact same
enumeration `show logging` reads (`list_logged_device_ids()` /
`list_device_logs()`) -- a symlink (a log file, or a device directory
itself) is excluded, never followed or treated as eligible.

`terminal.DeletionPlan` is the shared unit of work: an immutable,
comparable (`==`) snapshot of exactly which files and which device
directories one operation would touch. Each `build_*_deletion_plan()`
function (`build_file_deletion_plan`, `build_device_all_deletion_plan`,
`build_device_directory_deletion_plan`, `build_global_all_deletion_plan`,
`build_global_directory_deletion_plan`) either raises `TerminalError`
(nothing eligible, or something unsafe) or returns a plan; none of them
ever prompt or read input -- `cli/main.py` owns confirmation and message
text entirely (deletion backend never handles interactive `[y/N]` input
itself). `apply_deletion_plan()` unlinks the plan's files, then `rmdir`s
its directories (never `shutil.rmtree`/a recursive delete) -- non-recursive
by construction, so an unexpected directory content can only ever block a
plan at build time, never cause a partial deletion at apply time.

`cli/main.py`'s `_confirm_and_apply()` is the shared confirm-then-verify
flow every destructive handler uses: build the plan once (to compute
counts for the `[y/N]` message), confirm, then **build the plan again**
and compare it to the first one before ever calling
`apply_deletion_plan()`. Since preflighting device directories,
enumerating files, and checking active writers are all cheap, read-only
operations, re-running the exact same builder is sufficient to catch
anything that changed while the operator was deciding -- a new log
file, a session that started logging, or a directory that gained an
unexpected entry -- without a bespoke diff/lock mechanism. A mismatch (or
a re-preflight failure, which propagates as an ordinary `TerminalError`)
aborts with nothing deleted; only an unchanged, still-safe plan is ever
applied. `_read_confirmation_line()`/`_confirm_delete()` use plain
`input()`, never `PromptSession`, so a confirmation answer can never
reach the CLI's command-history masking. A destructive command reached
through `execute_input_block()`'s multi-line paste path always fails
closed (`execute_command_line(..., interactive=False)` threads a synthetic
`"_interactive": False` into the handler's `args`) instead of blocking on
stdin or risking the next pasted line being misread as the answer --
every other handler ignores that key entirely.

Active-writer protection is the central safety property, and its
granularity is a documented, deliberate limitation rather than an
oversight: both a production session and a Discovery bootstrap session
attach `pipe-pane` logging to the *same* `logs/terminal/<device-id>/`
directory, keyed only by device name (see above), and that attachment is
never explicitly detached before the session ends. Nothing in this
architecture records, anywhere retrievable after session creation, which
exact log file a live session is piping to -- `_start_session_logging()`
computes that path once and its return value is discarded by both
`open_device_terminal()` and `open_bootstrap_terminal()`. The only
reliable, provable primitive is therefore device-level:
`terminal._device_has_active_session(device_name)` checks whether a
production (`derive_production_session_name()`) or Discovery
(`derive_discovery_session_name()`) session currently exists for that
exact device -- if either does, deletion is rejected for **all** of that
device's stored logs, not a guessed "active" one. Validation sessions
never attach logging (`open_validation_session()` never passes
`log_device_name`), so they are never a protection concern here, and
tmux namespace classification is otherwise irrelevant to this check: a
Discovery session is protected because it writes an eligible log, not
because of its namespace.

`delete logging all` / `all directory` / `<device-id> all` / `<device-id>
directory` all preflight their entire target set (files, and for a
`directory` form, directory contents/writers too) before ever asking for
confirmation -- if any targeted device has an active writer, or (for a
`directory` form) any targeted directory contains anything other than
eligible log files, nothing at all is deleted, including the logs of
devices that are themselves inactive. `build_global_directory_deletion_plan()`
reuses `build_device_directory_deletion_plan()` once per valid device
directory rather than duplicating that per-device safety logic, so the
first unsafe device's own error (already naming that device) blocks the
whole operation. `logs/terminal/` itself is never a removal target, only
its valid, non-symlink direct child device directories are; unrelated
files directly under it are left alone. Deletion never closes a session,
stops `pipe-pane`, or otherwise touches session lifecycle; that remains
entirely the concern of `terminal_close()`/Discovery's own cleanup. If a
production or Discovery session currently exists for a device, an attempt
to delete its logging directory fails with:

```
% Cannot delete logging directory for '<device>' while a managed terminal session is still open.
```

### Minimal internal command runner

Discovery's login (`_login()`) and per-command execution (`_run_command()`
in `discovery.py`) poll the pane (`_wait_for_pattern()` in `terminal.py`)
for an IOS XR prompt regex (`RP/\S+/CPU\d+:<hostname>#`, which also
doubles as the hostname source -- see below) or a password prompt, with a
bounded timeout that fails closed (`TerminalError`/`DiscoveryError`, never
a silent guess or infinite wait). This is deliberately minimal: there is
no generic expect library, no IOS XR CLI state machine, and no terminal
automation framework -- just "send one command, wait for the prompt to
come back, slice out the output between the echoed command and the
prompt."

`_run_command()`/`_extract_command_output()` take the "end of output"
prompt regex as a parameter (default: IOS XR's) rather than hard-coding
it, which is what lets `_bootstrap_collect_iosxe()`/`_bootstrap_collect_
ios()` reuse the exact same send/wait/slice runner with the shared
classic-IOS-style prompt shape (`_IOS_STYLE_PROMPT_RE`, matching a
`hostname#`/`hostname>` line in full) instead of writing a second command
runner. The L3 enrichment commands are sent only when the collector is
called with `include_l3=True` (`discover topology l3`) and then use the same
strict runner: any transport failure is fatal like every other command.

Classic-IOS-style login (`_login_ios_style()`) is a separate, small
function from `_login()` -- not a generalized/parameterized version of it
-- since IOS XR's `_login()` waits for an IOS-XR-specific prompt shape
that classic IOS/IOS XE never produces. It answers at most one optional
`Username:` prompt (some login configurations show one, some don't) and
one `Password:` prompt, reusing `_resolve_login_password()`/`terminal.
resolve_target_password_prompt()` unchanged for the password itself (see
"Shared safe SSH password-prompt attribution" below for why no
telnet-specific attribution logic was needed). `_bootstrap_collect_iosxe()`
and `_bootstrap_collect_ios()` remain two separate, explicit collector
functions (each listing its own small set of commands) rather than one
parametrized collector — this project deliberately avoids a generalized
multi-vendor collection framework.

### Discovery disables terminal paging before collection

Discovery disables terminal pagination (`terminal length 0`) immediately
after successful login, before collecting any command output.
`_disable_terminal_paging(device_id, prompt_re)` is a small helper -- send
`terminal length 0`, wait for the device's own prompt to return, fail
closed (bounded `TerminalError`, converted to `DiscoveryError` with a
phase-specific message) if it doesn't -- called exactly once, immediately
after a successful login, by all three collectors
(`_bootstrap_collect()`/`_bootstrap_collect_iosxe()`/
`_bootstrap_collect_ios()`) before any other command. It is deliberately
*not* a pager state machine (no `--More--` detection/Space-key handling):
preventing the pager from ever activating is simpler and sufficient. Paging
is Discovery-only and session-local (a plain EXEC-mode command, never
entering configuration mode) -- managed `terminal_open()` never sends it,
and there is no global pager state; each device's paging-disable step is
fully independent, protected only by the existing per-device Discovery
bootstrap session lock (unchanged from the concurrency model above).

**Timeout semantics.** `terminal._wait_for_pattern()` -- the one shared
command-wait primitive used by every Discovery login/command and by
managed `terminal_open()`'s private authentication alike -- computes
`deadline = time.monotonic() + timeout` exactly once and loops `while
time.monotonic() < deadline`. This is a **fixed total deadline, not an
inactivity timeout**: new pane content arriving only ever unlocks whether a
match is considered at all (a stale-prompt-race guard against
`baseline_text`), it never recomputes or extends `deadline`. Proven with
deterministic tests (`tests/test_terminal_wait_timeout_semantics.py`, an
injectable fake monotonic clock/sleep -- no real 25-second waits) covering
a silent terminal, continuously-changing-but-never-matching output for the
whole window, and activity followed by silence: all three time out at the
same configured deadline, and a match that only becomes available *after*
the deadline is never seen, because the loop has already exited by then.
One honestly-acknowledged limitation: since this *is* a fixed total
deadline, a command whose valid output genuinely takes longer than the
configured timeout to fully arrive (independent of any pager) would still
time out — an inactivity-based command timeout would address this, and is
deliberately not implemented, to keep the pager fix narrowly scoped and
independently reviewable.

### Shared safe SSH password-prompt attribution

Both Discovery's bootstrap login and normal managed `terminal_open()`
need to answer the exact same kind of prompt safely: OpenSSH's own
client-side interactive password prompt, always exactly `"<user>@<host>'s
password: "` for whichever hop is currently authenticating (stable,
well-documented client text -- never the remote device's own banner/CLI).
This attribution logic lives once, in `terminal.resolve_target_password_
prompt(device_config, prompt_line)`, and is reused unchanged by both
callers -- never two independently-written password-prompt parsers:

- **Direct SSH** (no `jump_host_config`): unambiguous. The one password
  prompt that can ever appear is always the target device's own.
- **ProxyJump**: can show *two* separate password prompts in sequence
  (one per hop), and sending the wrong one to the wrong hop must never
  happen. The function reads the prompting hop's own address out of
  OpenSSH's prompt text and only answers when it confidently matches the
  target device's own address; a prompt that matches the jump host's
  address, or that cannot be confidently attributed to either hop, fails
  closed instead of guessing. This is a deliberately **bounded ProxyJump
  limitation**: jump-host authentication must be non-interactive (key/
  agent-based) for both Discovery and managed `terminal_open()` alike --
  neither one will ever answer a jump host's own password prompt.

**Telnet reuses this same function unchanged, with no telnet-specific code
at all.** Telnet has no client-side host-wrapping prompt text (unlike
OpenSSH's `"<user>@<host>'s password:"`), so on its own this function's
`SSH_HOP_PASSWORD_PROMPT_RE` attribution wouldn't apply -- but it doesn't
need to: the access-info schema's own jump-host validation requires
`transport: ssh` for any device that references a `jump_host`, so a telnet
device can structurally never have one. That means
`resolve_target_password_prompt()`'s very first check -- "no
`jump_host_config` -> unambiguous, answer with the target's own configured
password" -- already handles a telnet password prompt correctly, without
ever inspecting `prompt_line` at all in that case.

Discovery's `_resolve_login_password()` (in `discovery.py`) and managed
`terminal_open()`'s `_authenticate_managed_session()` (in `terminal.py`,
below) are each a thin, caller-specific wrapper around this one shared
function -- translating a rejected outcome into their own wording
(`DiscoveryError` vs. `TerminalError`), never re-implementing the
attribution rule itself.

### Managed-terminal private authentication

`terminal.open_device_terminal()` completes target authentication itself,
for either transport, immediately after the managed session is created or
reused, using the resolved access-info's own private credential —
the credential belongs to Network Lab MCP, not the AI, so the AI never has
to be asked for it.

`terminal._authenticate_managed_session()` runs after the managed session
is created or reused, still inside the concurrency model's per-device
lock (so a concurrent `terminal_open(R1)` from two callers can never send
its password twice, while a different device continues to authenticate
fully in parallel), and dispatches by the device's own configured
transport: `_authenticate_managed_ssh_session()` for `ssh`,
`_authenticate_managed_telnet_session()` for `telnet`, nothing for any
other/unknown transport. Splitting by transport -- rather than one
function trying to recognize both vocabularies -- keeps SSH's
OpenSSH-specific prompt/failure text and Telnet's classic-IOS-style
prompt/failure text from leaking into each other.

- **Transport-level (SSH), not device-CLI-specific.** SSH recognizes only
  OpenSSH's own password prompt and a small, stable set of OpenSSH's own
  authentication-failure messages (`Permission denied`, `Authentication
  failed`, `Connection refused`, ...) -- never an IOS XR (or any other
  vendor) CLI prompt. This keeps it safe for every device type
  `terminal_open()` supports, and preserves "Claude reads the pane" for
  anything this cannot resolve on its own (a host-key confirmation prompt,
  a device-CLI-level interaction).
- **Narrowly device-shaped (Telnet), by design.** Unlike SSH, Telnet has no
  client-side wrapping text to recognize generically -- login is a plain
  conversation with the device itself. This deliberately does not
  generalize into an interactive-login framework: it answers only an
  optional `Username:` prompt followed by `Password:` (the exact sequence
  Discovery's own `_login_ios_style()` already proved for classic-IOS-style
  lab devices), requires positively reaching the shared `IOS_STYLE_PROMPT_RE`
  exec-prompt shape to consider login complete (stricter than SSH's more
  lenient "anything else counts as progress" model), and recognizes a
  small, fixed set of classic-IOS-style login-failure text
  (`_TELNET_AUTH_FAILURE_RE`: "% Bad passwords", "% Login invalid",
  "Connection closed by foreign host"). Once the exec prompt is reached,
  authentication automation stops watching entirely -- never a persistent
  prompt-answering loop over the life of the session, and never
  enable/TACACS/OTP/MFA automation. `USERNAME_PROMPT_RE`/
  `IOS_STYLE_PROMPT_RE` live in `terminal.py` (Discovery's own names alias
  them) so managed Telnet auth and Discovery's own Telnet login share one
  definition, not two independently-maintained copies.
- **New vs. existing session.** A brand-new session's connection is still
  in flight, so this polls briefly (bounded) for a prompt/failure to first
  appear. An already-existing session's pane is already settled, so this
  takes exactly one immediate read-only capture -- if that does not show
  a password prompt right now (the ordinary case: already authenticated,
  or mid-command), nothing further happens at all: no send, no wait, no
  disturbance. This is what keeps an already-authenticated session fully
  idempotent, and also what lets a *pre-existing* session that happens to
  already be sitting at the password prompt (e.g. after an MCP server
  restart) get authenticated on the very next `terminal_open()`, with no
  need to manually destroy it first.
- **Sends the credential at most once.** If the same prompt reappears
  after one send (rejected), or an explicit failure message appears, or
  the wait after sending times out, authentication fails closed
  (`TerminalError`, always sanitized -- never includes the password or
  any other access-info content) -- never a retry loop.
- **Cleanup ownership.** If *this* `terminal_open()` call created a new
  session and authentication definitively fails, that now-unusable session
  is closed; a pre-existing session this call merely reused is never
  touched, even if authenticating it fails.
- **Credential boundary.** The password is read from the committed active
  access-info definition, passed through this one narrow call path, and
  sent only via `_send_secret_text()` — a dedicated tmux `load-buffer`
  (stdin) + `paste-buffer` primitive, never `tmux send-keys -l -- <secret>`,
  which would place the secret in that subprocess's own command-line
  arguments. The password never reaches any MCP tool result, any sanitized
  error, any log line (tmux's own pipe-pane transcript remains the only
  thing that might show a prompt or remote echo, exactly as it always
  could for any terminal content), or any process's argv. No
  module-global credential state is introduced; authentication context is
  local to each `terminal_open()` call.

## Topology discovery

```
committed active_access_info
    -> discovery._select_iosxr_targets() / _select_iosxe_targets() /
       _select_ios_targets() (type iosxr / iosxe / ios; host is skipped,
       not failed; nxos is skipped; unless zero targets of any kind remain)
    -> discovery._bootstrap_collect() (iosxr: login, _disable_terminal_
       paging(), `show version`, `show lldp neighbors`, `show cdp
       neighbors`, `show ipv4 interface brief` tolerantly) /
       _bootstrap_collect_iosxe() (iosxe: _login_ios_style(),
       _disable_terminal_paging(), `show version`, `show lldp
       neighbors`, `show cdp neighbors`, `show vrf` + `show ip interface
       brief` tolerantly) / _bootstrap_collect_ios() (ios: _login_ios_style(),
       _disable_terminal_paging(), `show version`, `show cdp neighbors`,
       `show vrf` + `show ip interface brief` tolerantly) -- paging
       disabled exactly once per session, immediately after login and
       before any other command; all logged persistently, see above
    -> discovery.parse_lldp_neighbors() / parse_cdp_neighbors()  (raw
       text -> NeighborObservation, tagged `source="lldp"`/`"cdp"`, never
       resolving identity itself)
    -> discovery.resolve_remote_identity()  (NeighborObservation.remote_
       device_id_raw -> logical device ID, or None -- fails closed on
       anything ambiguous; identical for either protocol)
    -> discovery.reconcile_links()  (resolved observations, mixed
       protocols -> ManagedLink list + LinkConflict list; the same link
       seen via both protocols dedupes to one, a same-interface
       cross-protocol disagreement is a conflict)
    -> discovery.parse_ipv4_interface_brief() / parse_ip_interface_brief()
       + parse_show_vrf()  (raw text -> {interface: (ipv4_or_None, vrf)};
       an L3ParseError skips just this device's enrichment, never the
       whole operation -- see "L3 interface enrichment" below)
    -> discovery.DiscoveryResult  (in-memory only)
    -> cli/config.py CliSession.apply_discovery_result()  (opens/creates
       the topology candidate via the *same* plan_topology_definition()/
       apply_topology_definition_plan() as a manually typed `topology
       <name>`, then replaces its candidate with
       discovery.build_discovery_candidate() -- a rebuild, never a merge)
```

`discover topology` (global configuration mode only): reads *committed*
`active_access_info` (never an uncommitted candidate selection), selects
its `type: iosxr`, `type: iosxe`, and `type: ios` devices (`type: host` is
skipped, not an error; `nxos` is unsupported and skipped; zero supported
targets of any kind is a hard failure), and requires **all** of them to
succeed for neighbor discovery -- any login/command/timeout failure fails
the whole operation before the prior candidate is touched. L3 enrichment
runs only for `discover topology l3` and then fails closed too (see below).
On success it prints a Discovery
summary and, if any neighbor could not be resolved to a managed device, an
explicit "Unresolved neighbors" section (raw Device ID, observing
device/interface, remote port, capability) — then enters topology
configuration mode with the result applied as the candidate, exactly like a
manually typed `topology <name>`. It never commits and never changes
`active_topology` itself.

`discovery.py` owns every Discovery-specific behavior (bootstrap
connectivity reuses `terminal.py`'s primitives, but the login sequence,
command runner, parser, identity resolution, and reconciliation are all
here) and is the *only* module dedicated to Discovery; `cli/config.py`'s
`apply_discovery_result()` is the only candidate-mutation logic for it,
and it is a thin adapter onto the pre-existing topology candidate machinery
-- there is no separate Discovery datastore, history table, or schema.

### IOS XR: LLDP/CDP disabled

On IOS XR, both `show lldp neighbors` and `show cdp neighbors` are always
collected, so one protocol being disabled never prevents using the other.
Exactly two responses are recognized as *disabled*, and only when they are
the entire meaningful output (blank lines, the CLI timestamp line and a
bare prompt/command-echo line are ignored) of their own command:

    % LLDP is not enabled
    % CDP is not enabled

A disabled protocol yields zero observations plus a fixed-text warning
(`Device '<id>': LLDP is disabled; ...`, logical device name and protocol
only -- never raw device output). Disabled is not negative link evidence:
it never creates a conflict, and the existing one-sided-observation policy
is unchanged (another device observing a disabled device still yields a
link). Disabled-like text that is not exactly the expected message for its
command (the other protocol's message, extra lines, mixed with a table) is
a `DiscoveryError`, as is any other unrecognized LLDP output (unchanged).
New wording from other IOS XR versions must not be added without a real
output sample and a test. If no link is discovered and a disabled warning
exists, an extra warning says the candidate has devices but no links; the
candidate is still generated by the existing rules (devices are always
included, links may be empty) and committing it is a separate, unchanged
step. Warnings are ordered by target order, then LLDP before CDP.

Known limitation: the IOS XR CDP parser remains lenient (shared with IOS XE
and IOS): unrecognized CDP output yields zero CDP observations rather than
an error. This was already the case and is not changed here. IOS XE/IOS
behavior is untouched. The feature is CLI-only; MCP tools do not run
Discovery, so no MCP contract changed. Not verified against a real lab.

### Identity resolution is bounded and fails closed

`resolve_remote_identity()` matches a remote Device ID (from either
protocol -- the function never looks at `NeighborObservation.source`)
against an in-memory `{logical_device_id: observed_hostname}` map built
from each bootstrap session's own login prompt (the IOS XR prompt for
`iosxr` targets, `_login_ios_style()`'s shared exec prompt for `iosxe`/
`ios` targets), in this order, each step requiring a *unique* match or the
neighbor stays unresolved:

1. exact observed-hostname match
2. case-normalized exact match
3. short-name/FQDN-style alias match (the raw ID's segment before its
   first `.`, compared case-insensitively against each hostname --
   e.g. `LAB_R2.example` matches hostname `LAB_R2`; this is also the path a
   CDP-reported FQDN like `LAB-SW1.example.com` resolves through, exactly
   the same as an LLDP one)

There is no substring search, no fuzzy matching, and no inference from
the logical device ID itself (never `"R2" in device_id`). A neighbor that
resolves to more than one logical device at any step is treated exactly
like one that resolves to none -- unresolved, never guessed.

### Managed vs. unresolved neighbors, and where their evidence lives

A resolved neighbor that is also one of *this run's* selected IOS XR/IOS
XE/IOS targets becomes a candidate topology device/link. Everything else --
external routers/switches visible only via LLDP/CDP but not in the
selected access-info, or genuinely ambiguous -- is an **unresolved
neighbor**: never invented as a managed topology device, but not silently
dropped either. Its full raw evidence (remote Device ID, observing
device/interface, remote port, capability) is:

- rendered directly in that `discover topology` run's own CLI output
  (mandatory, not reducible to just a count), and
- separately, persistently recoverable afterwards from the observing
  device's own terminal log (`show logging <device-id> <log-file>`,
  since the original `show lldp neighbors`/`show cdp neighbors` output is
  right there).

Deliberately, there is **no third persistence layer** for this evidence:
no `show discover`/discovery-history command, and no unresolved-neighbor
record written into the committed topology YAML. Re-running `discover
topology` produces a fresh normalized result the same way every time.

### Link reconciliation (multi-protocol)

`reconcile_links()` first groups *all* resolved observations (LLDP and
CDP mixed together) by their own `(local_device_id, local_interface)` key
(never by a "canonical pair" derived from a claimed remote port, since the
two sides of a conflict may claim *different* remote ports for the same
local interface -- keying by each side's own dict entry is what correctly
prevents double-processing in that case). Within one local endpoint's own
group (which may hold one LLDP observation, one CDP observation, or
both):

- if every observation for that endpoint agrees on the same (remote
  device, remote interface), they collapse into a single candidate for
  that endpoint -- this is what makes the same physical link, seen via
  both protocols or reciprocally from both ends, become exactly one
  `ManagedLink` rather than two;
- if they disagree -- whether one says LLDP and the other CDP, or both
  are the same protocol producing incompatible rows -- that endpoint is a
  `LinkConflict` on its own (`endpoint_a == endpoint_b`, both sides of the
  conflict being the differing observations of *this one* local
  interface), and it is excluded from further reconciliation; every other
  endpoint is unaffected.

Only endpoints that survived that first pass (i.e. have one agreed-upon
candidate) go through the existing reciprocal check:

- if the *remote* endpoint has no observation of its own, the link is
  one-sided but still created (bidirectional discovery is not an absolute
  requirement);
- if it does, and it reciprocally agrees (claims the same original local
  device/interface back), the pair collapses into one `ManagedLink`;
- if it does, but disagrees (a different interface mapping), a
  `LinkConflict` is recorded instead (`endpoint_a != endpoint_b`, the
  original reciprocal-mismatch shape) -- the link is never silently
  created from either side's guess.

A link's identity is its unordered pair of `(device, interface)`
endpoints, so two parallel links between the same router pair on
different interfaces are never deduplicated together.

### L3 interface enrichment

Additive topology context -- IPv4 address + VRF per interface, so Claude
can reason about questions like "check reachability from each router"
directly from `get_active_topology()` without first having to interact
with a device just to learn its addressing:

```
Discovery
   |
   +--> neighbor evidence (LLDP/CDP)
   |        |
   |        v
   |     links (connectivity)
   |
   +--> L3 interface evidence (show ipv4 interface brief /
   |     show ip interface brief + show vrf)
   |        |
   |        v
   |     interface attributes (ipv4_address, vrf)
   |
   v
topology candidate
```

Both enter the same topology candidate, but they answer different
questions: links are connectivity (who is physically/logically attached
to whom); IPv4/VRF are L3 context on an interface, independent of
whether that interface happens to also be a link endpoint. Neither is
ever derived from the other -- an IP address is never used to infer a
link, and a link is never used to fabricate an address.

Only a directly observed IPv4 address + VRF are stored -- deliberately
never a prefix length (no `/24`/`/30` inference from the address value,
no subnet calculation), and never operational state (`Up`/`Down`,
holdtime, counters): those interface-brief commands' Status/Protocol
columns are read only far enough to skip past them (see
`parse_ip_interface_brief()`'s own docstring on why a fixed token count
would be brittle against IOS's two-word `administratively down` status),
never persisted.

**Interface-name canonicalization** (`_canonicalize_interface_name()`):
classic IOS/IOS XE's `show vrf` and `show ip interface brief` can report
the *same* interface in different abbreviated forms (e.g. `show vrf`'s
Interfaces column showing `Gi0/0` while `show ip interface brief` shows
`GigabitEthernet0/0`) -- a small, fixed lookup table expands known
abbreviations to one canonical full name so the two commands' output can be
matched up; an unrecognized prefix is left completely unchanged rather than
guessed. This is a small, single-purpose SSOT, reused for both
`show vrf`/`show ip interface brief` reconciliation and as the canonical
key written into a device's `interfaces` mapping.

**Management-address exclusion**: access-info is *how* MCP reaches a
device; topology is *what the network looks like* -- an observed
interface IPv4 address that exactly equals the selected access-info's own
connection address for that same device is deliberately never copied into
topology L3 data (`_build_device_interface_fields()`), regardless of
whether that interface also happens to be a link endpoint (the link
itself, if any, is completely unaffected by this exclusion).

**Fail-closed distinction, per device, per L3 source**: IOS XR's single
`show ipv4 interface brief` either parses (its own header recognized) or
the device's L3 enrichment is skipped entirely; IOS XE/IOS require *both*
`show vrf` and `show ip interface brief` to parse -- one failing skips L3
enrichment for that whole device rather than guessing every unlisted
interface is `default` VRF. Either way this is captured as an
`L3ParseError`, caught only in `discover_topology()`'s own per-device L3
loop -- it never escalates to a `DiscoveryError` (which would fail the
*whole* run) and never touches that device's already-collected LLDP/CDP
links. For `discover topology l3` an `L3ParseError` (or a transport failure
on an L3 command) is converted to a `DiscoveryError` ("L3 interface
collection failed") and fails the whole run; a recognized table with zero
rows is a valid success. In the default L2 run none of this executes.

**L3 is rebuilt, never merged**: `fields["interfaces"]` is present only for
an `l3` run. `build_discovery_candidate()` writes exactly the observed
interfaces (an interface observed as `unassigned`, or excluded as a
management address, is represented by a `None` sentinel in the
`DiscoveryResult` and is simply absent from the candidate). An L2 run writes
no `interfaces` at all: previous L3 data is never carried over (the CLI always
prints `L3 enrichment: Disabled` / `Previous L3 data: Not carried over`).

### Topology candidate rebuild (no link merge)

`build_discovery_candidate(result, base)` builds the candidate from the
`DiscoveryResult` alone. Only what Discovery does not own is taken from
`base` (the open candidate, else the committed definition, else a new empty
one): `name`, `description`, unknown top-level keys and unrelated extra fields
of a still-managed device. Devices (every managed device of the active
access-info, links or not) and links are never carried over -- a device not in
the access-info, and any earlier link, disappear unless observed again. Output is
deterministic: devices sorted by ID, link endpoints in canonical
`(device, interface)` order, links sorted. `CliSession.discovery_removals()`
lists the committed devices/links that will be dropped; the CLI prints them
before `apply_discovery_result()` builds and validates the new candidate
(`lab.validate_topology_data`) and only then installs it in one assignment --
any earlier failure leaves candidate, mode and tracking untouched. Nothing is
persisted before `commit`; `clear` restores the committed definition.

**Strict collection rules** (`discover_topology()`): per device, LLDP and CDP
are each success (an empty recognized table counts), disabled (IOS XR: exact
`% LLDP/CDP is not enabled`; IOS XE LLDP: `LLDP is not enabled`; IOS/IOS XE
CDP: the same exact text as IOS XR), or unavailable (classic IOS has no LLDP).
CDP is additionally "unsupported" (a non-fatal capability state, no warning)
in exactly one measured case: a `type: iosxr` device whose `show version`
banner is an IOS XR **LNT** image (`Cisco IOS XR Software, Version <v> LNT`)
and whose `show cdp neighbors` output is only `% Invalid input detected at
'^' marker.` (the NCS540L has no CDP command). `% Invalid input` is never
treated as unsupported for any other OS, image or command (LLDP included).
A CDP table whose rows all fail to parse is a parser failure.
Disabled/unsupported protocols beside a working one are reported as a
`Neighbor discovery protocol status:` block (`CDP disabled: 2 devices (R1, R2)`,
`CDP unsupported: ...`), not as warnings; the "Neighbor discovery warnings"
section is reserved for real warnings, and a run without any ends with
`Topology discovery completed successfully.`
Command errors, timeouts and unrecognized output are fatal; at least one
protocol must be a success, otherwise Discovery fails ("no usable neighbor
discovery protocol"); one disabled protocol beside a success is a warning.
After reconciliation, any conflict, any managed interface in more than one
link, or a managed link endpoint also seen facing an unmanaged neighbor is
fatal. Failures stay fail-fast (first device in target order); there is no
all-failures listing.

### Dirty-candidate protection

`CliSession` keeps `_mutation_rev`, incremented by every manual candidate
mutation that actually changes something (device/jump-host field set/clear,
device add/remove, description, external edit, definition delete), and
`_discovery_baseline_rev`, set to it after Discovery rebuilds the candidate.
`check_discovery_allowed()` refuses to run when the target candidate is dirty
and the two differ -- even if the edit was reverted by hand -- and otherwise
defers to `can_switch_definition()` (a different dirty definition blocks it).
Re-running Discovery over a Discovery-only candidate is allowed. `clear`,
`commit`, a fresh load and leaving configure mode reset both counters. The
counters never affect `definition_dirty()`, which stays a pure value
comparison (net-zero semantics unchanged). The candidate lives only in the
CLI process, so the tracking state does too; the REPL is single-threaded and
Discovery's worker threads never touch the candidate, so no compare-and-swap
is needed.

Topology's own schema validation (`lab.validate_topology_interfaces()`)
keeps this bounded: only `ipv4_address` + `vrf`, both required non-empty
strings when an interface entry exists at all, `ipv4_address` checked as
a real IPv4 address (`ipaddress.IPv4Address`) -- no other field is
accepted. Existing topology files with no `interfaces` key anywhere remain
valid with zero migration. Because `lab.get_active_topology()` already
returns the whole validated topology mapping verbatim (it never filters
fields -- topology structurally cannot carry access-info's private fields
at all, see `validate_topology_no_access_fields()`), committed L3 data
appears through the exact same MCP-facing read Claude already uses with
zero changes to `lab.py`'s `get_active_topology()` or `mcp_server.py`. The
CLI's `render_topology_block()`/`render_topology_configuration_delta()`
(in `cli/main.py`) render this same `interfaces` data for `show
running-config topology`/`show configuration`/candidate review, following
the same read-only-review precedent already used for `links` -- there is no
structured CLI command to edit an interface's L3 fields directly (they are
Discovery/external-editor-populated data, reviewed but not hand-typed).

### Parallel per-device collection

`_bootstrap_collect()` for each device runs on a bounded
`concurrent.futures.ThreadPoolExecutor` (`DISCOVERY_MAX_WORKERS = 8`)
instead of a plain sequential loop:

```
R1 collect -----|
R2 collect -----|
R3 collect -----| concurrent, bounded by DISCOVERY_MAX_WORKERS
R4 collect -----|
                |
                v
     every future finished (success or failure)
                |
                v
     close every target's bootstrap session (unconditional cleanup,
     one outer step closes all of them, exactly as a sequential loop's
     own try/finally would)
                |
                v
     aggregate results in original target order, never completion
     order -- parsing/identity/reconciliation are unchanged and still
     run only after every device's result (or first failure) is known
```

Parallelism is device-level only: within one device, `_bootstrap_collect()`
/`_bootstrap_collect_iosxe()` themselves are strictly sequential (login,
then each command in order, in that device's own worker); IOS XR and IOS
XE targets are submitted to the *same* thread pool, just dispatched to
their own collector function. Workers are value-oriented -- each returns its
own collected dict; nothing here mutates a shared candidate, link list, or
conflict list from more than one thread. Aggregation and error attribution
are always by original target order, not by whichever thread happened to
finish first, so scheduler order can never change which device's result
lands where or which device's failure is the one reported.

## CLI control plane

The human configuration/control plane is an IOS XR-compatible CLI
(`./run_cli.sh`, `network_lab_mcp.cli`) that a human operator uses to create
and edit lab definitions and the running-config selection. It is a separate
process from the MCP server and never speaks the MCP stdio protocol.

```
Human Operator
    |
IOS XR-compatible CLI (./run_cli.sh)
    |
Candidate configuration (memory-only):
  - running-config candidate (settings_candidate)
  - at most one open definition candidate
    (topology | access-info | scenario | reference)
    |
commit
    |
Committed lab YAML:
  lab/settings.yaml, lab/topologies/*.yaml,
  lab/access-info/*.yaml, lab/scenarios/*.yaml,
  lab/references/*.yaml
    |
Network Lab MCP  ->  Claude Code
```

The CLI is a configuration/control plane, not a network-engineering
reasoning engine: it creates/edits *definitions* and the *running-config
selection*, never operates on network devices itself, and never decides
what Claude Code should do with a topology once committed. See
[cli_reference.md](cli_reference.md) for the full command reference.

### Commit validation scope

`commit` separates *what is being saved* from *the health of the rest of
the environment*, in two independent stages:

- **Stage A -- changed-object strict validation.** The definition being
  created/edited always goes through its formal validator (schema, device
  types, jump-host references, ...). A defect in the changed definition is
  never excused because it already existed. A pending deletion is not
  content-validated; its effect is checked in stage B.
- **Stage B -- baseline/candidate comparison.** Running-config selection
  findings ("selected X does not exist") are computed for the committed
  baseline (settings on disk vs. definitions on disk) and for the effective
  candidate (candidate settings vs. definitions after this commit applies,
  including a pending deletion or a definition created in the same commit).
  A finding's identity is `(source kind, source name, source field, target
  kind, target name)` with an occurrence count; list position is not part
  of it. A finding that is **new or whose count grew** fails the commit; an
  **unchanged** one is a warning; a **resolved** one simply disappears.

A warning never blocks the save, never rewrites unrelated files, and never
repairs the existing defect. The existing fail-closed invariant that
running-config must select an active topology and scenario is unchanged.
Dependencies are limited to the current data model: running-config
selections (and a definition's own internal references, covered by stage A).

```mermaid
flowchart TD
    A[commit requested] --> B{candidate dirty?}
    B -- no --> N[No changes to commit.]
    B -- yes --> C[Stage A: strictly validate changed definition]
    C --> D[Stage B: compare baseline vs effective candidate findings]
    D --> E{stage A error, or new / worsened finding?}
    E -- yes --> F[FAILED: nothing written, candidate kept]
    E -- no --> G[Write definition, then settings if changed]
    G -- I/O error --> F2[Commit failed message: candidate kept]
    G --> H{persistent findings?}
    H -- no --> S[SUCCESS]
    H -- yes --> W[SUCCESS_WITH_WARNING]
```

**Persistence -- guaranteed.** Each file is written with temp file +
`os.replace` (atomic per file); validation failure writes nothing and keeps
the candidate; an I/O failure during `commit` prints `% Commit failed: could
not write configuration (<ErrorClass>)` (no raw exception text), never
`Commit complete.`, and keeps the session and candidate.

**Persistence -- observed (fault-injection tests).** For a *deletion*
(`no <kind> <name>` committed together with the matching running-config
deselection/switch) `settings.yaml` is written first and the definition file
deleted second, after an up-front deletability check: a failure in between
leaves an unreferenced file (retry finishes the delete), never a persisted
selection of a deleted definition (`tests/test_reference_cli_consistency.py`).
For a create/edit, the definition file is written before `settings.yaml`. If the definition write fails, nothing
changed. If `settings.yaml` then fails (function-level or `os.replace`
failure), the definition stays saved, `settings.yaml` keeps its old
content (the new definition is simply not yet selected, so no new broken
reference is created), and the candidate keeps only the pending selection.
Retrying `commit` rewrites only `settings.yaml` (the saved definition is
not rewritten) and reports `settings_written` without claiming the
definition was committed again. A failed `os.replace` may leave a stray
`<name>.yaml.tmp` that is not read as a definition and is replaced on the
next successful write of the same file.

**Known limitations.** The two writes are not one transaction (the
intermediate state above is consistent but not rolled back). No
external-modification conflict detection exists (no hash, mtime, or lock).

### Two independent candidate scopes

- **Running-config candidate** (`settings_candidate`): a snapshot of
  `lab/settings.yaml`, mutated only from `running` mode
  (`topology`/`scenario`/`reference`/`no reference`). A settings-only change
  never blocks opening or switching a definition, and vice versa.
- **Definition candidate** (`definition_kind` / `definition_name` /
  `definition_original` / `definition_candidate`): at most one of
  `topology`, `access_info`, `scenario`, or `reference` at a time. Opening a
  *different* definition while the current one is dirty is blocked
  (`can_switch_definition()`), the same caution the topology-switch guard
  originally applied, generalized to all four kinds.

`commit()` validates both scopes, writes any dirty definition file(s)
first, then `lab/settings.yaml` last — a running-config selection may name a
definition that was only just created or edited in the very same commit,
and that definition must already exist on disk by the time the selection
referencing it is written.

A nested submode (a topology/access-info device, or an access-info jump
host) is only a context pointer (`current_device_name` /
`current_jump_host_name`) into that one owning definition candidate — it
never becomes, or is backed by, a second independent candidate. Entering
`device R4` from an existing access-info definition adds `R4` to the
*same* `definition_candidate["devices"]` dict that already holds every
other committed device; `commit` issued from inside `R4`'s own submode
persists that whole dict, siblings included, never a scoped fragment.
`show`/`show configuration` inside a submode *is* scoped to just that one
object (see cli_reference.md), but that is a rendering choice only — it
must never be read as "this is the only object in the candidate," and it
is never commit's source of truth (commit reads `definition_candidate`
directly, never a renderer's output).

### Symmetric definition deletion

`no <kind> <name>` (global configuration only; `<kind>` is
`access-info`/`topology`/`scenario`/`reference`) extends the single
`definition_candidate` slot above with a prospective-absence
representation, rather than adding a second, independent
"deletion set" data structure: `definition_kind`/`definition_name`/
`definition_original` stay set to the real committed definition being
removed, while `definition_candidate` itself becomes `None`
(`CliSession.remove_definition(kind, name)`, behind one method shared by
all four kinds — `remove_topology_definition()` is now a one-line wrapper
kept for its existing callers/tests). `definition_dirty()` treats this as
always dirty (a real committed original always differs from eventual
absence). All four kinds already shared this one candidate slot, so this
was already a stronger invariant than "one topology candidate" -- at most
one dirty *definition of any kind* per configure session -- and deletion of
any kind participates in exactly that same existing rule with no widening.

This minimal representation change means every existing mechanism
already does the right thing with no further changes:

- `_enter_definition()`'s existing reload guard only skips reloading when
  `definition_candidate is not None` -- so re-entering the *same*
  topology (`topology <name>`) while it is pending deletion naturally
  fails that guard and reloads fresh from disk, restoring the original
  candidate and cancelling the deletion, for free.
- `clear()`'s existing "restore from `definition_original`" branch
  (`self.definition_candidate = copy.deepcopy(self.definition_original)`)
  cancels a pending deletion the same way, with no special-casing.
- `can_switch_definition()` needed no changes at all: since deletion
  makes `definition_dirty()` True, attempting to edit or delete a
  *different* definition of any kind while a deletion is pending is
  already rejected by the pre-existing guard, and the existing "same
  identity is always allowed" check
  (`(self.definition_kind, self.definition_name) == (kind, name)`)
  already permits every same-definition transition (edit -> delete,
  delete -> restore, delete -> restore -> edit) for every kind.
- Deleting a definition that was only ever a brand-new, never-committed
  candidate (`definition_original is None`) is instead a net-zero
  cancellation: the whole candidate slot is discarded outright (the same
  case `clear()` already handles for a never-committed definition),
  since there is nothing real to mark absent.

`_render_configuration_delta()` (cli/main.py) renders a `None` candidate
alongside a non-`None` `original` as a single `no <kind> <name>` line
(never the per-field diff the other renderers produce -- for access-info,
deliberately never re-rendering that definition's own stored credentials),
and `commit()` (cli/config.py) has a parallel deletion path per kind: skip
the normal per-kind validator (there is nothing to validate), reject the
commit if the definition being deleted is still *effectively* (candidate,
not already-committed) referenced by running-config -- `active_access_info`
(optional), `active_topology` (mandatory), `active_scenario` (mandatory),
or present in `active_references` (a list) -- so a combined commit that
also switches the active selection to something else in the same
transaction is correctly allowed, generalized behind one small
`_existence_error()` closure rather than four hand-written checks.
Deletion itself dispatches through `_DEFINITION_DELETERS[kind]`
(`lab.delete_topology` / `delete_access_info` / `delete_scenario` /
`delete_reference`), each requiring an exact match against that kind's own
`list_*_names()` and a non-symlink, path-confined regular file
(`lab._stored_definition_is_deletable()`, one small shared primitive
behind four still-distinct named functions -- not a generic definition
framework), mirroring the discipline already used for terminal log
deletion (`terminal.py`). No cascade: no other definition, running-config
field, terminal session, or Discovery state is ever touched by a
definition deletion.

There is no real cross-definition reference to an access-info, scenario,
or reference definition by name anywhere else in the schema (scenario/
reference are open-ended YAML documents with no fixed cross-reference
fields; access-info is keyed by device name, never by another
definition's name) -- the only real references anywhere are
running-config's own four selection fields, handled above. `config-running`
mode has no `no topology` command at all (only `no access-info` and `no
reference <name>`), since `active_topology` (like `active_scenario`) is a
mandatory field with no safe "unset" value.

### Command grammar as the single source of truth

`cli/grammar.py` builds one trie per CLI mode (EXEC, global, running-config,
topology + its nested device submode, access-info + its nested device
submode, scenario, reference) out of fixed keywords and argument slots.
Parsing, unique fixed-keyword abbreviation, Tab/Ctrl-I completion,
context-sensitive `?` help (including the `<cr>` marker and "select or
create" identifier help — existing candidates plus a creation hint),
dynamic candidate lookup, and invalid/incomplete/ambiguous command reporting
all walk that same trie — there is no separate parser table, completion
table, or help table to drift out of sync.

Dynamic completion (topology/scenario/reference/access-info/device names,
and the `ssh`/`telnet` transport and `device.type` enums) is supplied by
small, pure provider functions of the shape `provider(context, prefix) ->
candidates`. `context` is a read-only `grammar.CliContext` snapshot that
`cli/main.py` builds fresh from the live `cli/config.CliSession` before
every parse/completion/help call. `cli/grammar.py` never imports
`cli/config.py` and owns no mutable session state itself, so there is no
grammar/config circular dependency, and completion/help can never mutate
the candidate. It does import `network_lab_mcp.lab` for the `device.type`
enum SSOT (`DEVICE_TYPES` / `normalize_device_type()`), a one-directional
dependency that introduces no cycle.

### Fixed-keyword vs. object-identifier case semantics

Fixed CLI keywords (`configure`, `topology`, `transport`, ...) are matched
case-insensitively, mirroring IOS XR. Object identifiers — topology,
scenario, reference, access-info, and device names — are matched
case-sensitively and are never abbreviated or case-folded, because they are
user-provided/stored names, not grammar keywords. Dynamic completion for
identifiers preserves and matches on exact stored case (`R1`/`R2` completing
`device R`, but not `device r`).

### Case-only topology-name collision safeguard

`topology <name>` (in global configuration mode) can both select an
existing topology definition and implicitly create a new one, so a
differently-cased near-duplicate (`SRv6_Lab` vs. an existing `srv6_lab`) is
a real hazard: silently opening the existing one would ignore the
operator's exact input, and silently creating a new one would produce a
confusing, hard-to-notice second topology. `cli/config.find_case_only_collision()`
is a narrow check — case-only equality, nothing fuzzier — that, when
triggered, requires explicit `yes`/`no` confirmation before a distinct
topology candidate is created. Declining is a true no-op; confirming
preserves the exact entered case. This is a CLI-side creation-safety check,
not a replacement for the schema validator below.

### Validator reuse

`cli/config.py` does not maintain its own topology/access-info/device-type
validation rules. `network_lab_mcp.lab.validate_topology_data()` (built on
`validate_topology_device_names()`, the topology-no-access-fields check, and
the shared `device.type` enum) and `lab.validate_access_info_data()` are the
single validation primitives used by both the MCP/CLI load paths and the
CLI commit path alike.

### Minimal, targeted persistence

`commit()` validates the whole candidate first (zero disk writes on
failure), then writes only the YAML files whose *scope* was actually dirty,
definition file(s) before `lab/settings.yaml`: an unchanged definition that
was merely opened is never rewritten, a running-config-only change never
touches definition YAML, and a fully clean commit writes nothing. Writes are
atomic (write to a sibling `.tmp` file, flush, `os.replace()`).

`commit()` itself never changes `cli/config.CliSession.mode` — `cli/main.py`
never sets it after a `_do_commit()` call, success or no-op. Navigating
between modes is `root` (jump straight to global configuration mode from
any nested submode, preserving candidate state, driven by
`CliSession.go_to_global()`), `exit` (exactly one level up, via the
`_EXIT_PARENT_MODE` table below), and `end` (a guarded jump to EXEC,
`overall_dirty`-gated) — none of which ever commits or clears.
`cli/config._EXIT_PARENT_MODE` is the single source of truth for "exit"'s
per-mode destination; `cli/main.py`'s generic `h_exit()` handler reads it
instead of hard-coding one function per mode.

### External YAML editor (topology / scenario / reference)

`cli/editor.py` resolves an external editor ($VISUAL, then $EDITOR, then a
`vim` fallback with just enough options to make an unfamiliar YAML file
legible — those decorative options are never added when the operator chose
their own editor). Separately, whichever editor is resolved gets Vim's
paste mode (`-c "set paste"`) appended if, and only if, its executable is
positively confirmed to be real Vim (never guessed from a `vim`/`vi`/`nvim`
name alone, never added to Neovim, and coexisting with any `-c` option the
operator already supplied) — see `editor._is_confirmed_vim()`'s own
docstring for the exact, subprocess-free detection rule. It then opens the
current definition candidate in a secure, unique `.yaml` temporary file
(never the committed file itself), and on a clean editor exit parses and
minimally validates the result (valid YAML, root is a mapping) before
installing it as the new candidate through the same
`replace_definition_candidate()` -> per-kind SSOT validator path `commit()`
uses. A non-zero editor exit, invalid YAML, or a non-mapping root leaves the
candidate untouched and reports a safe error (no raw editor content, no
credentials). The temporary file is always removed, on every path including
exceptions. access-info does not offer `edit` — its private fields stay on
the structured CLI path (see README.md's "Password display policy" for how
those fields are rendered).

## Security boundaries

### Committed-only MCP boundary

The MCP server never reads candidate configuration. `get_active_topology()`
and `get_execution_instructions()` only ever read `lab/settings.yaml` and
whatever it currently references on disk — exactly the same files a
successful `commit()` writes to. `terminal_open()` additionally reads
`lab/access-info/*.yaml`, but only ever the committed files, never a
candidate. There is no shared in-memory state, cache, temp file, or extra
MCP tool bridging the CLI process and the MCP server process; the boundary
is the committed YAML on disk, and the reload-on-every-call policy above
means a successful commit is visible to Claude Code on the very next tool
call, with no MCP server restart.

### Credential boundary summary

The full detail lives in README.md's ["Security model"](../README.md#security-model)
and in "Device access resolution"/"Managed-terminal private authentication"
above; in one place, the properties that hold everywhere in this project:

- access-info (credentials) is never returned by any MCP tool.
- A credential is never placed in any process's command-line arguments —
  `_send_secret_text()` uses a tmux buffer over stdin, never `send-keys -l`.
- Network Lab MCP does not intentionally place a credential in its own tool
  results, exceptions, `%`-prefixed error messages, or application-side log
  lines, with one deliberate, narrow exception: the human CLI's own
  explicit local `show`/`show configuration`/`show running-config` display
  of an access-info device/jump host shows `password` in clear text (this
  is a lab tool, not a secret manager) — never reachable through MCP, Tab/`?`
  completion, or command history.
- Discovery's private bootstrap sessions and managed `terminal_open()`
  sessions share one credential-sending primitive and one password-prompt
  attribution function (see above), rather than two independently written
  paths that could drift apart.
- None of this sanitizes terminal content. The persistent terminal logs are
  raw transcripts of the remote session: Network Lab MCP does not redact device
  output, and anything the remote side prints or echoes can appear there. Terminal
  output and logs are potentially sensitive and are not guaranteed to be
  free of secrets (see [SECURITY.md](../SECURITY.md)).

### Managed sessions vs. the active topology

See [MCP interface](#managed-sessions-vs-the-active-topology) above — this
is the one place `terminal_send()`/`terminal_read()` enforce a boundary
that `terminal_list()`/`terminal_close()` deliberately do not.

## Persistence and lifecycle

### Installation model and lab root ownership

Network Lab MCP supports exactly one installation model: a local repository
checkout installed with `pip install -e .`. The repository checkout **owns**
the `lab/` directory. `network_lab_mcp.lab.find_lab_root()` resolves this
directory relative to the installed package's own source location (its
`__file__`), never relative to the current working directory of the process
that launched the MCP server — Claude Code is normally started from an
unrelated task workspace, so depending on its working directory would be
incorrect.

Non-editable or wheel installation (`pip install .`, a built wheel, or a
package-index install) is **not a supported configuration**: such an
install has no `lab/` directory to find, since lab data is repository-local
operational data rather than a packaged resource. Supporting that would
require packaging work (e.g. `importlib.resources`, an external writable
config directory, or an environment-variable-based lab-root override) that
is out of scope for this project's current design.

### tmux persistence

Terminal sessions live in tmux, independent of the MCP server process. The
server does not create a session registry of its own, and it never destroys
a tmux session on exit. A restarted MCP server rediscovers and reuses any
existing managed session instead of creating a duplicate — including one
already sitting at a login prompt from before the restart, which the next
`terminal_open()` may safely resume and complete authentication for.

### Test isolation

The test suite never touches the real, production `network-lab-mcp` tmux
socket: a session-scoped autouse fixture redirects `terminal.TMUX_SOCKET_NAME`
to a per-test-run socket name for the duration of the suite, and tears that
socket's server down afterward. Tests that must exercise the real production
socket name directly (proving it is left untouched) do so explicitly via a
dedicated fixture, never by accident. This means an engineer's own real,
already-open managed sessions are never listed, read, or closed by an
ordinary `pytest` run.

## Current scope and deliberate exclusions

To keep the MCP layer thin and the scope tight, this repository
deliberately excludes:

- NX-OS discovery, SNMP/NETCONF/RESTCONF discovery, and a generic
  discovery/plugin framework — Discovery implements only IOS XR/IOS XE
  (LLDP + CDP) and classic IOS (CDP only).
- `discover topology` automatically
  committing or selecting `active_topology`, and a discovery-history
  subsystem (`show discover`/a discovery database).
- An HTTP MCP server, containerization, or an MCP-owned runtime/session
  database.
- A public local-shell transport (local processes are used only inside the
  internal validation helpers described above).
- External-editor support for access-info (structured CLI editing only).
- Multi-hop SSH jump chains: a jump host cannot itself reference another
  jump host; single-hop OpenSSH ProxyJump only.
- A shell-hop automation/state machine for ProxyJump: OpenSSH's own `-J`
  option is used directly, so there is nothing to automate.
- A generic cross-file consistency framework: the topology/access-info
  `device.type` check is a narrow, single-purpose comparison at resolution
  time, not a general validation framework.
- A persistent candidate journal or crash-recovery database: candidate
  configuration is memory-only and intentionally does not survive an abrupt
  CLI process termination (SIGKILL, crash, terminal destruction). Normal
  exit paths (`root`, `exit`, `end`, `clear`, Ctrl-D) never lose a candidate
  unexpectedly; that guarantee does not extend to a killed process.

These are deliberate boundaries, not unfinished work — each one keeps a
specific part of the system narrow and easy to reason about (see the goals
above). None of them is currently on a roadmap; if one is ever revisited,
it will be because a concrete need justifies the added complexity, not
because it was merely deferred.


### Name-less running selection submodes (access-info, topology, scenario, reference)

`config-running-<kind>` (internal modes `running_<kind>_common`, listed in `cfgmod.RUNNING_COMMON_MODES`, built by `cli/grammar.py::_build_running_common_root(kind)` and wired by `main.py::_register_running_common_handlers(kind)`) are selection-only submodes entered by the bare keyword under `config-running`. Selection calls `select_access_info` / `select_topology` / `set_scenario` / `select_reference_idempotent` and unselection `clear_access_info_selection` / `clear_topology_selection` / `clear_scenario_selection` / `remove_reference` -- the same `settings_candidate` mutations as the parent/named commands; a topology/scenario candidate may be unselected but `commit()`'s existing "missing a valid active topology/scenario" validation rejects it. The text below describes the access-info case; the others are identical apart from the selection method. It reuses `CliSession.select_access_info()` (the same Selection `access-info <name>` performs before entering its named submode) and the existing `settings_candidate` / dirty tracking; no new state, and it is intentionally not in `RUNNING_ENTRY_MODES`, so no description handler can reach it (`description` is not in its grammar; its `no <name>` is a selection removal handled by `h_running_access_info_common_no`, which exact-matches the candidate selection and then calls the same `h_running_access_info_remove` as `config-running# no access-info`; Help/Tab names come from `CliContext.candidate_access_info_names`, rebuilt from the candidate on every call). Fixed keywords take precedence over definition names (`_match_literal`: case-insensitive, unique-prefix); same-named definitions stay selectable through `access-info <name>`. In pasted blocks, `!` exits `running` / this submode only after a command that ran inside the section (`execute_input_block`). Provisional: only access-info has it.
