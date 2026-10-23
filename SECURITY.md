# Security Policy

This document describes the security boundaries Network Lab MCP implements,
the risks it does **not** remove, and how to report a vulnerability. It
describes current behavior only; where something is not enforced by the
software, it says so.

## 1. Scope and intended use

Network Lab MCP is intended for **controlled network lab environments**. It
lets an AI client (through MCP) operate real device terminals, and it gives a
human operator a separate CLI to manage lab definitions and watch that
activity.

- It is **not** a fixed, read-only network API wrapper. An AI client can send
  arbitrary input to a remote device CLI.
- It is **not** presented as a production network automation platform and is
  not designed or reviewed for production use.
- The human operator is responsible for the environment, access policy, the
  accounts used, which commands are acceptable, and final engineering
  decisions.

## 2. Trust model

```text
Human operator ── ./run_cli.sh ──► lab definitions, selection, private access-info
                                   (candidate -> commit)
AI / MCP client ── stdio MCP ──► Network Lab MCP server ──► tmux session ──► ssh/telnet ──► [optional jump host] ──► device
                                   │                                         │
                                   └── reads private access-info              └── transcript log (logs/terminal/)
```

- The **AI / MCP client** works with *logical device identifiers* (for example
  `R1`). It does not supply addresses, usernames, or passwords.
- The **Network Lab MCP server** resolves a logical device to its private
  access information on the local machine and starts the connection.
- The **human operator** and the MCP server run as the same local OS user and
  share local files. There is no separate privilege boundary between them.
- **Remote devices** and any **jump host** are outside Network Lab MCP's
  control. Everything they print is returned to the AI as terminal output.

Everything in this document assumes the local machine and its user account
are trusted.

## 3. Credential handling

Access information (address, transport, port, username, password, optional
jump host) lives in `lab/access-info/*.yaml`. Only the definition selected as
`active_access_info` in the local running configuration is used.

What the implementation does:

- No MCP tool returns access-info contents. `get_active_topology` returns
  topology data (device names, types, links), and `terminal_open` accepts only
  a logical device name.
- Device resolution fails closed with an error when: the device is not in the
  active topology; no access-info is selected; the selected access-info does
  not exist; the device is not in the selected access-info (other access-info
  files are never searched); the device type differs between topology and
  access-info; or the device references an unknown jump host. Malformed
  access-info is rejected by validation.
- Passwords are typed into the terminal through a tmux buffer, not through a
  subprocess command line, and are not placed in tool results or error
  messages. This is covered by tests.
- An authentication prompt is answered automatically only when it can be
  attributed to the target device. A prompt that cannot be attributed (for
  example, one that might belong to a jump host) is not answered with the
  target's credential; the attempt fails closed.
- Only one password attempt is made per `terminal_open` call.

What it does not do:

- It does not encrypt `lab/access-info/*.yaml`. Passwords are stored in plain
  text in those local files.
- The human CLI shows access-info passwords in clear text when you display
  that configuration (for example with `show running-config`). Treat that
  terminal output as sensitive.

Never commit real access-info. `.gitignore` excludes private lab files and
tracks only the fictional `sample_lab` definitions, which use documentation
addresses (`192.0.2.0/24`) and placeholder credentials that are not real. The
ignore rules reduce the risk of a mistake; they are not a security boundary,
so review changes before pushing.

## 4. Credential isolation is not output sanitization

The boundary above protects Network Lab MCP's *private access credentials*. It
does **not** make the content of terminal output safe.

- Network Lab MCP does not inspect or redact what a remote device prints.
- Output returned by `terminal_read` can contain anything the device shows
  to the logged-in account: for example passwords or hashed/encrypted secrets
  in configuration output, SNMP communities, keys, tokens, certificates,
  addresses, topology, customer information, and operational state.
- Commands such as `show running-config` can expose such data to the AI client
  and to the AI provider's service behind it.

"Credentials are not passed to the AI" must not be read as "no secret can
appear in MCP output". Decide which commands and data you are willing to expose
to the AI client before you use it, and use accounts and lab data accordingly.

## 5. MCP boundary

