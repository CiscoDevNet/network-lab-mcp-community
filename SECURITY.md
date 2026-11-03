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

- **Host keys (lab TOFU):** an SSH hop that is **not registered yet** is registered automatically on first
  use, before any credential is sent (section 11a). Registered keys are never extended, removed or replaced
  by `terminal_open` or any other MCP tool. Every credential-carrying SSH hop (target and jump host) runs with
  `StrictHostKeyChecking=yes`, `UpdateHostKeys=no` and no connection multiplexing, so a changed or other-type
  key ends the connection instead of showing a `yes/no` prompt that could be answered through
  `terminal_send`. Only hosts defined in the active access-info (device and its jump host) can ever be
  registered; there is no MCP argument that names a host or port.
- **Topology discovery:** the same TOFU applies (no typed fingerprint, no key-type choice). Replacing a
  registered key remains a human-only action in the CLI (section 11a).
- **Managed terminals:** see above. The managed terminal never removes or replaces host keys.
- Do not weaken SSH settings to make Network Lab MCP work.

## 10. Telnet

Telnet is supported for classic IOS-style devices.

**Telnet provides no transport encryption.** Credentials and all terminal
traffic can be read by anyone who can observe that network path. Prefer SSH
wherever practical, and use Telnet only in isolated lab networks where that
risk is understood and accepted.

## 11. Jump hosts

A device may reference one jump host from the same access-info definition,
connected through OpenSSH with an explicit per-hop
`-o ProxyCommand=ssh ... -W %h:%p` child (not `-J`, see below). Multi-hop chains are not supported, and a device
using a jump host must use SSH.

Jump-host authentication is expected to be non-interactive (key or agent). A
password prompt from the jump host is not answered automatically, and a prompt
that cannot be attributed to the target or the jump host fails closed. Jump
host entries are part of private access-info and are not returned over MCP.

### Disconnect safety and the jump-host boundary

When an SSH/Telnet connection ends (exit, idle timeout, network failure, a
failed connection attempt), input sent afterwards through `terminal_send`
must never be executed by a local shell. A managed session's transport is
the pane's main process, so no local shell remains; the session becomes an
`exited` pane that `terminal_send` refuses. This protects the machine running
Network Lab MCP.

The guarantee holds for the connections Network Lab MCP starts: direct SSH,
Telnet, and the single-hop OpenSSH `-J` ProxyJump, where the jump host only
forwards the connection and no interactive jump-host shell exists. It does
**not** cover a nested connection started from an interactive shell on a
jump host (for example `ssh jump` and then `ssh router`): if the inner
connection ended, input could reach the jump host's shell. That form is not
supported. A future form could start the inner connection with
`exec ssh ...` / `exec telnet ...` on the jump host so that no shell remains.
Custom `ProxyCommand` or `ssh` wrapper settings in the operator's own
`~/.ssh/config` are outside this guarantee.

### Jump-server authentication (SSH ProxyJump)

- A jump-host password prompt is answered by Network Lab MCP only when a
  jump-host password is configured, the prompt names exactly the configured
  jump user and address (`user@host's password:` or `(user@host) Password:`),
  and the two hops are distinguishable. Aliases, mismatching users, bare
  `Password:` prompts and identical user/host on both hops fail closed.
- Each hop's password is sent at most once; a re-prompt is
  `jump host authentication failed`. The pane and process state are
  re-verified immediately before sending. This reduces, but cannot eliminate,
  the window between prompt detection and sending.
- The ssh command always gets `-o StrictHostKeyChecking=yes` for the target
  **and** for the jump hop (inside the ProxyCommand child): changed or unregistered
  host keys fail instead of being accepted, whatever the user's `ssh_config`
  says (an unregistered key was registered *before* this connection, see section 11a). Verified with OpenSSH 10.3 on loopback: with `-J` the jump hop does
  **not** inherit a command-line `StrictHostKeyChecking` (it still prompted),
  which is why `-J` is no longer used.
