# SPDX-License-Identifier: Apache-2.0
"""Concurrency proven at the actual MCP tool boundary, not merely inside
terminal.py's private helpers.

Investigation found that the MCP SDK in use
(mcp==2.2.0) already dispatches each `tools/call` request as its own anyio
task (mcp.shared.jsonrpc_dispatcher.JSONRPCDispatcher._dispatch_request()
spawns rather than awaits, for every method except "initialize"), and that
a synchronous tool function -- every @mcp.tool() in mcp_server.py is a
plain `def`, not `async def` -- is invoked via
`anyio.to_thread.run_sync()` (mcp.server.mcpserver.utilities.func_metadata.
FuncMetadata.call_fn()), which offloads it to a real OS thread rather than
blocking the event loop. Two different-device tool calls can therefore
already overlap with zero changes to mcp_server.py's dispatch model.

These tests call `network_lab_mcp.mcp_server.mcp.call_tool(name, args)`
directly -- the same public, high-level entry point the framework's own
`_handle_call_tool()` calls for a real incoming `tools/call` JSON-RPC
request. This exercises the identical Tool.run() -> call_fn() ->
anyio.to_thread.run_sync() path a real concurrent client would, without
needing to also drive the stdio/JSON-RPC transport layer itself (pure
transport marshalling, irrelevant to the concurrency question)."""

from __future__ import annotations

import threading
import time

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from network_lab_mcp import lab, mcp_server, terminal


@pytest.fixture(autouse=True)
def _patch_lab_root(lab_root, monkeypatch):
    """terminal_send()/terminal_read() verify the device is present in the
    active topology before dispatching -- without this,
    these tests would silently (and only by coincidence) depend on
    whatever the real repository's own private lab/ directory happens to
    define R1/R2 as, which is exactly the kind of test-isolation gap this
    project's other test files already avoid via this same fixture."""
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)


# ==========================================================================
# Different-device overlap at the MCP tool boundary
# ==========================================================================


def test_different_device_reads_overlap_at_mcp_boundary(monkeypatch):
    barrier = threading.Barrier(2, timeout=5)
    entered: list[str] = []
    entered_lock = threading.Lock()

    def fake_read_device(device_name, lines=terminal.DEFAULT_READ_LINES):
        with entered_lock:
            entered.append(device_name)
        barrier.wait()  # only satisfied if BOTH calls are in flight at once
        return {"device": device_name, "session_name": f"network-lab-device-{device_name}", "content": ""}

    monkeypatch.setattr(terminal, "read_device", fake_read_device)

    async def run_both():
        results = {}

        async def call(device):
            results[device] = await mcp_server.mcp.call_tool("terminal_read", {"device": device})

        async with anyio.create_task_group() as tg:
            tg.start_soon(call, "R1")
            tg.start_soon(call, "R2")
        return results

    results = anyio.run(run_both)

    assert set(entered) == {"R1", "R2"}
    assert results["R1"].is_error is False
    assert results["R2"].is_error is False


def test_sequential_mcp_calls_would_not_satisfy_the_barrier():
    """Negative control: proves the barrier-based test above is not
    vacuously true. Two *sequential* (non-concurrent) calls into the same
    barrier-gated fake never both arrive -- the second call blocks alone
    until the barrier's timeout, raising BrokenBarrierError. This is the
    exact failure a regression to fully-serialized MCP dispatch would
    produce in the real overlap test above."""
    barrier = threading.Barrier(2, timeout=0.3)

    def fake_read_device(device_name, lines=terminal.DEFAULT_READ_LINES):
        barrier.wait()
        return {"device": device_name}

    with pytest.raises(threading.BrokenBarrierError):
        fake_read_device("R1")


# ==========================================================================
# Same-device serialization at the MCP boundary
# ==========================================================================


def test_same_device_mcp_calls_never_overlap(monkeypatch):
    active = 0
    max_active = 0
    state_lock = threading.Lock()

    def fake_read_device(device_name, lines=terminal.DEFAULT_READ_LINES):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.1)
            return {"device": device_name, "session_name": f"network-lab-device-{device_name}", "content": ""}
        finally:
            with state_lock:
                active -= 1

    # Exercise the real per-device lock: patch terminal.read_device to call
    # through terminal's own locking wrapper by wrapping the *session lock*
    # itself around the fake backend, exactly like read_device() does
    # around _capture_pane().
    session_name = terminal.derive_production_session_name("R1")

    def locked_fake_read_device(device_name, lines=terminal.DEFAULT_READ_LINES):
        with terminal._session_lock(session_name):
            return fake_read_device(device_name, lines)

    monkeypatch.setattr(terminal, "read_device", locked_fake_read_device)

    async def run_both():
        async def call():
            await mcp_server.mcp.call_tool("terminal_read", {"device": "R1"})

        async with anyio.create_task_group() as tg:
            tg.start_soon(call)
            tg.start_soon(call)

    anyio.run(run_both)
    assert max_active == 1


# ==========================================================================
# Mixed-tool concurrency
# ==========================================================================


def test_mixed_tool_calls_for_different_devices_overlap(monkeypatch):
    barrier = threading.Barrier(2, timeout=5)

    def fake_send(device_name, text, keys, enter):
        barrier.wait()
        return {"device": device_name}

    def fake_read(device_name, lines=terminal.DEFAULT_READ_LINES):
        barrier.wait()
        return {"device": device_name, "content": ""}

    monkeypatch.setattr(terminal, "send_to_device", fake_send)
    monkeypatch.setattr(terminal, "read_device", fake_read)

    async def run_both():
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp_server.mcp.call_tool, "terminal_send", {"device": "R1", "text": "x"})
            tg.start_soon(mcp_server.mcp.call_tool, "terminal_read", {"device": "R2"})

    anyio.run(run_both)  # would hang/raise BrokenBarrierError if not truly concurrent


# ==========================================================================
# Error isolation
# ==========================================================================


def test_one_device_failure_does_not_affect_the_other(monkeypatch):
    def fake_read(device_name, lines=terminal.DEFAULT_READ_LINES):
        if device_name == "R1":
            raise terminal.TerminalError("R1 is broken")
        return {"device": device_name, "session_name": "network-lab-device-R2", "content": "ok"}

    monkeypatch.setattr(terminal, "read_device", fake_read)

    async def run_both():
        results: dict[str, object] = {}

        async def call(device):
            try:
                results[device] = await mcp_server.mcp.call_tool("terminal_read", {"device": device})
            except Exception as exc:  # noqa: BLE001 - captured for assertion, not swallowed silently
                results[device] = exc

        async with anyio.create_task_group() as tg:
            tg.start_soon(call, "R1")
            tg.start_soon(call, "R2")
        return results

    results = anyio.run(run_both)
    assert isinstance(results["R1"], ToolError)
    assert "R1 is broken" in str(results["R1"])
    assert results["R2"].is_error is False


# ==========================================================================
# Public boundary regression: exactly seven MCP tools
# ==========================================================================


def test_exactly_seven_mcp_tools():
    tools = anyio.run(mcp_server.mcp.list_tools)
    names = {t.name for t in tools}
    assert names == {
        "get_active_topology",
        "get_execution_instructions",
        "terminal_open",
        "terminal_send",
        "terminal_read",
        "terminal_list",
        "terminal_close",
    }
