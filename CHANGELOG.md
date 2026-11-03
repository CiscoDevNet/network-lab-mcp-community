# Changelog

## Unreleased

### Changed
- **SSH host keys: lab TOFU (trust on first use) replaces the typed approval.** A host key that no known_hosts file
  has an entry for is registered automatically, for `discover topology` and managed terminals, with no key-type or
  fingerprint input. Method: an authentication-free OpenSSH probe (dummy user `nlm-hostkey-probe`, only the `none`
  method, no multiplexing, `-N -T`, temporary known_hosts, at most one probe per unregistered host, no retry), the
  one negotiated key validated and saved in the **product known_hosts** (macOS
  `~/Library/Application Support/Network Lab MCP/ssh/known_hosts`, Linux `~/.local/state/network-lab-mcp/ssh/known_hosts`,
  `0700`/`0600`, dedicated lock file, `fsync`, read back and compared) **before any credential is sent**, then every
  credential-carrying hop runs `StrictHostKeyChecking=yes` with the product and user known_hosts. The user's own
  known_hosts is read, never modified. Jump server and target are registered independently; the target probe goes
  through the verified, authenticating jump hop and counts toward the existing jump-password budget.
- **Replacing a registered key is unchanged in spirit and still human-only:** a changed key or a key of another type
  (`KEY_TYPE_MISMATCH`) is refused before any credential; the CLI shows old and new fingerprints and asks
  `Replace the trusted host key? [yes/no] (default: no):` on `/dev/tty`, then backs up, removes only that host's
  entries from the product known_hosts and registers the new key. Keys in the user's or the system known_hosts are
  never edited (a manual `ssh-keygen -R` is shown).
- Managed terminals and Discovery now use `ControlMaster=no`, `ControlPath=none`, `ControlPersist=no` and
  `CheckHostIP=no` on every credential-carrying hop, so no existing master connection can stand in for host-key
  verification.
- Every TOFU result is logged to `logs/hostkey-tofu.log` and printed by the CLI with the SHA256 fingerprint. New error
  classes: `TOFU_PROBE_ERROR`, `TOFU_STORAGE_ERROR`, `TOFU_IDENTITY_ERROR`, `HOST_KEY_ALGORITHM_MISMATCH`,
  `SSH_KEX_MISMATCH`, `JUMP_AUTH_ERROR`. MCP tool schemas and return values are unchanged (seven tools).
- Test isolation: the forced OpenSSH wrapper accepts the new probe/strict options only with their safe values.

### Removed
- The typed key-type/fingerprint pre-approval prompt and the `StrictHostKeyChecking=ask` registration connection.

### Security notes
- TOFU cannot prove the first key authentic; a man in the middle of the first connection is trusted (accepted for
  labs). The probe can be counted as a failed login by a device or its AAA and, on some platforms, can trigger login
  rate limits or source blocking; **effects on AAA and login blocking are not verified on real equipment**. If a device cannot be probed, register its key
  manually once.
- Local commands in your ssh_config (`Match exec`, `ProxyCommand`, `LocalCommand`) run during probes as for any `ssh`.
- Automated tests ran against isolated loopback OpenSSH 10.3 on macOS (full suite: 2220 passed, 2 skipped).

### Real-lab verification (macOS, maintainer-run, generic description)
- Topology Discovery through an SSH jump server ran twice. First run: 9 of 9 devices connected (8 IOS XR, 1 IOS,
  including a Telnet-only device), 7 unregistered host keys were registered automatically by TOFU (5 ED25519, 1 RSA,
  1 ECDSA) with no key-type or fingerprint input; 40 LLDP and 5 CDP observations, 22 managed links, 1 unresolved
  neighbor. Second run: 9 of 9 connected, no TOFU registration shown, identical counts.
- The CLI output alone does not prove that no probe ran on the second run or that `StrictHostKeyChecking=yes` was
  applied on every hop; that rests on the code and the automated tests.
