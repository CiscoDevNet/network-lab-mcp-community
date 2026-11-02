# Changelog

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
