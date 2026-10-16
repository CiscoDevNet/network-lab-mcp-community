# CLI Reference

This is the reference for the human-facing Network Lab CLI, launched with
`./run_cli.sh`. It is a human configuration/control plane for
running-config (`lab/settings.yaml`) and topology/access-info/scenario/
reference *definitions*, edited through a candidate/commit model with IOS
XR-compatible interaction. It does not speak the MCP stdio protocol and is a
separate process from `network-lab-mcp` — see
[architecture.md](architecture.md#cli-control-plane).

The command grammar (`src/network_lab_mcp/cli/grammar.py`) is the single
source of truth for everything in this document: parsing, abbreviation,
completion, `?` help, and error reporting all walk the same trie.

## Configuration model in one line

- **running-config** (`running` mode): *which* topology/scenario/references
  MCP currently uses — a selection.
- **topology** / **access-info** / **scenario** / **reference** (global
  mode's `topology`/`access-info`/`scenario`/`reference <name>`): create or
  edit a *definition*. Never changes the running-config selection.

## Modes and prompts

| Mode | Prompt |
|------|--------|
| EXEC | `network-lab#` |
| Global configuration | `network-lab(config)#` |
| Running-config selection | `network-lab(config-running)#` |
| Topology definition | `network-lab(config-topology-<name>)#` |
| Topology device (safe fields) | `network-lab(config-device-<name>)#` |
| Access-info definition | `network-lab(config-access-info-<name>)#` |
| Access-info device (private fields) | `network-lab(config-access-device-<name>)#` |
| Access-info jump host (single-hop ProxyJump endpoint) | `network-lab(config-access-jump-host-<name>)#` |
| Scenario definition | `network-lab(config-scenario-<name>)#` |
| Reference definition | `network-lab(config-reference-<name>)#` |

`<name>` is the exact, case-preserved identifier currently open for editing.

## Navigation: `commit` / `root` / `exit` / `end` / `clear`

| Command | Meaning |
|---------|---------|
| `commit` | Save the candidate. **Stays in the current mode** — never leaves it, whether the commit succeeded or was a no-op. |
| `root` | Jump straight to global configuration mode from any nested submode. Candidate state is preserved; never commits, never clears. Not offered at global configuration mode itself (already the configuration root) or in EXEC. |
| `exit` | Move exactly **one** configuration level up (device/jump-host -> its parent definition; a definition or `running` -> global). No dirty guard — still the same overall configure session. At global configuration mode, `exit` is guarded (see `end`). |
| `end` | Return to EXEC from anywhere in configuration mode. Blocked (with a warning) while any candidate scope is dirty. Never an implicit commit or clear. |
| `clear` | Discard the entire uncommitted configure-session state (running-config candidate and any open definition candidate). Stays in the current mode, or the nearest still-valid parent if the current submode's target no longer exists. Never writes disk, never returns to EXEC. |

### `commit` results

`commit` strictly validates the definition you changed, and rejects any
*new or worsened* running-config reference problem (for example selecting
a definition that does not exist, or deleting one that is active). A
reference problem that already existed and is unrelated to your change is
only a warning; the commit still succeeds and nothing else is modified or
repaired:

```
network-lab(config-access-info-sr-mpls)# commit
Commit complete.

% Warning: Running-config references missing definition:
  reference 'sample'

  The access-info 'sr-mpls' was committed successfully.
  The existing running-config was not modified.
```

On failure (one `% <reason>` line per error) nothing is written and the
candidate is kept. If writing to disk fails, `% Commit failed: could not write configuration (<ErrorClass>)` is printed instead of `Commit complete.`, the candidate is kept, and `commit` can be retried; for a create/edit the definition file is written before `settings.yaml`, so a failure between the two can leave the definition saved but not yet selected; for a *deletion* `settings.yaml` is written first and the file deleted second, so a failure between the two can leave an unreferenced file but never a persisted selection of a deleted definition (see [architecture.md](architecture.md#commit-validation-scope)). A no-op commit prints `No changes to commit.`. The "not
modified" line appears only when `settings.yaml` was really not rewritten.

None of `root`/`exit`/`end`/`clear` ever commits, and `commit` never
navigates — the two concerns are fully independent.

## `?` vs. `help`: syntax help vs. Quick Start/usage help

These two are deliberately different, and neither one replaces the other:

- **`?`** — IOS XR-style **context-sensitive command syntax help**: "what
  can I type here?" Unaffected by anything in this section; see
  "Context-sensitive `?`" below.
- **`help`** — Network Lab MCP's own **Quick Start/usage help**: "how do I
  use Network Lab MCP?" Bare `help` shows an overview (purpose, the typical
  workflow, the five configuration areas, editor resolution order, and a
  pointer to `?` and to the topics below); it does **not** repeat the
  command list bare `?` already shows.
- **`help <topic>`** goes deeper on one subject. Topics are a fixed set,
  the same "select from an enum" pattern as `device.type`/`transport`, so
  `help ?` lists them and `<cr>`:

  ```
  network-lab# help ?
    claude               Show Claude Code integration help
    workflow             Show the recommended Network Lab workflow
    editor               Show external YAML editor usage
    cli                  Show CLI usage information
    <cr>
  ```

  | Topic | Covers |
  |-------|--------|
  | `help claude` | How Claude Code uses Network Lab MCP: the seven MCP tools, what Claude does/doesn't receive, device addressing by logical ID, and how to register/start Claude Code. |
  | `help workflow` | The recommended end-to-end sequence: access-info -> topology -> scenario -> reference -> running-config -> review -> commit -> use Claude Code. |
  | `help editor` | External YAML editor usage: resolution order, candidate-only editing, when `commit`/`clear` apply. |
  | `help cli` | A short cheat sheet for `?`, Tab, `configure`, `show running-config`/`show configuration`, `commit`, `clear`, `exit`, `end`, Ctrl-C — not a substitute for this document. |

`help` and `help <topic>` are available in every mode (EXEC and every
configuration mode) with identical content — they answer a question about
Network Lab MCP itself, not about the current grammar position, so they do
not vary by mode the way `?` does. Both are read-only: neither touches
candidate state, dirty state, or any file.

## `show version`

`show version` is a read-only software-information command, **available
only in EXEC mode** (it is software-level information, not part of any
configuration context), that never touches candidate/dirty state, never
reads access-info, and does not depend on the active topology/scenario/
reference. `show ?` never lists it and Tab never completes it outside
EXEC; typing it explicitly in a configuration mode hits the ordinary
invalid-command/caret error, exactly like any other keyword that does not
exist at that grammar position:

```
network-lab# show version
Network Lab MCP

  Version:       0.2.0
  Release date:  2026-10-03
  Git commit:    abc1234
  Git branch:    main
  Author:        Eitaro Uemura
  License:       GNU General Public License v3.0
  Python:        3.12.3
  Platform:      Linux
  MCP SDK:       2.2.0
network-lab#
```

```
network-lab(config)# show version
     ^
% Invalid input detected at '^' marker.
```

- **Version**, **Author**, and **License** are read from installed package
  metadata (`pyproject.toml` is their single source of truth, via
  `importlib.metadata`) — never a second hard-coded copy.
- **Release date** is a small constant in `network_lab_mcp/__init__.py`
  (`__release_date__`), since standard packaging metadata has no field for
  it; it is a separate concept from **Version** (a release date is not a
  version identifier).
- **Git commit** and **Git branch** are both resolved dynamically
  (`git rev-parse --short HEAD` / `git rev-parse --abbrev-ref HEAD` against
  the repository checkout) every time the command runs — neither is ever
  hard-coded to whatever revision/branch happened to be current when a
  feature was written. If `git` is unavailable, the checkout is not a git
  repository, or resolution fails for any other reason, each prints
  `unavailable` instead of failing the command or leaking a raw git error.
  A detached-HEAD checkout prints the literal `HEAD` for **Git branch**
  instead — a true answer, not an error.
- **Python** is `platform.python_version()` of the interpreter actually
  running the CLI, and **Platform** is `platform.system()` (`Linux`/
  `Darwin`/`Windows`) — never hard-coded version/OS strings.
- **MCP SDK** is the installed `mcp` package's own version (via
  `importlib.metadata`, the same mechanism **Version**/**Author**/
  **License** use) — it is a required runtime dependency, so this is
  reliable whenever Network Lab MCP itself runs at all.

## EXEC mode commands

| Command | Effect |
|---------|--------|
| `configure` (alias: `configure terminal`) | Enter global configuration mode; initializes a running-config candidate (see "Candidate model" below). |
| `show running-config` | Show the committed MCP definition selection (access-info/topology/scenario/reference names) — never a definition's own content. Also the meaning in global/`running` mode; a definition mode (topology/access-info/scenario/reference) scopes this to that object's own committed state instead — see "`show running-config` vs. `show configuration`". |
| `show running-config access-info` | Show the committed *content* of the active access-info definition (clear-text passwords, same policy as definition-mode `show running-config`) — see "`show running-config <definition-type>`" below. |
| `show running-config topology` | Show the committed content of the active topology definition. |
| `show running-config scenario` | Show the committed content of the active scenario definition. |
| `show running-config reference` | Show the committed content of *every* active reference, in committed `active_references` order. |
| `show running-config reference <name>` | Show the committed content of just one active reference. `<name>` must already be active; Tab/`?` only complete active reference names. |
| `show version` | Show Network Lab MCP's own version/license/runtime information — see "`show version`" above. |
| `show logging` | List every device's persistent terminal session logs (`logs/terminal/<device-id>/<session-start>.log`), newest first — see "`show logging`" below. EXEC only, like `show version`. |
| `show logging summary` | Summarize every valid device logging directory with its eligible log-file count, plus a Total row. |
| `show logging <device-id>` | List just that device's logs, newest first. `<device-id>` Tab/`?`-completes from devices that currently have a valid logging directory. |
| `show logging <device-id> <log-file>` | Show one log file's raw contents. Read-only; never modifies/deletes/rotates. `<log-file>` Tab/`?`-completes from that device's own log filenames only — an unknown device or filename is rejected, never a path-traversal attempt (`../`, an absolute path). |
| `delete logging all` | Delete every eligible stored terminal log, across every device, leaving device logging directories in place — see "`delete logging`" below. Requires `[y/N]` confirmation. |
| `delete logging all directory` | Same, and additionally remove every valid (now-empty) device logging directory. `logs/terminal/` itself is never removed. Requires confirmation. |
| `delete logging <device-id> all` | Delete every eligible stored terminal log for one device, leaving its directory in place. Requires confirmation. |
| `delete logging <device-id> directory` | Delete a device's eligible logs, then remove its now-empty logging directory. Requires confirmation. |
| `delete logging <device-id> <log-file>` | Delete exactly one eligible stored terminal log, by its exact filename (same completion/eligibility rules as `show logging`). Requires confirmation. |
| `terminal monitor <device-id>` | Stream the current Network Lab MCP terminal activity for a device into the local terminal, with a live read-only status bar at the bottom — see "`terminal monitor`" below. `<device-id>` Tab/`?`-completes from the committed active topology's devices, union'd with any device that already has an existing managed or Discovery session. |
| `help` / `help <topic>` | Network Lab MCP Quick Start/usage help — see "`?` vs. `help`" above. Not the same as bare `?`. |
| `exit` / `quit` | Terminate the CLI process. Only reachable in EXEC mode, where by construction no candidate configuration exists. |

### `terminal monitor`

`terminal monitor <device-id>` streams the current Network Lab MCP terminal
activity for a device — the same terminal an AI/MCP client (or Discovery) is
driving — into the local terminal, with a small live status bar kept at the
bottom. It is EXEC-only and strictly observational:

- Never sends anything to the pane, never creates or closes a session. Only
  `has-session`/`list-panes`/`capture-pane`-style read-only introspection.
- **Activity streams like normal CLI output; only the status bar is live.**
  New terminal output is printed once, in order, and stays in the terminal
  emulator's normal scrollback — scroll up with the terminal emulator itself
  to review it, both while the monitor is running and after it exits. Only
  the 3-line status block at the bottom
  (`Monitoring terminal <device> | Read-only | Source: ... | Status: ... |
  q: quit`, width-adaptive) is continuously redrawn in place; it never
  accumulates into scrollback. A one-line `[monitor] ...` marker is printed
  only when activity starts, ends, switches source, or resumes after a
  session is recreated — never on every ordinary poll — so scrollback stays
  readable. The monitor never uses the terminal's alternate screen buffer,
  unlike a full-screen pager.
- **Source priority: managed session > Discovery session > waiting.** It
  prefers the normal managed session (`network-lab-device-<device-id>`, the
  one `terminal_open()`/`terminal_send()`/`terminal_read()` use); if none
  exists, it falls back to an active Discovery bootstrap session
  (`network-lab-discovery-<device-id>`, created by `discover topology`) for
  the same device; if neither exists, the status bar shows `Status: waiting
  for terminal activity`. This is re-evaluated on every refresh — no restart
  is needed when a session appears, disappears, or a higher-priority source
  takes over; a `[monitor] switched to ...`/`... started`/`... ended;
  waiting`/`... resumed` marker documents each such transition in scrollback.
  Discovery sessions are normally short-lived; the monitor never delays or
  blocks Discovery's own cleanup, and never causes a Discovery session to be
  created.
- **New output is appended exactly once; nothing is ever lost merely
  because it scrolled.** The monitor tracks how much of a session's output
  it has already streamed and only prints the new tail on each poll —
  repeated identical lines (e.g. duplicate routes) are preserved exactly,
  never collapsed, and a burst of output between two polls is never lost
  even if it exceeds the pane's own visible height. If the *same* session
  name is recreated (old instance gone, new one started) — with or without
  an intervening `waiting` observation — the monitor detects this is a new
  instance and starts a fresh bounded context for it, printing a `resumed`
  marker, rather than silently dropping or misattributing its output.
- Works even if no session of either kind exists yet — it starts in
  `Status: waiting for terminal activity` and begins streaming automatically
  the moment one appears, printing a bounded recent-context window (not the
  entire history) as its first output.
- Session loss (`terminal_close()`, the SSH/telnet process exiting, Discovery
  cleanup, the whole tmux session disappearing) never exits the monitor and
  never erases what it already streamed — the status bar returns to
  `Status: waiting for terminal activity` (or, if the pane still exists but
  its process has exited, `Status: ended`) and automatically resumes
  streaming once a session reappears (falling back to Discovery, or
  recovering to managed, per the same priority rule).
- Only the human can end it: press `q` or `Q` (no Enter needed) or Ctrl-C.
  Every other keystroke is ignored and never reaches the device. Quitting
  removes only the live status bar and returns cleanly to `network-lab#` —
  everything already streamed remains in scrollback; command history and CLI
  state are unaffected.
- Multiple monitors — of the same or different devices, from separate
  `./run_cli.sh` processes or CLI sessions — are independent; none of them
  affect each other, the AI's own terminal operations, or Discovery.
- `<device-id>` must be a device in the *committed* active topology, a
  device that already has a managed session, or a device that already has an
  active Discovery session (so a device being discovered for the first time
  is still a valid target); an unrecognized name is rejected immediately
  rather than waiting forever.

The local terminal scrollback is a human-observability convenience, not
durable storage: closing the terminal window discards it like any other
scrollback. Durable historical evidence remains the job of the existing
`logs/terminal/<device-id>/*.log` files, entirely unaffected by monitoring
— see ["`show logging`"](#show-logging) below.

### `show logging`

Bare `show logging` lists every device's persistent logs newest-first;
`show logging summary` is a separate, explicit per-device eligible-log-count
view:

```
network-lab# show logging
Device  Session Start        Log File
------  -------------------  --------------------
R1      2026-09-21 10:32:10  20260921T103210.log
R2      2026-09-21 10:31:55  20260921T103155.log

network-lab# show logging summary
Device  Log Files
------  ---------
R1             12
R2              8
SW2             0
------  ---------
Total          20

network-lab# show logging R1
Session Start        Log File
-------------------  --------------------
2026-09-21 10:32:10  20260921T103210.log

network-lab# show logging R1 20260921T103210.log
<terminal transcript>
```

`show logging summary` shows one row per valid device logging directory
with its eligible log-file count — an empty directory (e.g. left behind
by `delete logging <device-id> all`) is still shown, with `0`, since it
remains a meaningful `delete logging <device-id> directory` target —
plus a Total row (the sum of eligible logs, never a directory count). A
symlink is never a valid device logging directory and never appears in
either view; an unknown/non-log file inside a device directory is never
counted. No logs/no valid device logging directories at all prints `No
terminal logs found.` for either bare `show logging` or `show logging
summary` (never a traceback, never creates `logs/terminal/`). `show
logging` (in any of its four forms) is EXEC-only, like `show version` —
configuration mode `show` semantics remain exactly `show`/`show
configuration`/`show running-config`.

### `delete logging`

EXEC-only, like `show logging`; reuses the exact same stored-log
enumeration (`show logging` and `delete logging` can never disagree about
what exists). Bare `delete logging` and `delete logging <device-id>` are
deliberately **not** executable — only the five explicit forms below are,
and every one of them requires an explicit `[y/N]` confirmation before
anything is deleted:

```
network-lab# delete logging R1 20260921T091500.log
Delete terminal log R1/20260921T091500.log? [y/N]: y
Deleted terminal log R1/20260921T091500.log.

network-lab# delete logging R1 all
Delete all 4 terminal logs for R1? [y/N]: y
Deleted 4 terminal logs for R1.

network-lab# delete logging R1 directory
Delete empty logging directory for R1? [y/N]: y
Deleted logging directory for R1.

network-lab# delete logging all
Delete all 17 terminal logs? [y/N]: y
Deleted 17 terminal logs.

network-lab# delete logging all directory
Delete all 17 terminal logs and 6 device log directories? [y/N]: y
Deleted 17 terminal logs and 6 device log directories.
```

`[y/N]`: `y`/`Y` confirms; `n`/`N`/Enter alone (safe default) cancels
(`Delete cancelled.`, nothing changed); Ctrl-C/EOF also cancel safely;
anything else re-prompts (`Please enter y or n.`) instead of being
silently treated either way. The answer is read directly (`input()`),
never through the line-editing `PromptSession`, so it never enters
command history. A `delete logging ...` line encountered inside a
multi-line paste always fails closed (`% delete logging requires
interactive confirmation and cannot be run from multi-line paste.`) — a
paste can never safely supply, or be mistaken for, the answer.

Confirming is not the last word: right after `y`, the whole operation is
preflighted *again* and compared against what was originally shown. If
anything eligible changed while the operator was deciding — a new log
appeared, a writer became active, a directory's contents changed — the
command aborts with `% Logging state changed while waiting for
confirmation. % No logs were deleted. Retry the command.` (or the more
specific rejection, e.g. an active-writer message, if that's what
changed) rather than silently deleting a different set than what was
shown.

Safety, in order of how the implementation actually enforces it:

- **Eligible files only.** Deletion targets exactly the files `show
  logging` already lists for that device — no wildcards (`delete logging
  R1 *.log` is not supported; the filename must match an existing log
  exactly), no recursive directory deletion, and a symlink (a log file, or
  a device directory itself) is never treated as an eligible target
  (excluded, not followed).
- **Path confinement.** A device or filename token must exactly match one
  already enumerated by the stored-log subsystem; there is no string
  concatenation into a filesystem path, so `../`-style traversal or an
  absolute path is simply never matched, not specially detected.
- **Active-writer protection, at device granularity.** A production
  terminal session (`terminal_open()`) and a private Discovery bootstrap
  session both attach persistent logging to the same
  `logs/terminal/<device-id>/` directory, keyed only by device name —
  tmux's pipe-pane is attached once at session creation and never
  explicitly detached before the session ends. The current architecture
  has no registry mapping a live session to the *exact* log file it is
  writing (that path is computed once, at creation, and never recorded
  anywhere retrievable afterward), so protection is conservatively
  device-level, not file-level: if **either** a production or a Discovery
  session currently exists for a device, **none** of that device's logs
  or its directory can be deleted, regardless of tmux session-namespace
  classification (`Cannot delete logging directory for '<device>' while a
  managed terminal session is still open.`). This is a deliberate,
  documented limitation, not finer-grained protection than the
  architecture can actually prove. An active device is rejected *before*
  any confirmation prompt is shown.
- **Bulk operations fail closed.** `delete logging all`/`all directory`
  and `delete logging <device-id> all`/`directory` preflight the *entire*
  target set before ever asking for confirmation: if any device targeted
  by the operation currently has an active writer, or (for a `directory`
  form) any targeted device directory contains anything unexpected, the
  whole operation is rejected and deletes nothing (not even the logs of
  devices that are themselves fine).
- **Directory cleanup is non-recursive and exact.** `<device-id>
  directory` / `all directory` first prove a device's logging directory
  contains *only* eligible log files (no unknown regular file, no nested
  directory, no symlink of any kind) before unlinking those files and
  then `rmdir`-ing the now-empty directory — never `shutil.rmtree`/`rm
  -rf`. Any unexpected entry blocks that device (and, for `all
  directory`, the entire operation) before anything is touched.
  `logs/terminal/` itself is never a deletion target, only its valid
  direct child device directories are, and an unrelated file directly
  under `logs/terminal/` is never touched either.
- **`all` and `directory` are a deliberate distinction.** `delete logging
  all` / `delete logging <device-id> all` delete eligible log *files*
  only and always leave the device directory behind (so `show logging
  summary` keeps showing that device, with `0`); `directory` additionally
  removes the now-empty directory itself. A directory removed this way is
  recreated automatically the next time normal logging starts for that
  device (session creation always ensures its own log directory exists).
- **No session side effects.** Deletion never closes a terminal session,
  kills tmux, stops pipe-pane, or disturbs a Discovery bootstrap — an
  active-log rejection leaves the writing session completely untouched.
- **Clear, non-fatal errors.** An empty/nonexistent device, a nonexistent
  exact filename, or nothing eligible at all is a clear error (`% No
  terminal logs found for device 'R9'.` / `% No log file '<name>' for
  device '<id>'.` / `% No terminal logs found.` / `% No device logging
  directories found.`) with no confirmation prompt and no mutation, never
  a silent no-op or a fabricated success.

### `show running-config <definition-type>`

Bare `show running-config` shows *which* definitions are active — it is
unchanged. These EXEC-only commands are a read-only dereference of one of
those active selections: they read committed running-config, resolve the
active name, load *that* committed definition, and render it with the
exact same renderer definition-mode `show running-config` uses. They
never read the in-memory candidate, and never scan `lab/` for anything
other than the one resolved name.

```
network-lab# show running-config access-info
access-info sample_lab
 device R1
  type iosxr
  address 192.0.2.11
  ...
!

network-lab# show running-config topology
topology sample_lab
 device R1
  type iosxr
 !
!

network-lab# show running-config scenario
name: sample
...

network-lab# show running-config reference
name: iosxr_basics
...
!
name: sr_mpls
...

network-lab# show running-config reference sr_mpls
name: sr_mpls
...
```

`access-info`/`topology`/`scenario` are **single-selection**: running-config
has at most one active definition of each, so there is nothing to name --
`show running-config access-info test_lab` is rejected the same way an
unexpected extra argument anywhere else is. `access-info` is the one
exception that can legitimately have *zero* active definitions (`% No
active access-info is configured.`); a missing/invalid active topology or
scenario is a broken running state and fails the same way loading it
anywhere else would.

`reference` is **multi-select** — `active_references` is an ordered list
(committed references keep their committed order; newly activated ones are
appended in activation order; there is no reorder command, so unselecting
and re-selecting the same reference is an exact net-zero: no diff, no
`settings.yaml` rewrite), so:

- bare `show running-config reference` renders *every* active reference,
  in committed order (never sorted, never merged), separated by a single
  `!` between blocks — for exactly one active reference this is identical
  to `show running-config reference <that-name>` (no extra wrapper);
- `show running-config reference <name>` renders just that one reference,
  but only when `<name>` is currently active. A reference that exists as
  a file under `lab/references/` but is *not* in committed
  `active_references` is rejected (`% Reference '<name>' is not active in
  running-config.`) -- this command is not a general reference-file
  lookup, and `<name>` is a case-sensitive object identifier like
  everywhere else in this CLI (no fuzzy/prefix matching). Tab/`?` only
  ever complete currently-active reference names.
- loading is fail-closed and atomic: if any active reference fails to
  load/validate, the whole multi-reference view fails before anything is
  printed -- never a partial list.

`show running-config access-info` follows the existing password display
policy (clear text, same as definition-mode `show running-config` for
access-info) — it does not change what MCP tools, logs, or errors expose.

## Global configuration mode commands

| Command | Effect |
|---------|--------|
| `running-config` | Enter running-config selection mode (`network-lab(config-running)#`). |
| `discover topology` | Run IOS XR/IOS XE/IOS LLDP+CDP discovery (L2 only; no L3 command is sent) against the committed `active_access_info` and **rebuild** the same-named topology candidate from this run alone (no merge with existing links; earlier L3 data is not carried over) — see [architecture.md](architecture.md#topology-discovery). Enters topology definition mode on success. Never commits, never selects `active_topology`. Rejected if a *different* definition is open and dirty, or if the target topology has manual edits since the last discovery (use `clear`). Devices/links removed by the rebuild are listed before the candidate is replaced. On any failure the candidate is left unchanged (`% Topology candidate not updated.`). |
| `discover topology l3` | Same, plus per-device L3 interface enrichment (IPv4 + VRF). Any L3 collection/parse failure fails the whole run. |
| `access-info <name>` | Create or edit an access-info definition; `<name>` existing loads it, otherwise starts a new one. Enters access-info definition mode. |
| `topology <name>` | Create or edit a topology definition (see "Case-only topology-name collision safeguard" below). Enters topology definition mode. |
| `scenario <name>` | Create or edit a scenario definition. Enters scenario definition mode. |
| `reference <name>` | Create or edit a reference definition. Enters reference definition mode. |
| `no access-info <name>` / `no topology <name>` / `no scenario <name>` / `no reference <name>` | Candidate deletion of a **stored definition** of that kind — see "`no <kind> <name>`" below. Not the same as `config-running# no ...` commands, which select/deactivate a *running-config selection* instead (and, for topology, don't exist at all — see that section). |
| `show` / `show configuration` | Uncommitted changes only: the running-config candidate's delta, aggregated with the open definition's delta if one is open (each rendered by its own type-aware delta renderer) — see "`show running-config` vs. `show configuration`" below. Nothing open and nothing changed -> no output. |
| `show running-config` | The committed MCP running-config selection, same as EXEC — **not** the open definition's own content, even if one is open in the background. |
| `commit` | Validate and persist the candidate. Stays in global configuration mode. |
| `root` | Not offered here — global configuration mode is already the configuration root. |
| `clear` | Discard the entire uncommitted configure-session state (running-config candidate and any open definition candidate); stay in the current mode (or the nearest still-valid parent — see "`clear`" below). |
| `end` / `exit` | Both return to EXEC here, guarded by `overall_dirty`. |
| `help` / `help <topic>` / `?` | As in EXEC mode — see above. |

Opening a *different* definition (of any kind) while the current one is
dirty is blocked — see "Definition switching guard" below.

`help` and `help <topic>` behave identically in every mode (see above); the
per-mode tables below omit them for brevity and list only what differs from
EXEC/global. `show version` does **not** behave identically everywhere —
it is EXEC-only (see "`show version`" above and "`show running-config` vs.
`show configuration`" below) and is likewise omitted from the tables below.

## Running-config selection mode commands

| Command | Effect |
|---------|--------|
| `access-info <name>` | Select the access-info MCP/`terminal_open()` will use. `<name>` must already exist as a committed access-info definition, *or* be the access-info currently being created/edited in this same configure session, *or* one not currently pending whole-definition deletion in global configuration mode — Tab/`?` dynamically list exactly this set (`Select access-info for MCP`), never a generic `<name>` placeholder. |
| `access-info` (bare) | Enter the **name-less** submode `network-lab(config-running-access-info)#` (see below). Entering never changes the candidate. `access-info ?` lists the existing names plus `<cr>`. The same bare form exists for `topology`, `scenario` and `reference` (`config-running-topology` / `-scenario` / `-reference`); in **global** configuration mode every bare `access-info`/`topology`/`scenario`/`reference` is still `% Incomplete command.` (those `?` have no `<cr>`). |
| `no access-info` | Remove the access-info selection from the candidate (omission, not a sentinel value). A legitimate, fail-closed state once committed: `terminal_open()` then refuses every device until one is selected again. |
| `topology <name>` | Select the topology MCP will use (`Select topology for MCP`). Same existence/dynamic-completion rule as `access-info`. There is no `no topology` here; `active_topology` is a mandatory running-config field. The only way to unselect it in the *candidate* is `no <name>` in `config-running-topology`, and `commit` rejects a candidate without one (`% Running configuration is missing a valid active topology.`). This is unrelated to `no topology <name>` in global configuration mode, which deletes a **stored topology definition** instead — see "`no <kind> <name>`" below. |
| `scenario <name>` | Select the scenario MCP will use (`Select scenario for MCP`). Same existence/dynamic-completion rule. |
| `reference <name>` | Activate `<name>` among the selected references (`Activate reference for MCP` — additive, not a singleton "select"). Same existence/dynamic-completion rule; duplicates are rejected. |
| `no reference <name>` | Remove `<name>` from the selected references. |
| `access-info <name>` / `topology <name>` / `scenario <name>` / `reference <name>` | Select (or, for a reference, activate) the entry as before **and** enter its running-entry submode `network-lab(config-running-<kind>-<name>)#`. |
| `show` / `show configuration` | The running-config candidate's delta: only the selection(s) that actually changed (e.g. just `access-info test_lab`, or just `no access-info`) — unrelated unchanged selections are never repeated. |
| `show running-config` | Show the full committed selection on disk (unaffected by the candidate). |
| `commit` / `clear` / `root` / `end` / `exit` | As in global configuration mode. `root`/`exit` return one level up, to global configuration mode (no dirty guard — still the same overall configure session). `commit` stays in `running` mode. |

### Name-less selection submodes (`config-running-access-info` / `-topology` / `-scenario` / `-reference`)

All four kinds share one design (written below for access-info; the same rules apply to the others with the per-kind differences listed after the bullets). Entered with the bare keyword from `config-running`; the selection policy of each kind is unchanged: access-info 0 or 1, topology exactly 1, scenario exactly 1, reference 0 or more (additive, duplicates are a no-op).

- `<name>` (a definition name typed on its own) selects it through the same method as `access-info <name>` and **stays** in this submode; the single-selection rules and Net-zero behavior are identical to the named path (same name again, or A → B → A, leaves the candidate clean).
- Invalid names print the usual `% Access information '<name>' does not exist.`; the candidate, the submode and the stored definitions are unchanged.
- `no <name>` **unselects** the access-info currently selected in the running candidate and stays in this submode. `<name>` must equal the current selection exactly (no prefix matching; otherwise `% Access information '<name>' is not currently selected.`, candidate unchanged — also when nothing is selected). Bare `no` is `% Incomplete command.`; `no ?` lists only the candidate's selected name (none when unselected) with no `<cr>`, and Help/Tab are recomputed from the candidate after every change (commit/clear included). It uses the same Candidate update as `config-running# no access-info`, never deletes a definition (global `no access-info <name>` does that) and never touches `running_descriptions`. Net-zero follows the usual rules (`no A` then `A`, or `A` then `no A` from an unselected baseline, is clean).
- There is no `description` here: `description foo` is rejected with `% Invalid input ...` plus `% Use the named mode: access-info <name>`. Edit descriptions in the named submode (`access-info <name>` → `description <text>` / `no description`). **`no description` in this submode is never a description removal**: it is `no <name>` with the name `description` — it unselects only if the selected access-info is literally named `description`, otherwise it is rejected as not selected (also when nothing is selected). The submode keeps no hidden "last selected" target.
- `?` lists the keywords valid here (`show`, `no`, `clear`, `commit`, `root`, `end`, `exit`, `help`) plus the selectable names.
- **Keyword collisions**: keywords win and match case-insensitively and by unique prefix, so a definition named `show`, `Show`, `sh…`, `c` (ambiguous: clear/commit) or `e` (end/exit) cannot be selected by typing it here. Select it from the parent with `access-info <name>` (always works; that enters its named submode). A definition literally named `description` or `no` is selectable here as a plain name.
- `exit` returns to `config-running` keeping the candidate (never refused for a dirty running candidate); `root`/`end` follow the usual rules (`end` is refused while dirty); `commit`/`clear` stay in this submode, and a failed commit keeps it too. `show` / `show configuration` here show **only this kind's** selection delta (`<name>` / `no <name>` lines), not the whole running-config delta.

**Per-kind differences.** *Topology / Scenario*: `no <name>` may leave the **candidate** unselected (`show configuration` prints a block naming the **committed** selection — `topology` / ` no <committed-name>` / `!` (likewise `scenario`) — because `no topology` / `no scenario` have no running grammar; the block replays from `config-running` or global, see "Replay guarantees" below), but `commit` is rejected with the existing validation (`% Running configuration is missing a valid active topology.` / `... active scenario.`) — the candidate, the submode and the committed settings stay as they were, there is no auto-rollback, selecting a valid one again (in the same commit) makes `commit` succeed, and `clear` restores the committed selection. Scenario is required today because the existing commit validation already requires it; nothing new was added. *Reference*: `no <name>` removes one selected reference (`% Reference '<name>' is not currently selected.` otherwise) and `no ?` lists every candidate-selected reference; selecting a reference with a pending definition deletion is refused (D6), unselect→reselect restores the committed order and is net-zero (D7), and a `no <name>` plus a global `no reference <name>` in one commit keeps the settings-first persistence order (Case 12).

Paste: the mode is recognized by the distinct name `running_<kind>_common` (never a running-entry mode, so it never reaches the description handlers). A `!` closes this submode and `config-running` only when it follows a command executed *inside* that section (a leading `!`, a `!` right after entering a mode, or right after `exit` is a no-op), so a Common submode's own `show configuration` output (kind-scoped, no `!`) replays into that submode, and the running-scope output replays from `config-running` or global configuration mode. **Behavior change**: a bare `access-info` line used to be an incomplete-command error that stopped a paste; it is now valid, and later lines up to the next `!` are read as definition names. `show running-config` output (bare `topology`/`scenario`/`reference` headings) remains non-replayable and stops at the first such heading; because the `!` closes the submode, a heading is never mistaken for an access-info named `topology`.

### Running-entry submodes (`config-running-<kind>-<name>`)

Edit metadata of one **currently active** running-config entry — distinct from the definition editor (`config-<kind>-<name>`), which edits the definition itself. Parent-level selection commands are not available here (use `exit`, then select another entry).

| Command | Description |
|---|---|
| `description <text>` | Set the entry's human-readable description (non-empty). Stored in `settings.yaml` under `running_descriptions`, shown by `show running-config`, never written to a definition. |
| `no description` | Remove only the description; the entry stays active. |
| `show` / `clear` / `commit` / `root` / `end` / `exit` / `help` | As in other submodes. `clear` returns to `config-running` if it makes the entry inactive. |

A description disappears when its entry stops being active at commit; re-selecting the same name later starts blank.

## Running-config selection model

The four running-config selections are not all symmetric:

| Type | Selection | Can be unset | Behavior |
|------|-----------|--------------|----------|
| `access-info` | Single | Yes | Without it, terminal access and discovery are unavailable |
| `topology` | Single | No | Required; use `topology <name>` to switch |
| `scenario` | Single | No | Required; use `scenario <name>` to switch |
| `reference` | Multiple | Yes | Use `no reference <name>` to remove one |

`network-lab(config-running)# no ?` prints exactly this table as an
explanatory footer, right after its two real candidates:

```
network-lab(config-running)# no ?
  access-info          Remove the access information selection
  reference            Remove a reference used by MCP

Running-config selection model:
Type         Selection     Can be unset  Behavior
-----------  ------------  ------------  --------------------------------------------
access-info  Single        Yes           Without it, terminal access and discovery
                                          are unavailable
topology     Single        No            Required; use "topology <name>" to switch
scenario     Single        No            Required; use "scenario <name>" to switch
reference    Multiple      Yes           Use "no reference <name>" to remove one

network-lab(config-running)# no
```

`topology`/`scenario` appear **only** in this footer's prose — they are not
real grammar candidates under `no` in `running` mode, are never Tab-completable,
and `no topology`/`no scenario` are still rejected the same way they always
were. The footer is scoped to exactly the spaced `no ?` help context: the
attached `no?` form (IOS XR's `token?` vs `token ?` distinction — see "`?`
vs. `help`" below) shows its ordinary one-line summary instead, and the
footer never appears in EXEC/global configuration/any definition submode's
own `no ?`.

`show running-config` always renders the `access-info` and `reference`
sections, even when unset/empty, using a display-only `<none>` marker
instead of omitting the section:

```
network-lab# show running-config
!
 access-info
  <none>
!
 topology
  sample_lab
!
 scenario
  sample
!
 reference
  <none>
!
```

`<none>` is presentation only: it is never written to `lab/settings.yaml`
(absence is still represented natively — a missing key / empty list), never
a valid value for `access-info <name>`/`reference <name>`, never a Tab/`?`
candidate, and never appears in `show configuration`'s candidate-diff syntax
(deleting the access-info selection there is still rendered as
`no access-info`, unchanged). A pending `no access-info`/`no reference
<name>` in the candidate does not show `<none>` until `commit` succeeds; a
failed commit leaves the previously committed value displayed. `topology`
and `scenario` never show `<none>` — they are mandatory selections, and a
missing value there is left exactly as before (not papered over).

## Topology definition mode commands

| Command | Effect |
|---------|--------|
| `description <text>` | Set the topology's free-form description (rest-of-line argument). |
| `device <name>` | Create or edit a device's *safe* metadata (case-sensitive); enters topology device mode. |
| `edit` | Open the topology candidate in an external YAML editor (see "External YAML editor" below). |
| `show running-config` | This topology's own **full committed** state, re-read fresh from disk — empty if it has never been committed. **Not** the MCP running-config selection. |
| `show` / `show configuration` | This topology's **uncommitted changes only** (field-level for `description`/device `type`; falls back to the whole candidate block if something the structured CLI does not model, such as `links` or a device's `interfaces`, changed — see "`show running-config` vs. `show configuration`" below). |
| `commit` | Validate and persist; stays in topology definition mode. |
| `root` | Jump to global configuration mode, candidate preserved. |
| `clear` | As above (topology-scoped edits are part of the same overall candidate). |
| `end` | Return directly to EXEC; blocked while dirty. |
| `exit` | Return one level up, to global configuration mode. No dirty check (still the same configuration session). |
| `help` / `help <topic>` / `?` | As in EXEC mode. |

## Topology device mode commands (safe fields only)

| Command | Effect |
|---------|--------|
| `type <iosxr\|iosxe\|ios\|nxos\|host>` | Device type. See README.md's ["Supported device types & Discovery scope"](../README.md#supported-device-types--discovery-scope). |
| `show running-config` | Just *this device's* full committed block from the topology above — empty if this device (or the whole topology) has never been committed. |
| `show` / `show configuration` | Just this device's uncommitted field(s) (only `type` is modeled here). |
| `commit` | Validate and persist; stays in this device's mode. |
| `root` | Jump to global configuration mode, candidate preserved. |
| `clear` / `end` / `help` | As above. |
| `exit` | Return one level up, to topology definition mode. |

Topology device mode intentionally has **no** `address`/`transport`/`port`/
`username`/`password` — those private connection fields live under
access-info device mode instead, since topology is exposed to Claude via
`get_active_topology()` and must never carry them. A device's `interfaces`
(Layer-3 enrichment — ipv4_address/vrf per interface) is likewise not a
structured CLI field: it is populated by `discover topology` or the
external `edit` command, and displayed as read-only review information by
`show`/`show configuration`/`show running-config`.

## Access-info definition mode commands

| Command | Effect |
|---------|--------|
| `device <name>` | Create or edit a device's private connection data (case-sensitive); enters access-info device mode. |
| `jump-host <name>` | Create or edit a reusable single-hop OpenSSH ProxyJump endpoint (case-sensitive); enters access-info jump-host mode. See README.md's ["Single-hop SSH jump hosts"](../README.md#single-hop-ssh-jump-hosts-proxyjump). |
| `no device <name>` | Remove a device entirely from the candidate (Tab/`?` complete only names present in the *current candidate*, including ones created but not yet committed in this same session). Does not touch committed YAML until `commit`; does not cascade to topology or any other definition. Removing a nonexistent name is a clear error, not a silent no-op. |
| `no jump-host <name>` | Remove a jump host entirely from the candidate, the same way. Does not cascade: a device still referencing the removed jump host is left as-is, and `commit` will then fail with a dangling-reference error until the reference is fixed or cleared (`no jump-host` inside that device's own mode) or the jump host is restored. |
| `show running-config` | This access-info definition's own **full committed** state, re-read fresh from disk — empty if it has never been committed. **Not** the MCP running-config selection. Passwords in **clear text** (see "Password display policy" below). |
| `show` / `show configuration` | This access-info definition's **uncommitted changes only**: only the devices/jump hosts with at least one changed field, and within each only the changed fields. Passwords in clear text. |
| `commit` | Validate and persist; stays in access-info definition mode. |
| `root` | Jump to global configuration mode, candidate preserved. |
| `clear` / `end` / `help` | As above. |
| `exit` | Return one level up, to global configuration mode. |

access-info has no `edit` command — see
["Why access-info has no external editor"](#why-access-info-has-no-external-editor).

## Access-info device mode commands (private connection fields)

| Command | Effect |
|---------|--------|
| `type <iosxr\|iosxe\|ios\|nxos\|host>` | Device type. Validated against the same SSOT as topology device mode. |
| `address <value>` | Device management address. |
| `transport <ssh\|telnet>` | Device transport. Validated immediately (invalid input is rejected with a caret, not accepted into the candidate). |
| `port <1-65535>` | Device port. Validated immediately. |
| `username <value>` | Device username. |
| `password <value>` | Device password. Stored in plain text (this is a lab tool, not a secret manager); never completed or retained in history (see "Password safety" below) — but shown in **clear text** by this mode's own `show`/`show configuration`/`show running-config` (see "Password display policy"). |
| `jump-host <name>` | Reference a jump host (by name, within this same access-info definition) for a single-hop OpenSSH ProxyJump connection. Tab/`?` complete existing jump-host names in this definition; the name is validated to exist, and this device's `transport` to be `ssh`, at commit time. |
| `no type` / `no address` / `no transport` / `no username` / `no password` / `no port` / `no jump-host` | Clear the corresponding field from the candidate only. Every settable field has a matching `no` command (see "Candidate may be temporarily incomplete" below). |
| `show running-config` | Just *this device's* full committed block from the access-info definition above — empty if this device (or the whole definition) has never been committed. |
| `show` / `show configuration` | Just this device's changed field(s) only. |
| `commit` | Validate and persist; stays in this device's mode. |
| `root` | Jump to global configuration mode, candidate preserved. |
| `clear` / `end` / `help` | As above. |
| `exit` | Return one level up, to access-info definition mode. |

## Access-info jump-host mode commands (single-hop ProxyJump endpoint)

| Command | Effect |
|---------|--------|
| `type <host>` | Jump-host type. A single-value closed enum: only `host` (reusing the shared `device.type` SSOT for matching/normalization, so `HOST`/`h` still work, but `iosxr`/`iosxe`/`nxos` are rejected — a jump host is never a network device). |
| `address <value>` | Jump-host management address. |
| `transport <ssh>` | Jump-host transport. A single-value closed enum: only `ssh` (native OpenSSH ProxyJump is SSH-only). |
| `port <1-65535>` | Jump-host port. |
| `username <value>` | Jump-host username. |
| `password <value>` | Jump-host password. Same plain-text-storage/clear-text-local-display policy as a device password; kept entirely separate from any device's own credentials. |
| `no type` / `no address` / `no transport` / `no username` / `no password` / `no port` | Clear the corresponding field from the candidate only. Every settable field has a matching `no` command, same as device mode. |
| `show running-config` | Just *this jump host's* full committed block — empty if it (or the whole access-info definition) has never been committed. |
| `show` / `show configuration` | Just this jump host's changed field(s) only. |
| `commit` | Validate and persist; stays in this jump host's mode. |
| `root` | Jump to global configuration mode, candidate preserved. |
| `clear` / `end` / `help` | As above. |
| `exit` | Return one level up, to access-info definition mode. |

### Candidate may be temporarily incomplete

Every device/jump-host field that can be `set` can also be `no`-cleared, and
clearing a field never invents an implicit default (`no port` does not
become `port 22`; `no transport` does not become `transport ssh`) — the
field is simply absent from the candidate, exactly as `show configuration`
reports it, until something sets it again or `commit`/`clear` resolves the
session. `type`/`address`/`transport` are no exception: the candidate may
carry a device missing any of them while it is being edited interactively.
`commit` is unchanged and remains the only gate — it validates the
candidate with the same rules as always (`network_lab_mcp.lab`'s existing
validators), so a device that violates one of those rules still fails to
commit. Note that today's validators do not themselves require
`type`/`address`/`transport` to be present (a missing `type`, in
particular, has always been allowed) — this document describes that
existing policy rather than a new one.

## Scenario / reference definition mode commands

| Command | Effect |
|---------|--------|
| `edit` | Open the candidate in an external YAML editor (see below). |
| `show running-config` | This definition's own **full committed** YAML, re-read fresh from disk — empty if it has never been committed. **Not** the MCP running-config selection. |
| `show` / `show configuration` | This definition's **uncommitted changes only**: schema is intentionally not fixed (see [scenario_format.md](scenario_format.md)), so the whole (small) candidate document is shown when anything in it changed, and nothing at all when it hasn't. |
| `commit` | Validate and persist; stays in this definition's mode. |
| `root` | Jump to global configuration mode, candidate preserved. |
| `clear` / `end` / `help` | As above. |
| `exit` | Return one level up, to global configuration mode. |

Device identifier lookup/selection is case-sensitive (`device R1` and
`device r1` are different devices), and so is every other definition name.

## Example session

```
network-lab# conf
network-lab(config)# top srv6_lab
network-lab(config-topology-srv6_lab)# dev R1
network-lab(config-device-R1)# type iosxr
network-lab(config-device-R1)# exit
network-lab(config-topology-srv6_lab)# exit
network-lab(config)# access-info srv6_lab
network-lab(config-access-info-srv6_lab)# device R1
network-lab(config-access-device-R1)# type iosxr
network-lab(config-access-device-R1)# address 192.0.2.11
network-lab(config-access-device-R1)# tra ssh
network-lab(config-access-device-R1)# port 22
network-lab(config-access-device-R1)# username cisco
network-lab(config-access-device-R1)# password cisco
network-lab(config-access-device-R1)# root
network-lab(config)# access-info srv6_lab
network-lab(config-access-info-srv6_lab)# jump-host jump1
network-lab(config-access-jump-host-jump1)# type host
network-lab(config-access-jump-host-jump1)# address 192.0.2.10
network-lab(config-access-jump-host-jump1)# transport ssh
network-lab(config-access-jump-host-jump1)# username jumpuser
network-lab(config-access-jump-host-jump1)# password jumppass
network-lab(config-access-jump-host-jump1)# exit
network-lab(config-access-info-srv6_lab)# device R1
network-lab(config-access-device-R1)# jump-host jump1
network-lab(config-access-device-R1)# root
network-lab(config)# scenario troubleshoot
network-lab(config-scenario-troubleshoot)# edit
network-lab(config-scenario-troubleshoot)# root
network-lab(config)# running-config
network-lab(config-running)# access-info srv6_lab
network-lab(config-running)# topology srv6_lab
network-lab(config-running)# scenario troubleshoot
network-lab(config-running)# reference iosxr_operational
network-lab(config-running)# show
network-lab(config-running)# commit
Commit complete.
network-lab(config-running)# end
network-lab#
```

## Unique fixed-keyword abbreviation

Any *fixed keyword* (`configure`, `running-config`, `access-info`,
`topology`, `scenario`, `reference`, `device`, `transport`, `show`, `clear`,
`commit`, `exit`, `end`, `no`, `help`, `edit`, `username`, `password`,
`port`, `address`, `type`, `description`, `configuration`) can be
abbreviated to any prefix that is *unique at the current grammar position*:

```
conf            -> configure
top srv6_lab    -> topology srv6_lab
dev R1          -> device R1
tra ssh         -> transport ssh
```

A prefix matching more than one keyword at the current position is
**ambiguous** and is rejected outright — nothing is guessed or executed:

```
network-lab(config)# r
% Ambiguous command: "r"
```

(In global configuration mode, `r` matches both `running-config` and
`reference`.) Use Tab or `?` to see the candidates. Object identifiers
(topology/scenario/reference/access-info/device names) are never abbreviated
this way — see below.

## Fixed-keyword case-insensitivity vs. object-identifier case-sensitivity

Fixed CLI keywords are matched **case-insensitively**: `configure`,
`CONFIGURE`, and `Configure` are equivalent, as are nested keywords like
`TRANSPORT` or `Device`. Documentation and displayed help always show the
lowercase form.

Object identifiers — **topology, scenario, reference, access-info, and
device names** — are matched **case-sensitively** and are never case-folded:

```
topology srv6_lab      -> selects/edits the existing topology named exactly srv6_lab
topology SRv6_Lab       -> does NOT silently select srv6_lab (see below)

device R1               -> matches
device r1                -> a different device (or a new one), never R1
```

Under `running-config` mode, a wrong-case `scenario`/`reference`/`topology`
selector is a normal exact-identifier lookup failure (`% Scenario '...' does
not exist.`) — nothing is silently matched or created.

## Case-only topology-name collision safeguard

Global `topology <name>` is special because it can both *open* an existing
topology definition and *implicitly create* a new one. If the entered name
does not exactly match an existing topology, but differs from one **only by
letter case** (nothing else — not `-` vs. `_`, not any added/removed
character), the CLI requires explicit confirmation before creating a
distinct topology:

```
network-lab(config)# topology SRv6_Lab

% An existing topology 'srv6_lab' differs only by letter case.
Create a separate topology named 'SRv6_Lab'? [yes/no]:
```

- An empty answer or anything other than `y`/`yes` is treated as **no**: no
  topology candidate is created or changed, and the CLI remains in global
  configuration mode.
- Answering `yes` creates a new topology candidate under the **exact
  entered case** (`SRv6_Lab`, never rewritten to `srv6_lab`) and enters
  topology definition mode for it, leaving the existing `srv6_lab`
  completely untouched.

This is a narrow safety net, not fuzzy name matching: `srv6-lab`,
`srv6lab`, and `srv_lab` are all ordinary new-topology names relative to an
existing `srv6_lab` and never trigger this prompt. It applies only to
`topology <name>`; access-info/scenario/reference names have no equivalent
safeguard.

## Definition switching guard

Opening a definition (`access-info`/`topology`/`scenario`/`reference
<name>`) while a *different* definition is already open and dirty is
blocked:

```
network-lab(config-topology-lab_a)# description changed
network-lab(config-topology-lab_a)# exit
network-lab(config)# topology lab_b
% Uncommitted changes exist. Use 'commit' or 'clear' before switching.
```

A *clean* definition (loaded, but never semantically changed) can be freely
switched away from. Running-config editing is an independent scope: opening
`running-config` mode, or switching what it selects, is never blocked by a
dirty definition candidate, and vice versa.

## `no <kind> <name>`

`no access-info <name>` / `no topology <name>` / `no scenario <name>` /
`no reference <name>` (global configuration only) candidate-delete one
**stored definition** of that kind — distinct from every `config-running#
<kind> <name>` / `no ...` command, which selects/deactivates a
*running-config selection* instead (and, for topology, there is no
running-config `no topology` at all: `active_topology` is mandatory,
so there is no safe "unset" to fall back to; an unselected candidate is only
reachable as `no <name>` in `config-running-topology`).

```
network-lab(config)# no scenario maintenance_test
network-lab(config)# show
no scenario maintenance_test
network-lab(config)# commit
Commit complete.
```

Deletion is candidate-only until `commit` — nothing is unlinked by `no
<kind> <name>` itself. `<name>`'s file remains, `show running-config`
still shows its committed content (for access-info, in clear text, same
policy as everywhere else), and MCP/runtime continue reading the
committed definition, exactly as before, right up until a successful
commit. A deletion of a reference that is still active fails at `commit` with `% Cannot remove reference '<name>' because it is active in running-config.` followed by `% Deselect it with 'no reference <name>' under 'running-config', then commit again.`; the candidate is kept and `clear` cancels it. A reference with a pending deletion cannot be newly selected under `running-config` (`% Reference '<name>' is pending deletion in this session. Use 'clear' ...`); deselecting it there and committing both in one commit still works. `show configuration` renders exactly one whole-definition line
(`no access-info test_lab`, never a per-field/per-device diff — an
access-info deletion never renders any of that definition's stored
credentials). `clear` cancels the pending deletion (restores the
definition into the candidate, exactly as it existed before, private
access-info fields included) the same way it reverts any other
uncommitted definition edit.

**All four kinds share the same single definition-candidate slot and
the same definition-switching guard above** — a configure session may
have at most one dirty definition candidate at a time, of *any* kind,
and deletion counts as a definition mutation just like editing does.
`topology test_lab` and `access-info test_lab` are different candidate
identities (kind + name) even though the name string matches:

- editing definition A, then attempting to delete a *different*
  definition B (of the same kind or a different one), is rejected until
  A's edit is committed or cleared — and vice versa;
- deleting definition A, then attempting to delete a different
  definition B, is likewise rejected, regardless of kind;
- but operations on the **same** kind+name identity compose freely:
  edit A then delete A (the edit becomes moot — the diff is just `no
  <kind> A`); delete A then re-enter it (cancels the deletion, restoring
  A's original committed content with no net diff); delete A, restore,
  then edit (only the new edit appears in the diff, original unrelated
  content — sibling access-info devices/jump-hosts, topology
  devices/links, etc. — untouched); create a brand-new definition then
  delete it in the same session (net zero — the whole candidate is
  simply discarded, and `commit` writes nothing).

`no <kind> ?` only ever lists names that are actually legal to select
next: every stored definition of that kind when the definition candidate
is clean, only the one already-dirty identity of that same kind if an
edit or pending deletion of it is open, and nothing at all if a
*different* kind (or a different name) is dirty. Execution enforces the
same rule regardless of what help/completion showed.

Deleting a definition never cascades: it never touches running-config's
own selection, another definition, or terminal/Discovery state. If the
definition being deleted is the currently *active* one in running-config
(checked against the candidate running-config, so a combined commit that
also switches the active selection to something else in the same commit
is allowed — e.g. `running-config` / `scenario new_scenario` / `exit` /
`no scenario old_scenario` / `commit`), `commit` fails closed:

```
% Cannot remove topology 'test_lab' because it is active in running-config.
% Cannot remove access information 'test_lab' because it is active in running-config.
% Cannot remove scenario 'maintenance_test' because it is active in running-config.
% Cannot remove reference 'iosxr_basics' because it is active in running-config.
```

## Tab / Ctrl-I completion

Tab (and Ctrl-I, which is the same key at the terminal protocol level)
performs context-sensitive completion of the token under the cursor,
without executing the command (Enter is always required separately):

- **Unique match** (fixed keyword or a single matching dynamic candidate):
  completes the current token in place.
- **Multiple matches**: the current input is left untouched and the
  candidate list is printed below the prompt, which is then redisplayed
  with the original input intact — nothing is guessed.
- **Zero matches**: the input buffer is left untouched; nothing is executed;
  a terminal bell may sound.

Dynamic completion is available for:

| Position | Candidates |
|----------|------------|
| `topology <name>` (global) | Existing topology names on disk |
| `access-info <name>` (global) | Existing access-info definition names on disk |
| `scenario <name>` (global) | Existing scenario names on disk |
| `reference <name>` (global) | Existing reference names on disk |
| `access-info <name>` (running) | Existing access-info definition names on disk |
| `topology <name>` (running) | Existing topology names on disk |
| `scenario <name>` (running) | Existing scenario names on disk |
| `reference <name>` (running) | Existing reference names on disk |
| `no reference <name>` | The running-config candidate's *currently selected* reference names |
| `device <name>` (topology) | Devices already in the open topology candidate |
| `device <name>` (access-info) | Devices already in the open access-info candidate |
| `jump-host <name>` (access-info) | Jump hosts already in the open access-info candidate |
| `jump-host <name>` (access-device) | Jump host names already defined in the same access-info candidate |
| `transport <value>` | `ssh`, `telnet` |
| `transport <value>` (jump-host) | `ssh` only |
| `type <value>` | `iosxr`, `iosxe`, `ios`, `nxos`, `host` |
| `type <value>` (jump-host) | `host` only |

Object-identifier completion is case-sensitive and always displays the
exact stored case:

```
device R<Tab>   -> R1 / R2 (if both exist)
device r<Tab>   -> no match on R1/R2 purely by case folding

topology s<Tab> -> may complete srv6_lab
topology S<Tab> -> does not match srv6_lab merely by case folding
```

Free-form fields (`address`, `username`, `description`) are never
completion candidates. **`password` never offers completion candidates or
reveals a value**, at any position (`password <Tab>` or `password
partial<Tab>` — both a no-op beyond an optional bell).

## Context-sensitive `?`

`?` is a single key press — it never inserts a literal `?` character and
never requires Enter, never touches candidate/mode/history state on its
own, and preserves the input buffer and cursor position. Pressing it
echoes a `prompt + buffer + ?` transcript line into scrollback (exactly what
was typed, the same way a real terminal leaves a record of the keypress),
then the matching help lines, then redisplays the same prompt and buffer so
you can keep typing immediately:

```
network-lab# show ?
  running-config       Show committed MCP definition selection
network-lab# show
```

Repeated `?` presses each leave their own transcript + help block — that is
normal scrollback, not a bug:

```
network-lab# show running-config ?
  <cr>
network-lab# show running-config ?
  <cr>
network-lab# show running-config
```

- **Bare `?`** (empty line, or right after a space): lists every valid next
  token at the current position.
- **Partial-token `?`** (immediately after a partial fixed keyword or a
  partial dynamic object-ID token, no space): lists only the matches that
  still match the partial input, with no `<cr>` -- a partial token is not
  yet a complete command endpoint.
- **Complete-token `?`** (immediately after a fixed keyword or dynamic
  object-ID token that exactly matches, no space): help for *that one*
  already-typed token -- its own description, plus `<cr>` if the command
  may legally terminate there. This is distinct from the same keyword
  followed by a space (below), which asks what may *follow* it instead:

  ```
  network-lab# show running-config access-info?
    access-info          Committed active access-info definition
    <cr>
  network-lab# show running-config access-info ?
    <cr>
  ```

  The same distinction applies to a complete dynamic active-reference
  name (see "`show running-config <definition-type>`" above):

  ```
  network-lab# show running-config reference iosxr_basics?
    iosxr_basics         Committed active reference name
    <cr>
  network-lab# show running-config reference iosxr_basics ?
    <cr>
  ```

- **Next-token `?`** (after a keyword and a space): lists the next
  syntax — a generic hint (e.g. `<name>` for a plain selector argument, such
  as `running-config` mode's `topology <name>`) when nothing has been typed
  yet, matching dynamic candidates once a prefix has been typed for an
  identifier argument, the full small enum list (e.g. `ssh`/`telnet` for
  `transport`, or the five `device.type` values) even with nothing typed, or
  — for a **"select or create"** identifier such as global mode's
  `topology`/`access-info`/`scenario`/`reference <name>`, or `device <name>`
  under topology/access-info — every existing name *plus* a creation hint,
  with nothing typed yet:

  ```
  network-lab(config)# topology ?
    sample               Existing topology
    test                 Existing topology
    <name>               Create or edit topology
  ```

- **`<cr>`**: shown whenever the current position is already a complete,
  executable command (e.g. `commit ?` shows only `<cr>`).

```
network-lab# ?
  configure          Enter configuration mode
  show               Show information
  help               Display Network Lab MCP quick start help
  exit               Exit the CLI
  quit               Exit the CLI

network-lab# con?
  configure

network-lab(config-access-device-R1)# transport ?
  ssh                 Use SSH transport
  telnet              Use Telnet transport

network-lab(config-running)# topology ?
  <name>              Topology name

network-lab(config)# commit ?
  <cr>
```

Object-identifier candidates shown through `?` preserve exact stored case,
the same as Tab completion. `password ?` shows only the generic
`<password>` hint — never a real value or candidate list.

## Command history

In-process-only command history (never written to disk, and gone once the
CLI exits):

- Up Arrow / Ctrl-P: previous command
- Down Arrow / Ctrl-N: next/newer command

A `password`-setting command (including an abbreviated form like `pas
cisco123`) is never added to this in-memory history, so it can never be
recalled with Up Arrow either. A pasted multi-line block (see below) is
never stored as one history entry either way; if none of its lines set a
password, its individual command lines are still recallable one at a time.

## Multi-line configuration paste

Pasting a multi-line configuration block (e.g. copied from a `show`/`show
running-config` output) is supported in every configuration mode:

```
network-lab(config-access-info-test_lab)# device R2
 type iosxr
 address 192.0.2.20
 transport ssh
 port 22
 username example-user
 password Example!Password123
network-lab(config-access-device-R2)#
```

- Each physical line of the pasted block is executed in order through the
  exact same command grammar and handlers as manually typed input — there
  is no separate paste parser. A line that changes mode or candidate state
  (`device`/`jump-host`/`exit`/`root`/`end`/`clear`/`commit`/...) is
  reflected before the next line runs, so `clear` partway through a paste,
  or `device`/`exit` pairs that switch between objects, behave exactly as
  they would typed one at a time.
- Leading indentation (as produced by `show`/`show running-config`) is
  stripped from each line; internal spacing, punctuation, and special
  characters in a value (e.g. `!` in a password) are preserved exactly.
- Blank lines are ignored.
- A standalone `!` line (a visual block-closing separator in
  `show`/`show running-config` output) has a narrow, explicit meaning
  **only** inside a multi-line paste, and **only** in these five modes
  (access-info's three modes plus topology's two):

  | Current mode when the `!` line is reached | Effect |
  |---|---|
  | `config-access-device-<name>` | Exactly one level up, to `config-access-info-<definition>`. |
  | `config-access-jump-host-<name>` | Exactly one level up, to `config-access-info-<definition>`. |
  | `config-access-info-<definition>` | Exactly one level up, to `config` (global configuration). |
  | `config-device-<name>` (topology) | Exactly one level up, to `config-topology-<definition>`. |
  | `config-topology-<definition>` | Exactly one level up, to `config` (global configuration). |
  | `config-running` and the name-less `config-running-<kind>` submodes | Exactly one level up, but **only** if at least one command was executed inside that section (one that did not itself change mode); a leading `!`, a `!` right after entering the mode or right after `exit` never climbs another level. |
  | anywhere else (`config`, EXEC, scenario/reference modes, named running submodes, ...) | No-op: never `exit`/`end`/`quit`, never changes candidate state, never ends the paste, never terminates the CLI. |

  This is what makes a rendered access-info or topology block (device/
  jump-host blocks each closed by their own `!`, and the whole block closed
  by a final `!`) pasteable back without manually inserting `exit` between
  every sibling block. It is deliberately **not** a generic `!` = `exit`
  alias. Note that in topology a stray `!` right after `topology <name>` or
  `device <name>` now leaves that mode (it used to be a no-op), so the next
  mode-specific line (`type`, `description`, ...) fails with `% Unknown
  command` and the paste stops there (see the non-atomic paste rule below);
  a following global command would run at global level. An extra stray
  trailing `!` (an imperfect copy/paste) is always safe —
  once the paste reaches global configuration mode or EXEC, further `!`
  lines simply do nothing. A single manually typed `!` (not part of a
  multi-line paste) is unaffected by any of this, and so are scenario and
  reference modes (no CLI body grammar there, so `!` stays a no-op).
- Processing stops at the first invalid or failing line — commands from
  earlier lines remain in the candidate (no automatic rollback); use
  `clear` to discard them if the paste didn't go as intended.
- Pasting never commits automatically. If the pasted block itself contains
  `commit`, that line commits exactly as it would if typed manually.

## Line editing

Standard Emacs/IOS XR-style editing:

| Key | Effect |
|-----|--------|
| Left / Ctrl-B | Cursor left |
| Right / Ctrl-F | Cursor right |
| Ctrl-A | Cursor to beginning of line |
| Ctrl-E | Cursor to end of line |
| Backspace | Delete previous character |
| Ctrl-D (non-empty line) | Delete character at cursor |
| Ctrl-W | Delete previous word |
| Ctrl-U | Delete from cursor to beginning of line |
| Ctrl-K | Delete from cursor to end of line |

## Ctrl-C: cancel the input line only

Ctrl-C **never** terminates the CLI process and **never** discards candidate
configuration (it is not `clear`). It cancels only the currently-typed,
not-yet-submitted input line and redisplays a fresh prompt in the same mode:

```
network-lab(config-access-device-R1)# address 192.0.2.<Ctrl-C>
network-lab(config-access-device-R1)#
```

Everything committed to either candidate scope *before* that keystroke
(including edits made earlier in the same session) is untouched. Pressing
Ctrl-C repeatedly has the same effect each time — it is never treated as a
request to exit or to discard the candidate; use `exit`/`end`/`clear`/`quit`
or Ctrl-D for that.

## Ctrl-D / EOF

- **Non-empty input line**: deletes the character at the cursor (standard
  line editing, not EOF).
- **Empty input line**: requests to exit the whole CLI process, subject to
  the same uncommitted-changes guard as `exit`/`end`:
  - If `overall_dirty` is true (either candidate scope has uncommitted
    changes), the request is refused, a warning is printed, the candidate is
    left completely intact, and the CLI keeps running in the same mode.
  - If clean, the CLI process exits normally.

```
network-lab(config)# <Ctrl-D on empty line, with a pending scenario change>
% Uncommitted changes exist. Use 'commit' or 'clear'.
network-lab(config)#
```

## Candidate model

```
committed_settings   <- lab/settings.yaml, snapshotted on `configure`
settings_candidate    <- editable copy, mutated only from `running` mode

definition_kind       <- "topology" | "access_info" | "scenario" | "reference" | None
definition_name
definition_original   <- None for a brand-new definition, otherwise the loaded snapshot
definition_candidate  <- editable copy
```

Everything above is memory-only inside the CLI process. Nothing under
`lab/` is written until `commit` succeeds. At most one definition is open
at a time; the running-config candidate and the open definition candidate
are dirtied, switched, and cleared independently of each other.

### Candidate initialization

`configure` snapshots committed running-config into a settings candidate
only. No definition is automatically loaded — `show configuration` works
immediately (producing no output, since nothing has changed yet), and no
definition YAML is read or rewritten until a
`topology`/`access-info`/`scenario`/`reference <name>` command is issued.

### Scoped dirty state

- `settings_dirty` — the running-config candidate differs from the
  committed settings snapshot.
- `definition_dirty` — the open definition candidate differs from what was
  loaded from disk (or, for a brand-new definition, is always dirty until
  committed).
- `overall_dirty` — the logical OR of the two; this is what guards
  `exit`/`end`/Ctrl-D.

A running-config-only change (e.g. a pending `scenario` selection) does
**not** block opening or switching a definition, and vice versa. An
in-progress definition edit **does** block opening a *different* definition
until `commit` or `clear`:

```
network-lab(config-topology-lab_a)# description changed
network-lab(config-topology-lab_a)# exit
network-lab(config)# topology lab_b
% Uncommitted changes exist. Use 'commit' or 'clear' before switching.
```

A *clean* definition (loaded, but never semantically changed) can be freely
switched away from.

## `commit`

1. Validates the entire candidate: if a definition is open and dirty, it is
   validated with the matching SSOT validator for its kind
   (`lab.validate_topology_data()`, `validate_access_info_data()` —
   including any `jump_hosts`/device `jump_host` reference — or the
   minimal "valid YAML mapping" check for scenario/reference); the
   running-config selection's access-info (if any)/topology/scenario/every
   reference must exist on disk, or be the definition just created/edited
   in this same commit.
2. On any validation failure: **zero disk writes**, the candidate and
   current mode are retained unchanged, and every failure is printed as a
   `% ...` line.
3. On success, a created/edited definition file is written first, then
   `lab/settings.yaml` if the running-config candidate is dirty — in that
   order, since a running-config selection may reference a definition just
   created in this same commit. A **deletion** is written in the opposite
   order (`settings.yaml` first, then the file is deleted; deletability is
   checked before any write), so a failure in between can leave an
   unreferenced file but never a persisted selection of a deleted
   definition; retrying `commit` finishes the delete. Writes are atomic (temp file, flush,
   `os.replace()`); an unchanged definition that was only opened is never
   rewritten, and a fully clean commit ("No changes to commit.") writes
   nothing at all.
4. **`commit` always stays in the current mode** — success or a clean
   no-op, it never navigates. A newly-created object's mode remains valid
   and usable immediately after a successful commit (its candidate is no
   longer dirty, so `show configuration` there now shows nothing and `show
   running-config` shows what was just written). Use `root`/`exit`/`end` to
   move elsewhere.

## `clear`

Discards **every** uncommitted change in the current configure session —
the running-config candidate and the open definition candidate together —
restoring committed state. It never writes disk and does not return to
EXEC, and does not navigate at all beyond the nearest-valid-parent
fallback below:

- A brand-new (never-committed) definition candidate is discarded entirely,
  not reset to an empty version of itself.
- An existing definition's edits are reverted to the last-loaded (committed)
  snapshot.
- If the current submode's target no longer exists after the revert (e.g.
  you were editing a device just added to a topology, and `clear` reverted
  the topology to a state before that device existed, or you were inside a
  brand-new topology/scenario/reference/access-info/jump-host that just got
  discarded entirely), the CLI steps back to the nearest still-valid parent
  mode instead of lingering in a now-invalid submode:

  ```
  network-lab(config-scenario-new_scenario)# clear
  network-lab(config)#
  ```

`clear` is unrelated to Ctrl-C: Ctrl-C only cancels the current, not-yet-
submitted input line and never touches the candidate.

## `root` / `exit` / `end`

- **`root`** jumps straight to global configuration mode from *any* nested
  submode (a definition, or a device/jump-host submode nested inside one),
  in one step, preserving candidate state — never commits, never clears.
  Not offered at global configuration mode itself (already the
  configuration root) or in EXEC.
- **`exit`** moves exactly **one** configuration level up: a device/
  jump-host submode -> its parent definition mode; a definition mode
  (topology/access-info/scenario/reference) or `running` mode -> global
  configuration mode. No dirty guard at any of these levels (still the
  same overall configuration session). In global configuration mode,
  `exit` is guarded, equivalent to `end` (see below).
- **`end`** returns directly to EXEC from anywhere in configuration mode
  (any depth), guarded by `overall_dirty`: if either candidate scope
  (running-config or the open definition) has uncommitted changes, `end`
  is refused with a warning and the candidate is left completely intact.
  `end` is never an implicit commit or clear.

None of the three ever changes what `commit` means or does — navigation
and persistence are fully independent operations.

## `show running-config` vs. `show configuration`

Both commands are scoped to the **current CLI context**, not to a single
fixed meaning. `show` with no argument is a complete command in every
configuration mode (never in EXEC) and means exactly the same thing as
`show configuration` there — see "Bare `show`" below.

- **EXEC, global configuration, and `running` mode**: `show running-config`
  is the **committed MCP running-config selection** (access-info/topology/
  scenario/reference *names*, never a definition's own content) — this is
  the one meaning that predates definitions having their own candidates,
  and it never changes:

  ```
  network-lab# show running-config
  !
   access-info
    sample
  !
   topology
    sample
  !
   scenario
    sample
  !
   reference
    sample
  !
  network-lab#
  ```

  `show configuration` here is **that same selection's uncommitted changes
  only** — see "Uncommitted-changes-only `show configuration`" below — the
  pending running-config delta while in `running` mode, or, in global
  mode, that delta aggregated with the open definition's own delta if one
  is open in the background.

- **A definition mode** (`topology`/`access-info`/`scenario`/`reference`,
  or their nested device/jump-host submodes): both commands are scoped to
  *that one object* instead of the MCP selection —

  - `show running-config` — the *full* state currently **committed on
    disk** for this object, re-read fresh every time (never a stale
    in-memory copy). **Empty** for a definition (or sub-object) that has
    never been committed; reflects the object's state immediately after
    this session's own `commit`.
  - `show configuration` (and bare `show`) — **uncommitted changes only**,
    not a full dump of the candidate.

  ```
  network-lab(config)# access-info test
  network-lab(config-access-info-test)# device R1
  network-lab(config-access-device-R1)# type iosxr
  network-lab(config-access-device-R1)# address 192.0.2.11
  network-lab(config-access-device-R1)# exit
  network-lab(config-access-info-test)# show running-config
  network-lab(config-access-info-test)# show
  access-info test
   device R1
    type iosxr
    address 192.0.2.11
   !
  !
  network-lab(config-access-info-test)# commit
  Commit complete.
  network-lab(config-access-info-test)# show
  network-lab(config-access-info-test)#
  ```

  (`show running-config` printed nothing at all — "empty" means no output
  line, not a blank line — because no `access-info test` was committed
  yet; after `commit`, bare `show` prints nothing either, since there is no
  longer any uncommitted change.) A nested device/jump-host submode
  further scopes both commands to *that one sub-object* rather than every
  device/jump-host in the parent definition — a sub-object that only exists
  in the candidate (just added, not yet committed) makes `show
  running-config` empty even if the parent definition itself is already
  committed.

  There is deliberately no `show running-config configuration` or other
  combined command — the two stay separate, just each newly aware of
  context.

- **`show version` exists only in EXEC mode.** It is software-level
  information, not part of any configuration context, so `show ?` never
  lists it and Tab never completes it outside EXEC; typing it explicitly
  elsewhere hits the ordinary invalid-command/caret error, like any other
  keyword that does not exist at that grammar position — there is no
  special-cased error just for this command. See "`show version`" above.

Explicit local rendering shows device/jump-host passwords in **clear
text** (see "Password display policy" below) and displays every identifier
using its exact stored/candidate case, in both the committed and
uncommitted-delta views.

### Bare `show`

Every configuration mode (never EXEC) accepts bare `show` as a complete
command, meaning exactly `show configuration`:

```
network-lab(config-access-device-R1)# show ?
  configuration       Contents of uncommitted device configuration
  running-config      Contents of committed device configuration
  <cr>
network-lab(config-access-device-R1)# show
```

EXEC's `show` never gains a `<cr>` or becomes executable on its own —
`show` there is always followed by `running-config` or `version`.

### Uncommitted-changes-only `show configuration`

`show configuration` (and bare `show`) show **only what changed**, not a
full dump of the candidate — a small, bounded, pragmatic delta, not a
mathematically minimal generic recursive diff:

- **Structured scalar configuration** (running-config selections,
  access-info device/jump-host fields, a topology device's `type`):
  field-level. Only the fields that actually differ from committed are
  shown, as `field value` (set/changed) or `no field` (cleared) — unchanged
  fields are never repeated:

  ```
  # committed: type iosxr / address 192.0.2.11 / port 22
  # candidate change: port only
  network-lab(config-access-device-R1)# show
  access-info sample_lab
   device R1
    port 2222
   !
  !
  ```

  A brand-new object (never committed) is diffed against nothing, so every
  field you set on it shows up — entering a new, still-empty object alone
  produces no output at all (nothing has actually changed yet).

- In **global** `show configuration`, the whole running-config selection
  delta (switches, selections, removals) is grouped under a `running-config`
  section so a removal cannot be mistaken for the top-level `no reference
  <name>` stored-definition deletion and a `topology`/`scenario` block is never
  read at global level; inside `config-running` the same delta is shown
  ungrouped. The section is closed by `!` when its last item is a command line
  (` no reference <name>`), or by `exit` when it ends with a block (a `!`
  right after a block's own closing `!` is a no-op in the structural-`!`
  model), so a following global line such as the definition-deleting `no
  reference <name>` always runs at global level:

  ```
  network-lab(config)# show configuration
  running-config
   topology
    no sample_lab
   !
   no reference sample
  !
  no reference sample
  ```

  In `config-running` a block's leading `!` is omitted when it follows a
  command line (e.g. `no access-info`), since that `!` would otherwise close
  `config-running` on paste.

### Replay guarantees (`show configuration` output pasted back)

Replay means: paste the output into a Fresh Session on the same committed
baseline, in the context it was taken from; candidate, dirty state, reference
order and the final mode then match the source session, and the committed
state is untouched. Net-zero changes print nothing and replay as clean.

| Context | Replay | Notes |
|---|---|---|
| Global | Guaranteed | Running-selection delta in a `running-config` section (see above), followed by any open definition's delta. |
| `config-running` | Guaranteed | The selection delta, e.g. `!`/` topology`/`  no <name>`/`!`. |
| `config-running-<kind>` (name-less Common) | Guaranteed (kind-scoped) | Shows **only that kind's** selection delta in the submode's own grammar: `<name>` (select/switch) and `no <name>` (unset, using the committed name); a reference shows `no <name>` lines then added names in candidate order. No `!`, no `description`, no other kind, no definition body. Replays into the same Common submode of a Fresh Session and stays there. A definition named like a submode keyword (e.g. `show`) cannot be typed as `<name>` there, so it is rendered as a `% ... collides with a submode keyword` line that stops a paste (`no show` is always safe). |
| `config-running-<kind>-<name>` (named) | Existing scope | Unchanged: shows the whole running delta, as before. |
| Description edits (Global / `config-running`) | Guaranteed | Rendered through the named submode: `<kind> <name>` / ` description <text>` (or ` no description`) / ` exit`. The entry command selects it, so it also carries a selection change; plain selections without a description keep the `!`/`<kind>`/`<name>`/`!` block. Not shown in Common submodes. |
| Definition sections (Global) | access-info, topology: Guaranteed | `!` closes `device`/`jump-host` blocks and the definition (topology's `device` and `topology` modes now close with `!` like access-info's), so the replay returns to global and a following global command (e.g. `no reference <name>`) is not read inside the definition mode. |
| scenario / reference definition body | Display-only | These modes have no CLI body grammar (only external `edit`); their delta is YAML review text. A paste stops at the first unsupported line (`% Unknown command: "name:"`) and later lines are not processed. Candidate changes applied by earlier lines of the same paste (e.g. the `running-config` section of a Global output) are **kept**; the committed state is never changed. |
| topology `links` / device `interface` body | Display-only | `interface <name>` blocks (inside `device`) and the `links` block are review text: a paste stops at `interface` (device mode) or `links` (topology mode) with `% Unknown command`, the mode stays open, and earlier lines of the same paste stay applied in the candidate. Device `type`/`description` edits replay. |

**Pastes are not atomic.** Processing stops at the first failing line; earlier
lines stay applied to the candidate and later lines are never run. There is no
automatic rollback: use `clear` to discard the candidate. The committed state
is never touched by a paste. A Common-submode description-only change shows no
output there (descriptions are not shown in Common submodes) although the
session is dirty.

Commit validation is unchanged: an unselected topology/scenario still fails
`commit`. Running-selection removal (`no <name>` in a Common submode, or the
`running-config` section in Global) is distinct from the definition deletion
`no <kind> <name>` at global level, which Global output prints after the section.

- **Structured additions/removals** use the same set/`no` rendering, e.g.
  a newly-added `reference` shows as an addition, `no access-info` shows as
  `no access-info`, `no reference sr_mpls` shows as that line. A whole
  access-info device/jump-host removed via `no device <name>` / `no
  jump-host <name>` shows the same way, as a single ` no device <name>` /
  ` no jump-host <name>` line (not the full block a *modified* object
  gets) — e.g. removing a committed `R4` shows `no device R4`, while a
  device that never existed in committed state and was removed again in
  the same session (or removed and then recreated identically) shows no
  diff at all, since the net effect against committed is nothing. A
  topology device's `interfaces` follows the same set/`no` shape at the
  interface level: an added or changed interface shows its changed fields
  (`ipv4_address`/`vrf`), a removed interface shows `no interface <name>`,
  and a device with only an L3-only change (its own `type` unchanged)
  still appears in the diff rather than disappearing.

- **Open/complex structures** without a direct CLI representation below
  the whole-document level (scenario/reference content — schema
  intentionally not fixed — or a topology field like `links` that the
  structured CLI never edits) fall back to showing the smallest practical
  enclosing changed object as a whole (the entire scenario/reference
  document, or the entire topology block) rather than attempting an exact
  leaf-level diff. This may include unchanged sibling fields inside that
  object; it never shows an unrelated, unchanged definition.

- **Global configuration mode**'s `show`/`show configuration` aggregates
  the running-config candidate's own delta with the open definition's
  delta (if one is open), each rendered by its own type-aware renderer —
  not merged into one cross-definition minimal diff.

`commit` clears the displayed delta for whatever it wrote (there is
nothing left to show once the candidate matches the newly-committed data);
`clear` clears it by discarding the candidate back to committed state
instead.

## Password safety

Device and jump-host `password` values (entered in access-info's device/
jump-host submodes) are stored in plain text in access-info YAML (this is a
lab tool, not a secret manager). A password is never offered as a Tab/`?`
candidate or value, and never retained in the CLI's in-memory command
history — see "Command history" and "Tab / Ctrl-I completion" above.
Topology never contains `password` at all (rejected by
`lab.validate_topology_data()`), and Network Lab MCP does not intentionally
put one in an MCP tool response, error message, or application-side log
line — see "Password display policy" below for the one deliberate exception
(explicit local CLI display).

### Password display policy

`show running-config` / `show configuration` / bare `show`, when rendering
an access-info device or jump host, show `password` in **clear text**:

```
network-lab(config-access-device-R1)# show running-config
access-info test_lab
 device R1
  type iosxr
  address 192.0.2.11
  transport ssh
  port 22
  username cisco
  password cisco
 !
!
```

This is the **only** place a password is ever shown in clear text by
Network Lab MCP. Network Lab MCP does not intentionally place it in an MCP
tool result, an application-side log line, an exception, a `%`-prefixed error
message (including the access-info-not-found/type-mismatch errors elsewhere
in this document), `?` help, or Tab completion, and it is never retained in
command history — those protections are unchanged and unweakened by this
policy.

This does not sanitize terminal content. Terminal logs are raw transcripts of
the remote session: device output is not redacted, anything the remote side
prints or echoes can appear in them, and they are not guaranteed to be free
of secrets. See [SECURITY.md](../SECURITY.md).

## External YAML editor

`edit` (available in topology/scenario/reference definition mode) opens the
**candidate** — never the committed file — in an external editor:

```
committed definition -> candidate data -> secure temp .yaml file
    -> external editor -> editor exits -> read temp file -> parse -> validate
    -> candidate updated
```

- **Resolution order**: `$VISUAL`, then `$EDITOR` (each split with
  `shlex.split()` since it may embed arguments; `shell=True` is never
  used), then a `vim` fallback. Network Lab MCP only adds the decorative
  `-c "syntax on" -c "set filetype=yaml"` options when it chose `vim`
  itself — never when the operator explicitly set `$VISUAL`/`$EDITOR` to
  `vim`.
- **Vim paste mode**: whichever editor is resolved, if its executable is
  positively confirmed to be real Vim (`vim`/`vim.basic`/`vim.tiny`, or a
  `vi` that resolves — via `shutil.which()` + `os.path.realpath()`, no
  subprocess probe — to one of those through a symlink/alternatives chain),
  Network Lab MCP appends `-c "set paste"` so pasting multi-line YAML into
  the editor never triggers Vim's autoindent-amplification on every
  newline. This applies even when the operator chose `vim` explicitly via
  `$VISUAL`/`$EDITOR` (unlike the decorative syntax/filetype options
  above), coexists with any `-c` option the operator already supplied, and
  is scoped to that one editor invocation only — no vimrc is touched.
  Neovim (`nvim`) and any `vi` that can't be positively confirmed as real
  Vim are left completely alone.
- **Temp file**: a securely created, uniquely named `.yaml` file (never a
  predictable fixed name), removed after the editor exits — on every path,
  including an error.
- **On a non-zero editor exit**: the candidate and committed file are both
  left untouched; a clear `%` error is printed and the CLI keeps running.
- **On invalid YAML**: same as above, with a safe line/column-only message
  (`% YAML validation failed at line 8, column 4. Candidate was not
  updated.`) — the raw editor content is never dumped into the error.
- **`:wq` in the editor does not commit**: the candidate is only updated in
  memory; `commit` is still required to persist it to the real YAML file.
- **No semantic change, no dirty flag**: opening and saving without
  changing anything does not mark the candidate dirty (the same
  `definition_candidate == definition_original` equality check `clear`/
  `commit` already use).

### Why access-info has no external editor

access-info's `password` field must never be offered as a Tab/`?`
completion candidate and should not be casually written to an arbitrary
external editor's temporary file; an editor round trip would put the
plaintext value in a file outside the structured, retained-in-nothing
path this CLI otherwise guarantees for it. Structured CLI editing keeps
password entry on that one controlled path.

## Limitations

- No structured `link`/`interfaces` editing command; `links` and a
  device's `interfaces` are only ever set by `discover topology` or the
  external `edit` -- `show`/`show configuration`/`show running-config`
  display them as read-only review information, not as re-typeable
  command lines (see "Topology device mode commands" above). Any other
  field the CLI does not directly edit is likewise preserved untouched
  through a commit.
- `discover topology [l3]` supports IOS XR/IOS XE (LLDP + CDP) and classic IOS
  (CDP only) — see README.md's ["Supported device types & Discovery
  scope"](../README.md#supported-device-types--discovery-scope) for the
  full scope and limitations; no NX-OS discovery, no automatic commit or
  `active_topology` selection, and no discovery-history command.
- `show logging` only lists/reads existing terminal logs; there is no
  `clear logging`/rotation/search CLI beyond `delete logging`.
- Scenario and reference content has a schema that is intentionally not
  fixed (see [scenario_format.md](scenario_format.md)) — only "valid
  YAML, root is a mapping" is enforced.
- `lab/principles.yaml` is not edited by the CLI.
- The case-only collision safeguard is implemented for `topology <name>`
  only; `device <name>` (under either topology or access-info) and
  access-info/scenario/reference names have no equivalent safeguard
  (identifiers remain fully case-sensitive, but a near-duplicate by case is
  not flagged).
- access-info has no external-editor support (see above).
- Single-hop OpenSSH ProxyJump only: a jump host cannot itself reference
  another jump host (no jump chains), and only `type: host` /
  `transport: ssh` jump hosts are accepted — see README.md's
  ["Single-hop SSH jump hosts"](../README.md#single-hop-ssh-jump-hosts-proxyjump).
- Candidate configuration is memory-only: it is not recoverable after an
  abrupt process termination (SIGKILL, crash, host failure). Normal exit
  paths (`root`, `exit`, `end`, `clear`, `quit`, Ctrl-D) never lose it
  unexpectedly.