The server exposes exactly seven tools:
`get_active_topology`, `get_execution_instructions`, `terminal_open`,
`terminal_send`, `terminal_read`, `terminal_list`, `terminal_close`.

A small tool surface makes the interface easier to audit; it does not limit
what a remote CLI can do once a terminal is open.

- **Context exposed to the AI:** the active topology; the operating
  principles, active Scenario and Reference content; and name/description
  catalogs of stored Scenarios and References (with read-only inspection of
  chosen ones). Local lab files are read from disk on each call.
- **Terminal tools:** open, send input to, read, list, and close managed
  terminal sessions for devices in the active topology. `terminal_send`,
  `terminal_read` and related calls refuse devices not in the active topology.
- **Not returned:** access-info contents.
- **Returned and potentially sensitive:** terminal output (section 4).
- **What the AI cannot change through MCP:** lab definitions and the running
  configuration selection. No MCP tool writes them. The AI can *propose*
  Scenario/Reference content in its response; a human reviews and commits it
  in the CLI.
- `terminal_send` input is delivered to the session as given. It is not
  written to Network Lab MCP's own logs, but it appears in the session
  transcript if the remote side echoes it (section 8).

## 6. Terminal access and command risk

- The AI can send any text or keys to a device terminal. Network Lab MCP has no
  command allow-list or deny-list and does not classify commands as safe or
  unsafe.
- What can actually be done depends on the privileges of the remote account
  and the device CLI. Network Lab MCP cannot make arbitrary remote commands
  safe.
- An AI client can misjudge a situation or choose an inappropriate command,
  including configuration changes, reloads, or commands that expose secrets.
- Sending a command through Network Lab MCP adds no transaction, rollback, or
  confirmation semantics on the remote device.
- Use dedicated lab accounts with the least privilege that is practical, and
  review the active Scenario and References, since they steer AI behavior.

**Monitoring is not authorization.** See the next section.

## 7. Human terminal monitoring

`terminal monitor <device>` in the CLI streams the managed terminal activity of
a device into the operator's terminal.

- It is read-only: it observes the tmux session (existence check, pane query,
  capture) and does not send keystrokes to it. Other keys are not bound to
  anything, and `q` or Ctrl-C exits. Monitoring does not modify the session.
- It is optional and shows activity only while it is running. There is no
  requirement that anyone is watching.
- It does **not** approve, block, or delay commands. Every command the AI
  sends is executed without per-command human approval.

Human observability is not human authorization.

## 8. Terminal logging

Each managed terminal session (and each Discovery bootstrap session) is
recorded with tmux `pipe-pane` into
`logs/terminal/<device-id>/<timestamp>.log` under the repository directory.
A log is created when a session is created and holds the raw terminal output
of that session.

- Logs are a raw transcript. They can contain device output (including
  anything described in section 4), banners, prompts, and anything the remote
  side echoes back, such as typed commands.
- Network Lab MCP does not sanitize logs. Do not describe them as redacted,
  secret-free, or safe to share or commit.
- Password entry for SSH password prompts and Telnet password prompts is
  normally not echoed by those clients, so the password is not expected in the
  transcript. This depends on the remote side behaving that way and is not
  something Network Lab MCP guarantees. A Telnet username is echoed by the
  device and can appear in the log.
- Network Lab MCP does not set special file permissions on log files and does
  not rotate or expire them. Files use your default umask and are not
  encrypted. Retention, access control, and cleanup are your responsibility
  (the CLI provides `show logging` and `delete logging` commands for local
  cleanup).
- `logs/` is ignored by Git. Never commit real lab logs.

## 9. SSH

Managed sessions run the system OpenSSH client. Password and key/agent
authentication are supported.

- **Host keys:** `terminal_open` does not set any host-key option, so
  OpenSSH's own defaults and the user's own SSH configuration and
  `known_hosts` apply. An unknown host-key confirmation prompt is not
  automated by `terminal_open`; it is left in the terminal, where an AI
  client using `terminal_send` can answer it. Network Lab MCP therefore does
  not by itself guarantee that a host key was verified by a human. Pre-populate
  `known_hosts` for lab devices if you want to avoid trust-on-first-use
  decisions being made through the terminal.
