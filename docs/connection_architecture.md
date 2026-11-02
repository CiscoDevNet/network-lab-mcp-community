# Connection Architecture

## Purpose

Network Lab MCP provides a lightweight **AI network engineering layer** for real network laboratories. It lets an MCP-capable AI client work with established device command-line interfaces while keeping device connection details inside the local Network Lab MCP process.

**The project does not implement its own SSH protocol, Telnet protocol, or terminal emulator.** Instead, it composes proven operating-system tools and adds the device-resolution, session-control, observability, and engineering-context functions needed by an AI agent.

This document explains **how and why those pieces fit together**. For the overall configuration and MCP architecture, see [Architecture](architecture.md); for human commands, see [CLI reference](cli_reference.md); and for the external tool contract, see [MCP tools](mcp_tools.md).

> **Verification status:** The behaviour described here is implemented in the source and covered by automated tests, including tests against isolated loopback OpenSSH servers (OpenSSH 10.3 on macOS) and fakes for jump-server password prompts. The earlier macOS real-lab checks (managed terminals and Discovery through an SSH jump server) predate the host-key management and the per-hop `ProxyCommand` described below; **host-key registration, replacement and the strict ProxyCommand path have not been validated against real routers or a real jump server**, nor on Linux or other OpenSSH versions. See [SECURITY.md](../SECURITY.md) for security boundaries. No unrestricted AI autonomy or production-network safety is claimed.

## Design philosophy: compose, do not reinvent

The architecture follows a practical Unix-style principle: use a mature tool for each established responsibility, and build only the coordination layer that the application needs.

| Component | Primary responsibility | What Network Lab MCP does not recreate |
|---|---|---|
| **OpenSSH client** | Encrypted remote connections, SSH authentication, host-key verification, jump connectivity and local port forwarding | An SSH transport or cryptographic implementation |
| **tmux** | Long-lived pseudoterminal sessions, pane lifetime, output capture and independent observation | A custom terminal emulator or session daemon |
| **Telnet client** | Telnet connectivity for legacy lab equipment where required | A custom Telnet protocol stack |
| **Network Lab MCP** | Resolves logical devices, selects private access data, orchestrates connection processes, controls terminal I/O, maintains isolation boundaries and provides lab context | The network-engineering reasoning model |
| **AI client** | Plans, investigates, interprets output and decides which commands to request | Device transport and session infrastructure |
| **Human engineer** | Defines objectives, controls access and managed definitions, reviews evidence and takes responsibility for operational decisions | — |

Composing existing tools reduces protocol implementation work and allows the project to benefit from upstream fixes. It does **not** remove complexity: subprocess arguments, authentication prompts, host-key policy, terminal ownership, failure handling and OS-specific behavior remain important integration concerns.

## Architecture at a glance

```text
                 Human engineer
          goals, review, CLI commit
                        |
                        v
              MCP-capable AI client
             reasoning and decisions
                        |
                seven MCP tools
                        |
                        v
                Network Lab MCP
        +-----------------------------+
        | Logical device resolution   |
        | Private Access-info lookup  |
        | Topology / Scenario / Ref.  |
        | Managed terminal lifecycle  |
        | Discovery and observations  |
        +-----------------------------+
                        |
                Dedicated tmux
              socket and sessions
                        |
             +----------+----------+
             |                     |
          OpenSSH               Telnet client
             |                     |
       SSH device / jump       Legacy lab device
             |
       Optional SSH tunnel
```

The diagram is conceptual. SSH and Telnet are executed as local client processes within managed terminal workflows; the precise process topology depends on the connection method.

The AI operates through the MCP interface, **not** by receiving an SSH password, private `access-info`, or unrestricted access to the host's tmux server.

## Responsibility boundaries

### AI client versus Network Lab MCP

The AI client supplies reasoning: which command to run, which output matters, what the results imply, and what to investigate next. Network Lab MCP supplies **bounded access and engineering context**, not an independent reasoning engine.

`get_active_topology()` and `get_execution_instructions()` expose selected lab context. The terminal tools provide an operational interface to devices by logical name. Private access information is resolved internally rather than returned through MCP.