- Discovery answers the jump-host prompt through the same stage logic (`terminal.authenticate_bootstrap_jump_stage`), once per hop, with the same attribution and re-prompt fail-closed rules. Discovery uses `StrictHostKeyChecking=yes` on every hop, so a changed key fails Discovery. The jump-host prompt logic was verified against local fakes and the host-key behaviour against loopback OpenSSH; the later real-lab Discovery run through a jump server is summarized in the verification status of section 11a. Real-device key changes, AAA effects and the jump server's own first-use registration remain unverified.
- Access-info address, port and username are validated at commit and again
  when the argv is built; unsafe values never reach ssh/telnet.

### Telnet via jump host (SSH local port forward)

`transport: telnet` with `jump-host` runs as one logical Managed Terminal made
of two tmux sessions, both created through the managed-terminal launch path (read
gate, `exec`, remain-on-exit, pane identity, pipe-pane logging):

- tunnel session `network-lab-tunnel-<device>`: `ssh -N -T -o ExitOnForwardFailure=yes
  -o ForwardAgent=no -o ForwardX11=no -o PermitLocalCommand=no
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile=<product> <user> -o ControlMaster=no -o ControlPath=none
  -o LogLevel=VERBOSE -o ServerAliveInterval=15 -o ServerAliveCountMax=3
  -L 127.0.0.1:<dynamic-port>:<device>:<port> <jump>`. It is internal: no MCP
  tool derives its name, `terminal_list` never shows it, `terminal_send`
  cannot reach it. The 15 s x 3 keepalive (about 45 s) ends a silently dead
  tunnel without being so tight that a slow link drops it.
- device session `network-lab-device-<device>`: the telnet client, resolved once
  to an absolute path (`telnet client not found` before any tunnel starts),
  run as `<abs telnet> -K -E 127.0.0.1 <port>` (no auto-login, no escape).

Order of events: jump password answered once on the tunnel pane (prompt must
name the configured jump user and address, the pane is re-verified right
before sending) -> *ready* only when the pane is alive, OpenSSH logged
`Authenticated to ...`, and the managed ssh pid is the sole owner of the
`127.0.0.1:<port>` listener -> telnet starts -> an `lsof -nP -F` snapshot must
show exactly one ESTABLISHED connection from the telnet pid to the port whose
server side is owned by the managed ssh pid -> the same 4-tuple and owners must
verify again immediately before the username and again before the password.
Any lsof failure, parse failure, missing/duplicate socket, foreign owner or
changed 4-tuple sends nothing and closes both sessions. `ControlMaster=no`
plus `ControlPath=none` means a foreign ControlMaster cannot own the listener.
A port conflict (explicit bind error only) is retried on a new port up to 3
times; each retry is a new ssh and answers the jump password once again.

Lifecycle: the telnet client ending closes the tunnel (lazily, on the next
list/read/send/open); a dead tunnel makes `terminal_send` refuse and
`terminal_list` report `exited`; `terminal_close` closes the device session and
then the tunnel session by their derived names, and refuses a tunnel session
whose recorded pane identity cannot be verified. After an MCP restart the tmux
options (pane id/pid, tunnel port, tunnel session name) allow reuse of a
healthy logical session after re-verifying ownership; a tunnel session without a
matching device session is an orphan that is neither reused nor deleted
automatically (`terminal_close` is the explicit way out).

The runtime guard is the code constant `terminal.TELNET_VIA_JUMP_ENABLED`
(True since the safety tests in `tests/test_telnet_via_jump.py` pass). No
environment variable, YAML, CLI command or MCP argument reads or changes it.
Set it to False to disable the path again; Direct SSH, ProxyJump and direct
Telnet are unaffected. Discovery uses the same code with its own namespaces
(`network-lab-discovery-*`, `network-lab-discoverytunnel-*`) and never reuses a
session.

