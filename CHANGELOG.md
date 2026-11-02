# Changelog

## 0.3.0 — unreleased

The release date is set when v0.3.0 is publicly released.

### Added
- **macOS local workstation support:** run Network Lab MCP and the Claude Code MCP registration on macOS; the README
  install steps (Python, tmux, OpenSSH, optional Telnet client, Vim) were rewritten for macOS.
- **SSH ProxyJump with Jump Server password authentication:** managed terminals answer the jump server's interactive
  password prompt (`user@host's password:` and `(user@host) Password:`), strictly attributed by user and host, with at
  most one password send per hop. A re-prompt fails closed.
- **Telnet over an SSH jump server:** SSH local port forwarding (`127.0.0.1` only) plus a local Telnet client for
  legacy Telnet-only devices, with TCP ownership verification of the tunnel before credentials are sent.
- **Topology Discovery through a jump server:** Discovery authenticates the jump server for SSH ProxyJump and supports
  Telnet via the tunnel, reusing the managed-terminal authentication logic.
- Access-info validation for host, port, and username values (option-like values, whitespace, and control characters
  are rejected).

### Changed
- When a jump password is configured, SSH uses `StrictHostKeyChecking=yes`; host keys are never auto-approved.
- `help claude` now shows `claude mcp add --scope user network-lab -- <path>/.venv/bin/network-lab-mcp`, matching the
  README, and mentions `"$(pwd)/.venv/bin/network-lab-mcp"` from the repository root.

### Fixed
- Managed terminal disconnect safety: keystrokes are no longer sent to the local shell after an SSH/Telnet session
  ends; sends are refused unless the managed pane identity is verified.

### Verification
- Managed terminal and Topology Discovery were exercised end to end from macOS against a lab (maintainer-run).
- The MCP tool surface is unchanged (seven tools).