The project intentionally exposes a small, stable set of **seven MCP tools**, documented in [MCP tools](mcp_tools.md). It does not need a new MCP tool for every network protocol or device feature: the AI can interact with existing device CLIs through a terminal session.

### Human CLI versus MCP

The human CLI manages lab definitions and running selections using the candidate/commit workflow. A discovered topology is a **candidate for review**, not an automatic change to the active topology.

The human CLI and MCP interface have different authority boundaries. For example, interactive SSH host-key approval and recovery, where implemented, belong to the designated human CLI workflow and must not become an implicit MCP permission.

**Important:** candidate/commit controls Network Lab MCP's *managed definitions*. It does not automatically prevent the AI from sending configuration commands to a real device through a permitted terminal. AI-client permissions, authorization and lab isolation remain necessary.

## Why tmux?

tmux is more than a way to display terminal output. It provides a stable operating-system-managed session boundary:

- A device session can outlive an individual MCP tool call, allowing read–decide–send workflows.
- The MCP server can inspect existing sessions after reconnecting or restarting instead of maintaining a separate terminal-session database.
- Terminal output can be observed by the engineer independently of the AI client's reasoning interface.
- Separate session namespaces help distinguish managed terminal sessions, Discovery sessions and internal validation sessions.
- Pane identity and liveness checks can prevent output being read from, or input being sent to, the wrong process after disconnection.

Network Lab MCP uses a **dedicated tmux socket** so its managed sessions do not share the operator's ordinary tmux namespace.

Conceptual lifecycle:

```text
terminal_open(device-id)
    -> resolve device from committed topology and private Access-info
    -> validate transport and connection policy
    -> create or locate owned tmux session
    -> start local ssh/telnet client in a pane

terminal_read(device-id)
    -> verify managed session and pane
    -> capture observable pane output

terminal_send(device-id, text)
    -> verify permitted device, session and pane
    -> inject text into the managed terminal

terminal_close(device-id)
    -> close the owned managed session
```

This flow is illustrative, not a substitute for the exact error and lifecycle contracts in [Architecture](architecture.md) and [MCP tools](mcp_tools.md).

### Managed session versus a live process

A tmux session can exist even when its original SSH process has exited. Session existence alone therefore does not prove connectivity. Liveness checks and explicit rejection of unsafe fallback states are necessary, especially before sending input. The goal is to avoid accidentally delivering an AI-issued device command to a local shell or a replacement process.

### Live observation and transcripts

A human can use the CLI's read-only terminal-monitor functionality to follow managed terminal activity. Logs can help reconstruct what the AI saw and did. **Raw terminal output is not guaranteed to be redacted**: devices may echo passwords or disclose sensitive information in commands or responses. Logs require the same care as other operational evidence.

## Connection paths

### 1. Direct SSH

```text
Local workstation
  Network Lab MCP
      |
  tmux pane
      |
  OpenSSH client ===== encrypted SSH =====> Router1
```

OpenSSH handles transport encryption, host-key verification and user authentication. Network Lab MCP handles logical device lookup, command construction, session ownership, input/output and error handling.

### 2. SSH device through a jump server

```text
Local workstation                         Lab network
  Network Lab MCP
      |
  tmux / OpenSSH ---- SSH ---> jump1 ---- SSH transport ---> Router1
                        jump hop                 target hop
```

A single SSH jump server allows the workstation to reach an SSH device that is not directly accessible. The **jump server and target are separate SSH host-key trust and authentication boundaries**. An option applied to the target SSH client must not be assumed to apply automatically to the jump client's SSH process.

OpenSSH's `ProxyJump` (`-J`) child process does **not** inherit a command-line `-o StrictHostKeyChecking=yes` (observed with OpenSSH 10.3: an unknown jump-server key still produced a `yes/no` prompt). Network Lab MCP therefore does not use `-J` for managed terminals or Discovery. It starts the target with an explicit child command:

```text
ssh -o StrictHostKeyChecking=yes -o UpdateHostKeys=no \
    -o ProxyCommand="ssh -o StrictHostKeyChecking=yes -o UpdateHostKeys=no -W %h:%p -p <jump-port> <jump-user>@<jump-host>" \
    -p <port> <user>@<target>
```