- **Topology discovery:** the Discovery bootstrap login uses
  `StrictHostKeyChecking=accept-new`, which records a new host key on first
  contact (trust on first use) and rejects a changed one. Use it only on lab
  networks where that is acceptable.
- Do not weaken SSH settings to make Network Lab MCP work.

## 10. Telnet

Telnet is supported for classic IOS-style devices.

**Telnet provides no transport encryption.** Credentials and all terminal
traffic can be read by anyone who can observe that network path. Prefer SSH
wherever practical, and use Telnet only in isolated lab networks where that
risk is understood and accepted.

## 11. Jump hosts

A device may reference one jump host from the same access-info definition,
connected with OpenSSH `-J`. Multi-hop chains are not supported, and a device
using a jump host must use SSH.

Jump-host authentication is expected to be non-interactive (key or agent). A
password prompt from the jump host is not answered automatically, and a prompt
that cannot be attributed to the target or the jump host fails closed. Jump
host entries are part of private access-info and are not returned over MCP.

## 12. Candidate / commit boundary

The CLI's candidate/commit model governs **Network Lab MCP's own definitions
and selection** (access-info, topology, Scenario, Reference, running-config):
changes are staged as a candidate and become effective only when a human
commits them.

It does **not** apply to the remote devices. It gives no transaction,
rollback, or commit-confirm behavior to commands sent to a device CLI, and it
does not review them.

## 13. Topology discovery

`discover topology` collects LLDP/CDP (and optional IPv4/VRF) information with
read-only `show` commands, plus `terminal length 0` to disable paging, and
turns the result into a **topology candidate**. It does not auto-commit or
auto-select the topology, and parsing or reconciliation that is ambiguous
fails closed instead of guessing. A human reviews and commits the candidate.

This describes how discovery results are applied locally. It is not a safety
guarantee for terminal commands in general.

## 14. Local files to protect

- `lab/access-info/*.yaml`: plain-text private credentials.
- `lab/settings.yaml`: local selection (ignored by Git).
- `logs/terminal/`: session transcripts (section 8).
- Non-public topology, Scenario, and Reference files: they may describe
  private environments or customers. Only explicitly approved public
  definitions are tracked.

Protecting these files with appropriate filesystem permissions and backup
handling is a user responsibility; Network Lab MCP does not enforce it.

## 15. Human oversight

The intended division of work: the human states the intent; the AI
investigates and may propose reusable Scenario/Reference content; the human can
watch terminal activity and reviews and commits definition and selection
changes; the operator keeps final engineering judgment.

This is not per-command approval. Terminal commands sent by the AI do not
require human confirmation.

## 16. Deployment recommendations

- Use isolated, controlled lab networks and dedicated lab accounts with least
  practical privilege.
- Prefer SSH over Telnet; keep `known_hosts` for lab devices maintained.
- Protect `lab/access-info/` and `logs/`; never commit real credentials or
  logs.
- Be deliberate about commands that print secrets (for example configuration
  displays) or change device state.
- Review Scenarios and References before relying on them.
- Run the MCP server only on a host and account you trust; do not expose it to
  untrusted users or environments. It uses stdio and a dedicated local tmux
  server.
- Keep Python dependencies, OpenSSH, and tmux up to date.

## 17. Reporting a vulnerability

Network Lab MCP is maintained in the CiscoDevNet organization
(<https://github.com/CiscoDevNet/network-lab-mcp-community>). The project team
and community take security bugs seriously, and responsible disclosure is
appreciated.

Report security bugs by emailing `oss-security@cisco.com`.

- Do **not** report a security bug through a public GitHub issue or pull
  request.
- Do **not** post credentials, exploit details, private device data, logs, or
  other sensitive material in any public place.
- Include the affected version or commit and, where possible, steps to
  reproduce that do not require real credentials or real device data.

### Disclosure policy

When a security bug report is received, a primary handler coordinates the fix
and release process:

- Confirm the problem and determine the affected versions.
- Audit the code for similar problems.
- Prepare fixes for the releases that are still maintained.

This project does not currently offer a bounty. It makes no guaranteed
response-time commitment: this is a lab-oriented project, and responses are
handled on a best-effort basis.

### Comments on this policy

If you have suggestions on how this process could be improved, please submit a
pull request.
