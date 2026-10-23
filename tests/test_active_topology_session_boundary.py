# SPDX-License-Identifier: Apache-2.0
"""Managed-session / active-topology boundary.

Investigation found a real inconsistency: `terminal_open()` already
required the device to be present in the *active* topology
(`lab.get_device()`), but `terminal_send()`/`terminal_read()` never
checked this at all -- they dispatched straight to `terminal.send_to_
device()`/`terminal.read_device()`, which only look at the tmux session
name. Since managed sessions are persistent and survive an
active-topology change, this meant `terminal_open()`'s own active-topology
gate was trivially bypassable simply by continuing to use a session that
was already open before the topology changed.

Chosen model (the smallest internally consistent one, matching what
`terminal_close()` already needed for stale-session cleanup):

    terminal_open()   -> requires active-topology membership (unchanged)
    terminal_send()   -> requires active-topology membership (new)
    terminal_read()   -> requires active-topology membership (new)
    terminal_list()   -> unrestricted: shows every managed session,
                         regardless of active-topology membership, so a
                         stale session remains discoverable
    terminal_close()  -> unrestricted: can always close a stale session

This file tests the *new* verify_device_in_active_topology() unit
behavior, and the full scenario end-to-end at the real MCP tool boundary:
open a session while the device is in the active topology, switch the
active topology to one that no longer contains it, and confirm send/read
now fail closed while list/close are unaffected."""

from __future__ import annotations

import json

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from network_lab_mcp import lab, mcp_server, terminal


@pytest.fixture(autouse=True)
def _patch_lab_root(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)


@pytest.fixture(autouse=True)
def isolated_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")


@pytest.fixture(autouse=True)
def _cleanup_device_sessions():
    yield
    for session in terminal.list_device_sessions():
        terminal.close_device_terminal(session["device"])


# ---- unit-level: verify_device_in_active_topology() ----


def test_verify_device_in_active_topology_passes_for_a_present_device():
    lab.verify_device_in_active_topology("R1")  # sample_lab topology has R1 -- no raise


def test_verify_device_in_active_topology_fails_for_an_absent_device():
    with pytest.raises(lab.LabConfigError, match="not present in active topology"):
        lab.verify_device_in_active_topology("NOT-IN-TOPOLOGY")


# ---- end-to-end: active topology changes out from under an open session ----


def _switch_active_topology_to_empty(lab_root):
    lab.write_topology("other_topology", {"name": "other_topology", "devices": {}, "links": []}, lab_root)
    settings = lab.read_settings(lab_root)
    settings["active_topology"] = "other_topology"
    lab.write_settings(settings, lab_root)


def test_send_and_read_fail_once_device_leaves_the_active_topology(monkeypatch, lab_root):
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["bash", "-c", "cat"]))
    monkeypatch.setattr(terminal, "_authenticate_managed_session", lambda *a, **k: None)

    terminal.open_device_terminal("R1", {"transport": "ssh", "address": "192.0.2.1"})
    assert "R1" in {s["device"] for s in terminal.list_device_sessions()}

    _switch_active_topology_to_empty(lab_root)

    async def call(tool_name, args):
        return await mcp_server.mcp.call_tool(tool_name, args)

    with pytest.raises(ToolError, match="not present in active topology"):
        anyio.run(call, "terminal_send", {"device": "R1", "text": "show version"})

    with pytest.raises(ToolError, match="not present in active topology"):
        anyio.run(call, "terminal_read", {"device": "R1"})


def test_terminal_list_still_shows_a_stale_session_outside_the_active_topology(monkeypatch, lab_root):
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["bash", "-c", "cat"]))
    monkeypatch.setattr(terminal, "_authenticate_managed_session", lambda *a, **k: None)

    terminal.open_device_terminal("R1", {"transport": "ssh", "address": "192.0.2.1"})
    _switch_active_topology_to_empty(lab_root)

    result = anyio.run(lambda: mcp_server.mcp.call_tool("terminal_list", {}))
    assert not result.is_error
    payload = json.loads(result.content[0].text)
    devices = {s["device"] for s in payload["sessions"]}
    assert "R1" in devices


def test_terminal_close_can_still_close_a_stale_session_outside_the_active_topology(monkeypatch, lab_root):
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["bash", "-c", "cat"]))
    monkeypatch.setattr(terminal, "_authenticate_managed_session", lambda *a, **k: None)

    terminal.open_device_terminal("R1", {"transport": "ssh", "address": "192.0.2.1"})
    _switch_active_topology_to_empty(lab_root)

    result = anyio.run(lambda: mcp_server.mcp.call_tool("terminal_close", {"device": "R1"}))
    assert not result.is_error
    assert "R1" not in {s["device"] for s in terminal.list_device_sessions()}


def test_open_still_requires_active_topology_membership(lab_root):
    """terminal_open()'s own, pre-existing gate (lab.get_device()) --
    unchanged by the send/read boundary check."""
    _switch_active_topology_to_empty(lab_root)

    async def call():
        return await mcp_server.mcp.call_tool("terminal_open", {"device": "R1"})

    with pytest.raises(ToolError, match="not present in active topology"):
        anyio.run(call)