Verification status of this path. Verified with fake `ssh`/`telnet`
executables and an isolated tmux: the launch, ownership and cleanup logic.
Verified with a real OpenSSH 10.3 client against throw-away loopback `sshd`
instances in an isolated sandbox (never the user's `~/.ssh` or `known_hosts`):
strict per-hop host key verification (unknown and changed keys are refused),
and that no prompt reaches `terminal_send`. Not verified: whether real `lsof`
behaviour for `ssh -L` listeners matches the Fakes, the macOS telnet client's
option negotiation (`-K`, NEW-ENVIRON), a real jump server (including its
password authentication), real routers, and Linux.

Residual risks: Telnet is plaintext between the jump host and the device;
loopback is not per-process access control (any local process can connect, and
the ownership proofs narrow but cannot eliminate the window between a proof
and the next keystroke); lsof snapshots are not atomic; a tunnel left running
after a crash holds a loopback port and a jump-host connection until closed;
the tunnel pane's log lives in the device's log directory as a separate file.

## 11a. SSH host keys: lab TOFU and human-only replacement

**What changed.** Earlier versions made the operator type the key type and fingerprint of every new host. The product is for labs, so a host key that is **not registered anywhere** is now registered automatically (TOFU, trust on first use) and the operator does nothing. Registered keys keep their full protection.

**TOFU limit (accepted for lab use).** The first key is obtained over the network and cannot be proven authentic. An attacker who sits between you and the device during the *first* connection is trusted from then on. Register keys yourself (manual `ssh`, or edit your own known_hosts) when a lab network is not trusted.

**Method: pre-register, then strict connect.**
1. The host identifier (`host` for port 22, `[host]:port` otherwise) is looked up in the product known_hosts, your `~/.ssh/known_hosts` and the global known_hosts. An entry in any of them means *registered*: nothing is probed or added. A file that cannot be read safely (symlink, unreadable, unsupported entry) stops the operation (`TOFU_IDENTITY_ERROR`).
2. An unregistered hop gets **one authentication-free probe** (no automatic retry): the OpenSSH client with the dummy user `nlm-hostkey-probe`, `PreferredAuthentications=none`, password / public-key / keyboard-interactive / host-based / GSSAPI off, `IdentityAgent=none`, `BatchMode=yes`, `-N -T`, `SessionType=none`, `PermitLocalCommand=no`, `ControlMaster=no`, `ControlPath=none`, `ControlPersist=no`, `UpdateHostKeys=no`, `HashKnownHosts=no`, `CheckHostIP=no`, `VerifyHostKeyDNS=no`, `StrictHostKeyChecking=accept-new` against a **temporary** known_hosts, with a connect timeout and an overall deadline. Verified against a local OpenSSH 10.3 server at DEBUG3: the server saw exactly one request (`none` for `nlm-hostkey-probe`) although it also offered `publickey` and `keyboard-interactive`. The probe never uses the real login name. An authentication success or a confirmation prompt on the probe is treated as an error: the connection is closed, nothing runs and nothing is registered.
3. The recorded key is validated: exactly one entry, the expected host identifier (a `HostKeyAlias` or `HostName` in your ssh_config that changes it fails closed with `TOFU_IDENTITY_ERROR`), a supported type (ED25519, ECDSA, RSA), a well-formed blob whose embedded type matches, and a SHA256 fingerprint cross-checked with `ssh-keygen -l`. Only the **one negotiated key** is registered; the algorithm is negotiated by OpenSSH (not fixed), and SHA-1 `ssh-rsa` is never enabled by this tool.
4. The entry is appended to the **product known_hosts** (macOS `~/Library/Application Support/Network Lab MCP/ssh/known_hosts`, Linux `~/.local/state/network-lab-mcp/ssh/known_hosts`; directory `0700`, file `0600`, not a symlink, owned by you) under a dedicated lock file (`flock`, in a `locks` directory next to it; the known_hosts itself is not the lock), `O_APPEND` single write, `fsync` of file and directory, then **re-read through `ssh-keygen` and compared** with the probed key. The probe, the append and the re-read happen under the lock, after the state was re-checked, so concurrent processes probe and register a host once. Any failure stops before a connection that carries a credential (`TOFU_STORAGE_ERROR`). Your own `~/.ssh/known_hosts` and the system file are **never modified** (they are only read).
5. Every credential-carrying connection then runs `StrictHostKeyChecking=yes`, `UpdateHostKeys=no`, `CheckHostIP=no`, `VerifyHostKeyDNS=no`, `ControlMaster=no`, `ControlPath=none`, `ControlPersist=no`, with `UserKnownHostsFile` = product file + your file, on **every hop**. Multiplexing is disabled so that no existing master connection can stand in for this verification. `CheckHostIP=no` means the IP address of a host reached by name is not verified independently (trade-off, consistent with the probe).

**Invariants.** (1) The key verified by the credential-carrying connection is the registered key (for a first use, the probed key). (2) No credential is sent to a hop before its key is accepted *and persisted*: after a TOFU the persistence and the re-read are complete before the first credential. (3) A first-use key is accepted without a human; every other change of trust needs a human at the CLI. (4) MCP and managed terminals cannot approve, remove or replace anything.

**ProxyJump.** Jump server and target are independent hops, registered in that order. The probe of a target runs through the same jump hop, which is strictly verified and authenticated with its real user as in a normal connection; only the probed hop gets the dummy user and no credential. If the jump key does not verify, the target is never reached. A jump password is only ever sent through the existing attribution logic, at most once per connection, never again after a re-prompt or failure in the same run, and every probe counts toward the cap `min(40, 2 x (devices behind a password-authenticated jump host) + 6)`. Probes happen only for **unregistered** hops (a registered target is not probed), so the cap is not reached by normal use. **Telnet over an SSH jump host** (local port forwarding): only the jump server's SSH key is verified; the Telnet device has none and the Telnet leg stays plaintext.

**What the probe costs on the device.** A device that logs or counts the `none` attempt of a non-existent user may count it as a failed login or ask its AAA server about `nlm-hostkey-probe`. Some devices rate-limit or temporarily block a source address after failed logins (for example `login block-for`). Whether that happens depends on the platform, the software and the AAA configuration and **has not been verified on real equipment**. A probe that fails is never retried; if a device cannot be probed safely, register its key manually once (`ssh -o StrictHostKeyChecking=ask` to the device with your own ssh, or add it to the known_hosts) and the tool will use it. If an error does not clearly identify the cause, the tool does not claim that AAA blocked it.

**Registered keys are protected.**
- A changed key, and a key of a **different type** than the registered one (OpenSSH reports this as a changed key; the CLI calls it `KEY_TYPE_MISMATCH`; it can also be caused by a configuration change or software update, not only by reinstalling), stop the connection before any credential is sent. They are never replaced, removed or extended automatically, and a server that offers an additional key type does not get it registered (`UpdateHostKeys=no`; a registered type is preferred in the negotiation by OpenSSH).
- **Human only.** Replacement exists only in the interactive human CLI (`discover topology`). The probe obtains the new key into a temporary file; the CLI shows the trusted and new fingerprints and the warning that the key was obtained over the network and is not independently verified, then asks `Replace the trusted host key? [yes/no] (default: no):` on the **controlling terminal (`/dev/tty`)**. Input that was already buffered (pasted, piped or typed ahead), an empty or any other answer, EOF, Ctrl-C and the 180 s timeout all cancel; stdin and stdout must be the same TTY. On yes: re-check that the file did not change, back up (to a `0700` directory, files `0600`; contains other hosts' keys; never auto-deleted), remove exactly that host's entries (`ssh-keygen -R`), restore owner and `0600` and delete the leftover `.old`, append the new key, re-read and compare. If registration fails after removal, the removed entries are appended back (never overwriting other processes' additions) or the backup path is shown. Approvals are in memory, scoped, expiring and single-use.
- Entries in **your own** known_hosts or the system file are never edited. For a changed key that lives there the CLI prints the manual command, e.g. `ssh-keygen -R '[host]:port' -f ~/.ssh/known_hosts` (use the `[host]:port` form for a non-default port or the `HostKeyAlias`), or tells you to ask the administrator. This tool does not run it.
- MCP tools and managed terminals have no code path to replacement, removal, backups or the TTY approval (tested statically and dynamically). The terminal layer reaches only `hostkey_tofu`, which can append a key for an unregistered hop of a device from the access-info.

**Logging.** Every TOFU result is appended as a JSON line to `logs/hostkey-tofu.log` (`0600`; git-ignored): time, device, hop (`direct`/`jump`/`target`), host identifier, port, key type, SHA256 fingerprint of the key that was actually registered, and the result (`registered`, `probe_failed`, `registration_failed`, `replaced`). The CLI prints `SSH host key automatically accepted (TOFU):` with device, hop, type and fingerprint. No credential, private key or key blob is logged.

**Local commands in your ssh_config.** Production connections and probes use your normal ssh_config resolution (`Host`, `HostName`, `Port`, `KexAlgorithms`, `HostKeyAlgorithms`, `ProxyJump`, `ProxyCommand`, `IdentityFile`), so settings such as `Match exec`, `ProxyCommand` and `LocalCommand` run on your machine exactly as they would for a normal `ssh`; `PermitLocalCommand=no` does **not** switch off `Match exec` or `ProxyCommand`. The probe adds no new local command, but it runs the ones your configuration attaches to that host. This tool does not run `ssh -G` against your configuration.

**Other limits.**
- A host that is reachable under several names or addresses is registered per identifier (`host` or `[host]:port`); the same device under another name or its IP is a different, unregistered host (it would get its own TOFU entry). Existing entries that already cover it (any file) are respected; entries this tool cannot interpret (markers, `@cert-authority`, patterns, `KnownHostsCommand`, DNS host keys) fail closed.
- Exactly one key type per host is registered. A device that later offers only another type triggers the human review.
- Legacy devices that only offer SHA-1 `ssh-rsa` or old key exchange fail with `HOST_KEY_ALGORITHM_MISMATCH` / `SSH_KEX_MISMATCH`; enable the algorithm for that host in your own ssh_config. If the key was registered but the main connection then fails during negotiation, the message says so separately.
- A TTY does not prove a human is typing: an AI or script able to drive your terminal (PTY automation) can approve a *replacement*; keep the human CLI out of the AI's reach.
- Error classes: `HOST_KEY_UNKNOWN`, `HOST_KEY_CHANGED`, `KEY_TYPE_MISMATCH`, `HOST_KEY_ALGORITHM_MISMATCH`, `SSH_KEX_MISMATCH`, `TOFU_PROBE_ERROR`, `TOFU_STORAGE_ERROR`, `TOFU_IDENTITY_ERROR`, `JUMP_AUTH_ERROR`, `SSH_AUTH_ERROR`.
- Verification status:
  - Automated tests ran against isolated local OpenSSH 10.3 (macOS) servers behind a forced test wrapper.
  - Real lab (macOS, maintainer-run): Topology Discovery through an SSH jump server connected to 9 of 9 devices, registered 7 first-use keys automatically (ED25519, RSA, ECDSA) with no manual key input, and a second run succeeded with no new registration. The CLI output alone does not show that no probe ran the second time or that every hop used `StrictHostKeyChecking=yes`; that rests on the code and tests.
  - **Not verified:** the jump server's own first-use registration, refusal of a changed real-device key, CLI re-registration on real devices, effects on AAA failed-login counters, TACACS+/RADIUS and `login block-for`, Linux, legacy SSH algorithms on real devices, all authentication methods and failure patterns, and all failure handling of SSH local port forwarding for Telnet. This is not a completed security test of the feature.

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