Both hops are verified strictly, the arguments are a list (never a shell string supplied by the user; the child command is quoted element by element), and no credential appears on any command line. Jump-server password prompts are answered through the same attribution logic as before (user and host must match exactly one hop; at most one send per hop; a re-prompt stops the connection).

The jump server runs its SSH service; it does **not** need Network Lab MCP installed.

### 3. Legacy Telnet-only device via SSH local forwarding

```text
Local macOS workstation                         Lab network

Network Lab MCP
     |
  tmux session
     |
 Local Telnet client ---> 127.0.0.1:<local-port>
                                    |
                            SSH local forward
                                    |
                               jump1 (sshd)
                                    |
                         Telnet TCP, plaintext
                                    |
                               LegacyRouter
```

This is **not SSH ProxyJump to the legacy device**. The workstation establishes an SSH local port forward through `jump1`; a **local** Telnet client then connects to the forwarded loopback port. The SSH jump server only needs SSH forwarding capability; it does **not** need a Telnet client or Network Lab MCP installed.

Operational advantages:

- The user can run Network Lab MCP on the local workstation without deploying it on the intermediate server.
- Existing legacy Telnet-only equipment remains accessible without developing a custom Telnet protocol stack.
- Access-info can describe the selected device and its intermediate connection path without exposing those private details to the AI client.

**Security boundary:** the local-workstation-to-jump-server segment is carried through SSH. The **jump-server-to-legacy-device segment is Telnet and remains plaintext**. Use this only in appropriately controlled lab networks, and do not imply end-to-end encryption.

For this path the SSH tunnel runs with `StrictHostKeyChecking=yes`. Host-key **registration and replacement are disabled**: the jump server's key must already be registered (for example with a manual `ssh` connection), otherwise the connection is refused with a hint. Telnet itself has no host-key verification.

### 4. Direct Telnet

```text
Local workstation -> local Telnet client -> LegacyRouter
```

Direct Telnet does not provide SSH host-key verification or encrypted Telnet traffic. It is a lab compatibility path, not a recommended transport for sensitive or production networks.

## Connection resolution and private data

A tool invocation names a **logical device**, not an IP address with credentials. Network Lab MCP checks the selected topology, resolves the selected private Access-info definition, and builds a transport-specific connection. The AI does not receive Access-info through the MCP tool contract.

The separation between `access-info`, `topology`, `scenario` and `reference` is deliberate:

| Definition | Connection architecture role |
|---|---|
| `access-info` | Private endpoint, transport, username and authentication parameters |
| `topology` | Logical device identity and network relationships |
| `scenario` | Current engineering objectives and verification procedure |
| `reference` | Reusable engineering knowledge and platform guidance |

These definitions are selected by the human-managed running configuration. See [Architecture](architecture.md) for candidate/commit, file structure and reload semantics.

## SSH host-key trust and human approval

SSH host-key verification is not the same as user authentication. It verifies the **server identity** before the client trusts the connection for authentication.

```text
Known host key matches            -> strict verification, no question asked
Unknown key, human CLI            -> operator approves expected key type + SHA256 fingerprint
                                     -> short registration connection (authentication disabled)
                                     -> Discovery connections run strict on every hop
Changed key, human CLI            -> connection stops; operator may review replacement
                                     (verify on a copy, backup, remove one host, re-register)
Unknown or changed key, anything
else (non-interactive Discovery,
managed terminal / MCP)           -> connection refused; nothing is registered or replaced
```

- **Registration connection.** After approval, a short-lived ssh connection with `StrictHostKeyChecking=ask`, `HostKeyAlgorithms` limited to the approved key type and every authentication method disabled on the hop under test lets OpenSSH itself write the key. The approved fingerprint is typed at most once, only if host, port, algorithm and fingerprint of the prompt all match. Afterwards `ssh-keygen -F` must show exactly the approved fingerprint. For a target behind a jump server the jump hop must already be trusted. The Discovery connections that carry credentials are separate and always strict.
- **Replacement.** The old entry is removed only after the new key was verified on a throw-away copy of `known_hosts`, the operator confirmed, and a backup was written. A concurrent change of `known_hosts` stops the operation; the backup is never written back wholesale.
- **Stops without recovery.** A key type that differs from the approved one, certificate-authority or revoked markers, unreadable `known_hosts`, unsupported entries and shared host patterns all stop the operation and leave `known_hosts` unchanged; recover manually (`ssh-keygen -R`).
- **Boundaries.** Approval and replacement exist only in the human CLI (`discover topology`) and are not importable from the MCP server or terminal module (tests enforce this). A TTY does not prove a human is typing; see [SECURITY.md](../SECURITY.md).

