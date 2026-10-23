# SPDX-License-Identifier: Apache-2.0
"""Network Lab MCP stdio server.

Exposes exactly seven tools to Claude Code:

    get_active_topology()
    get_execution_instructions()
    terminal_open()
    terminal_send()
    terminal_read()
    terminal_list()
    terminal_close()

This process communicates over stdio, and stdout is reserved for MCP protocol
traffic. Diagnostic logging goes to stderr only; nothing is ever printed to
stdout.
"""

from __future__ import annotations

import logging
import sys

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from network_lab_mcp import __version__, lab, terminal

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s network-lab-mcp: %(message)s")
logger = logging.getLogger("network_lab_mcp")

mcp = MCPServer("network-lab-mcp", version=__version__)


@mcp.tool()
def get_active_topology() -> dict:
    """Return the currently active lab topology: where the work is performed and
    which devices and links exist. Settings and the topology YAML are reloaded
    from disk on every call, so editing lab/settings.yaml takes effect
    immediately without restarting this server."""
    try:
        return lab.get_active_topology()
    except lab.LabConfigError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def get_execution_instructions(
    inspect_scenarios: list[str] | None = None,
    inspect_references: list[str] | None = None,
) -> dict:
    """Return the operating principles, the active scenario, and the active
    reference knowledge for the current task: how to behave, what to
    accomplish, and what reusable knowledge is available. Also returns
    metadata-only catalogs (name + description) of every stored scenario
    and reference, so existing knowledge can be discovered and reused.
    Optional `inspect_scenarios` / `inspect_references` (lists of stored
    definition names from those catalogs) additionally return the full
    content of those definitions, read-only: inspecting never activates or
    changes anything, and unknown names fail the call. Definition changes
    are not made through MCP; propose them and a human reviews and commits
    them in the CLI. Reloaded from disk on every call."""
    try:
        return lab.get_execution_instructions(inspect_scenarios, inspect_references)
    except lab.LabConfigError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def terminal_open(device: str) -> dict:
    """Open (or reuse) a terminal session for a device in the active topology,
    launching ssh or telnet inside a dedicated tmux environment. The active
    topology is reloaded from disk before opening the session. An
    interactive login prompt from the device itself is answered
    automatically using the active access-info definition's own private
    credentials, once it can be safely confirmed to belong to the target
    device (never a jump host) -- credentials are never returned by this
    tool or any other. This works for SSH (the client's own password
    prompt) and for Telnet (a classic-IOS-style Username:/Password:
    sequence); which one applies depends on the device's own configured
    transport. A host-key confirmation prompt, or any other situation this
    cannot safely resolve on its own, is still left for
    terminal_read()/terminal_send() to handle interactively."""
    try:
        _, device_config = lab.get_device(device)
        return terminal.open_device_terminal(device, device_config)
    except (lab.LabConfigError, terminal.TerminalError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def terminal_send(
    device: str,
    text: str | None = None,
    keys: list[str] | None = None,
    enter: bool = False,
) -> dict:
    """Send input to a device's open terminal session. The device must
    still be present in the active topology, reloaded from disk on every
    call -- an already-open session does not stay usable if the active
    topology changes to no longer include this device.

    Execution order is fixed and deterministic: if `text` is given it is sent
    literally first, then any `keys` (e.g. "C-c", "Tab", "Up") are sent in the
    supplied order, then Enter is sent last if `enter` is true. Nothing is
    deduplicated: passing keys=["Enter"] together with enter=true sends Enter
    twice. `text` is delivered to the pane without ever passing through a
    subprocess's own argv or appearing in an error message, and this MCP
    server does not separately log or echo it back in any tool result. It
    is not hidden from the device's own terminal transcript, though: this
    project intentionally records the raw pane output via tmux pipe-pane
    (see `show logging`), and if the remote endpoint echoes back what it
    received, that echo may naturally appear in that transcript like any
    other terminal output."""
    try:
        lab.verify_device_in_active_topology(device)
        return terminal.send_to_device(device, text, keys, enter)
    except (lab.LabConfigError, terminal.TerminalError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def terminal_read(device: str, lines: int = terminal.DEFAULT_READ_LINES) -> dict:
    """Capture recent terminal output for a device, so the caller can inspect
    the current prompt, command output, a password/interactive prompt, paging
    state, or unexpected errors. `lines` limits how much recent scrollback is
    returned (default 100); the underlying tmux session retains a much larger
    history buffer. The device must still be present in the active topology,
    reloaded from disk on every call -- an already-open session does not
    stay readable if the active topology changes to no longer include this
    device."""
    try:
        lab.verify_device_in_active_topology(device)
        return terminal.read_device(device, lines)
    except (lab.LabConfigError, terminal.TerminalError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def terminal_list() -> dict:
    """List every currently managed production terminal session,
    regardless of whether each device is still present in the active
    topology -- including a stale session left over from before the
    active topology changed, so it remains visible and closeable via
    terminal_close() even though terminal_send()/terminal_read() will
    refuse to use it. Local validation sessions are never included,
    regardless of whether a device happens to be named similarly to a
    validation identifier."""
    return {"sessions": terminal.list_device_sessions()}


@mcp.tool()
def terminal_close(device: str) -> dict:
    """Close a device's managed production terminal session. Only ever targets
    the production session namespace; cannot affect validation sessions.
    Deliberately does not require the device to still be present in the
    active topology, so a stale session left over from before the active
    topology changed can always be closed."""
    try:
        return terminal.close_device_terminal(device)
    except terminal.TerminalError as exc:
        raise ToolError(str(exc)) from exc


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