- **Still unverified:** the jump server's own first-use TOFU registration (a successful connection through it is not the
  same thing), refusal of a changed real-device key, CLI re-registration of a real-device key, effects on AAA
  failed-login counters, TACACS+/RADIUS, `login block-for` or source blocking, TOFU on Linux, legacy SSH algorithms on
  real devices, all SSH authentication methods and failure patterns, and all failure handling of SSH local port
  forwarding for Telnet.

## 0.3.0 — 2026-11-02

### Added
- **macOS local workstation support:** run Network Lab MCP and the Claude Code MCP registration on macOS; the README
  install steps (Python, tmux, OpenSSH, optional Telnet client, Vim) were rewritten for macOS.
- **SSH ProxyJump with jump-server password authentication:** managed terminals and Discovery answer the jump
  server's interactive password prompt (`user@host's password:` and `(user@host) Password:`), strictly attributed by
  user and host, with at most one password send per hop. A re-prompt fails closed.
- **Telnet over an SSH jump server:** SSH local port forwarding (`127.0.0.1` only) plus a local Telnet client for
  legacy Telnet-only devices, with TCP ownership verification of the tunnel before credentials are sent.
- **Topology Discovery through a jump server** for both methods, reusing the managed-terminal authentication logic.
- **Interactive SSH host-key trust (human CLI `discover topology` only):** an unknown key is registered only after
  the operator approved the expected key type and `SHA256:` fingerprint and the host presents exactly that key.
- **SSH host-key recovery:** after a changed key the operator can review replacement: the new key is verified on a
  copy of `known_hosts`, the old file is backed up, only that host's entries are removed, and the new key is
  registered after a second exact match. Backups are kept in a private product directory.
- Access-info validation for host, port and username values (option-like values, whitespace and control characters
  are rejected).
- `docs/connection_architecture.md`: how OpenSSH, tmux and the Telnet client are composed.

### Security
- SSH host keys are verified strictly (`StrictHostKeyChecking=yes`, `UpdateHostKeys=no`) on **every hop** of managed
  terminals and Discovery. `accept-new` is no longer used anywhere; non-interactive Discovery stops on an unknown or
  changed key and registers nothing.
- ProxyJump uses an explicit per-hop `ProxyCommand` instead of `-J`: with OpenSSH 10.3 the `-J` child did not inherit
  a command-line `StrictHostKeyChecking`, so an unknown jump-server key could be answered through `terminal_send`.
- MCP tools and managed terminals cannot approve, remove or replace host keys.
- Registration connections never send target credentials; a jump password is sent at most once per connection with a
  bounded attempt budget and a stop after the first failure for that jump server.
- `known_hosts` replacement never writes a backup back wholesale; concurrent changes are detected and stop the
  operation with a pointer to the backup.
- Managed terminal disconnect safety: input is no longer sent to the local shell after an SSH/Telnet session ends;
  sends are refused unless the managed pane identity is verified.

### Changed
- SSH to a host whose key is not yet registered now fails in managed terminals instead of showing OpenSSH's
  confirmation prompt: register the key with `discover topology` or manually first.
- `help claude` shows `claude mcp add --scope user network-lab -- <path>/.venv/bin/network-lab-mcp`, matching the
  README.

### Testing
- Host-key behaviour is tested against isolated loopback OpenSSH servers through a forced wrapper that keeps tests
  away from the real `~/.ssh`, plus fakes for jump-server password prompts.

### Verification status
- macOS real-lab checks (managed terminals and Discovery through an SSH jump server, including a Telnet-only device)
  were run by the maintainer **before** the host-key management and the per-hop ProxyCommand change.
- Host-key registration, replacement and the strict ProxyCommand path have **not** been validated against real
  routers or a real jump server. Telnet-over-SSH host-key registration is disabled; Linux is unverified.
- The MCP tool surface is unchanged (seven tools).