## Authentication

- Credentials stay inside Network Lab MCP. They are typed into the pane through a private input path (never `terminal_send`, never a command line) and only after the hop's host key is verified.
- SSH: the target password prompt is answered once when it is attributable to the target. With a jump server the jump prompt is attributed first by exact user and host; an ambiguous or re-appearing prompt stops the connection. Key or agent authentication needs no automation.
- Telnet: username and password prompts are answered once each.
- Registration connections may send only a jump-server password (never target credentials), with a bounded budget and a stop after the first failure per jump server.

## Failure handling and ownership

Integration safety is a first-class concern because process-based tools can fail independently.

| Failure | Design response |
|---|---|
| SSH disconnects but tmux remains | Detect dead or invalid pane before further device input; never fall back to a local shell |
| Jump server or target cannot be authenticated | Stop the affected connection and report a bounded, sanitized failure |
| Host-key mismatch | Do not silently replace the key; require the appropriate separate recovery workflow |
| Connection command or hop cannot be validated | Fail closed instead of guessing a replacement command |
| Discovery fails midway | Preserve committed definitions and avoid publishing a partial candidate |
| Temporary session exits | Clean up only owned sessions, files and subprocesses |
| Connection output contains sensitive text | Treat transcripts as potentially sensitive; avoid claiming automatic redaction |

OS-process isolation, tmux ownership checks and SSH policy validation serve different purposes; one cannot substitute for the others.

## Cleanup

Network Lab MCP closes only sessions it owns: managed device sessions on `terminal_close`, Discovery bootstrap sessions when each device finishes, and tunnel sessions with the device that uses them. Orphaned tunnel sessions from an earlier run are neither reused nor deleted automatically. A pane whose process ended is retained for reading but never accepts input.

## Portability and trade-offs

The design is naturally portable across Unix-like environments because it relies on standard command-line tools, but its behavior is not automatically identical everywhere.

- **OpenSSH variants:** prompt text, supported options, host-key algorithms and jump-child option propagation can differ. Host-key workflows require version-aware testing.
- **tmux variants:** session lifecycle, process spawning, hooks and config-file behavior need controlled testing and clear ownership boundaries.
- **Operating systems:** client availability, command paths, TTY behavior and sandbox facilities differ between Linux and macOS.
- **Telnet:** client availability varies by platform; Telnet itself is unencrypted.
- **User SSH configuration:** custom host aliases, key files and host-key policies may conflict with the project's strict trust-handling rules. The application should fail safely rather than silently weakening verification.
- **External dependencies:** upstream tools must be installed and kept current; the project does not bundle its own SSH protocol implementation.

## Why this design matters

This approach keeps Network Lab MCP focused on what it uniquely contributes:

1. Turning a **logical network lab** into a usable AI-accessible workspace.
2. Preserving the distinction between **AI reasoning**, **transport/session execution**, and **human authority**.
3. Providing observable, reusable command-line access to equipment that engineers already know how to operate.
4. Supporting legacy devices without replacing established system tools.
5. Capturing sufficient operational evidence for later troubleshooting, review and knowledge reuse.

In short: **built on proven tools, designed for AI-driven networking**. Network Lab MCP adds a lightweight engineering coordination layer above OpenSSH, tmux and Telnet rather than reimplementing them.

## Related documentation

- [Overall architecture](architecture.md)
- [MCP tool contract](mcp_tools.md)
- [Human CLI reference](cli_reference.md)
- [Scenario and Reference format](scenario_format.md)
- [Security policy](../SECURITY.md)
- [Project README](../README.md)
