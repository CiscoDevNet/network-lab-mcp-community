# SPDX-License-Identifier: Apache-2.0
"""Shared pytest fixtures: an isolated lab/ directory tree per test, and an
isolated tmux socket for the whole test session.

Tests never touch the real repository lab/ directory; each test gets its own
temporary lab root with the same layout (settings.yaml, principles.yaml,
topologies/, scenarios/, references/).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from network_lab_mcp import terminal

# The production tmux socket managed sessions/Discovery/monitor use in real
# operation. Tests must never list, read, send to, or close sessions on
# this socket -- only the isolated one below.
PRODUCTION_TMUX_SOCKET_NAME = terminal.TMUX_SOCKET_NAME

# Most of this suite deliberately validates against a real (isolated) tmux
# server rather than mocking session mechanics away -- see "Local terminal
# validation policy" in README.md. When `tmux` genuinely is not installed,
# every one of those tests fails at the same single, well-defined boundary:
# `terminal._tmux_base()` raising this exact TerminalError message. Rather
# than either (a) leaving that as a confusing raw failure/fixture-teardown
# error for every tmux-dependent test, or (b) trying to guess in advance
# which test files are "pure" and skip them preemptively (which risks
# hiding a genuinely broken test behind a wrong guess), this hook narrowly
# reclassifies exactly that one sentinel exception as a skip, in whichever
# phase (setup/call/teardown) it surfaces. A pure unit test that never
# touches terminal.py is completely unaffected and still runs normally;
# any *other* exception is reported exactly as it always was.
_TMUX_MISSING_MESSAGE = "The 'tmux' binary is not available on this system."


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if call.excinfo is not None and _TMUX_MISSING_MESSAGE in str(call.excinfo.value):
        report = outcome.get_result()
        report.outcome = "skipped"
        # pytest's own "-rs"/folded-skip summary requires a skipped report's
        # longrepr to be exactly this (path, lineno, reason) tuple shape --
        # the same shape a real pytest.skip() call produces internally.
        report.longrepr = (
            str(item.fspath),
            item.location[1],
            f"Skipped: {_TMUX_MISSING_MESSAGE} (tmux is not installed)",
        )


@pytest.fixture(scope="session", autouse=True)
def _isolated_tmux_socket():
    """Redirect every test in the suite onto its own, disposable tmux
    server for the whole pytest run -- ordinary pytest must never be able
    to list, read, send to, or close a real production managed session
    (or a Discovery/validation session) on `network-lab-mcp`, the socket
    real operation uses.

    `terminal._tmux_base()` reads `terminal.TMUX_SOCKET_NAME` as a plain
    module-global lookup at call time (not a frozen default parameter), so
    reassigning the module attribute here is sufficient -- every existing
    tmux invocation throughout terminal.py picks up the new socket name
    with no further changes needed anywhere else.

    Session-scoped (one shared test socket for the whole run, not one per
    test): tests already rely on per-file/per-test cleanup fixtures to
    avoid session-name collisions *within* a socket, and spinning up a
    fresh tmux server per test would add real overhead across a suite
    this size for no isolation benefit (pytest-xdist is not in use, so
    tests never actually run concurrently against each other).

    Real-lab-gated tests (`NETWORK_LAB_REAL_TESTS=1`) are unaffected in
    substance: they still open real ssh/telnet sessions to real devices --
    only the *local* tmux socket multiplexing those sessions is
    redirected, which has no bearing on which remote device is reached."""
    test_socket_name = f"network-lab-mcp-test-{os.getpid()}"
    terminal.TMUX_SOCKET_NAME = test_socket_name
    yield
    terminal.TMUX_SOCKET_NAME = PRODUCTION_TMUX_SOCKET_NAME
    if shutil.which("tmux") is not None:
        subprocess.run(["tmux", "-L", test_socket_name, "kill-server"], capture_output=True)


@pytest.fixture()
def production_tmux_socket_name() -> str:
    """The real production tmux socket name -- for the one test file
    (test_tmux_socket_isolation.py) that must deliberately create a *fake*
    session directly on it (bypassing terminal.py, via a raw `tmux`
    invocation) to prove ordinary pytest never touches it."""
    return PRODUCTION_TMUX_SOCKET_NAME


def _write_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)


@pytest.fixture()
def lab_root(tmp_path: Path) -> Path:
    root = tmp_path / "lab"

    _write_yaml(
        root / "settings.yaml",
        {
            "active_access_info": "sample_lab",
            "active_topology": "sample_lab",
            "active_scenario": "sample",
            "active_references": ["sample"],
        },
    )
    _write_yaml(root / "principles.yaml", {"general_operating_principles": ["Inspect before changing."]})
    _write_yaml(
        root / "topologies" / "sample_lab.yaml",
        {
            "name": "sample_lab",
            "description": "Sample lab used for tests.",
            "devices": {
                "R1": {"type": "iosxr"},
                "R2": {"type": "iosxr"},
            },
            "links": [{"a": "R1", "b": "R2"}],
        },
    )
    _write_yaml(
        root / "access-info" / "sample_lab.yaml",
        {
            "name": "sample_lab",
            "jump_hosts": {
                "jump1": {
                    "type": "host",
                    "address": "192.0.2.10",
                    "transport": "ssh",
                    "port": 22,
                    "username": "example-user",
                    "password": "example-password",
                },
            },
            "devices": {
                "R1": {
                    "type": "iosxr",
                    "address": "192.0.2.11",
                    "transport": "ssh",
                    "port": 22,
                    "username": "example-user",
                    "password": "example-password",
                },
                "R2": {
                    "type": "iosxr",
                    "address": "192.0.2.12",
                    "transport": "ssh",
                    "port": 22,
                },
            },
        },
    )
    _write_yaml(
        root / "scenarios" / "sample.yaml",
        {"name": "sample", "description": "Sample scenario.", "objectives": ["do the thing"]},
    )
    _write_yaml(
        root / "scenarios" / "failover_test.yaml",
        {"name": "failover_test", "description": "Failover scenario."},
    )
    _write_yaml(
        root / "references" / "sample.yaml",
        {"name": "sample", "description": "Sample reference.", "guidance": ["do it well"]},
    )
    _write_yaml(
        root / "references" / "iosxr_basics.yaml",
        {"name": "iosxr_basics", "description": "IOS XR basics."},
    )
    return root


@pytest.fixture()
def fake_editor(tmp_path: Path):
    """Build a small, deterministic stand-in for an interactive editor, so
    automated tests never depend on a real interactive vim session.

    Returns a factory `make(body) -> path`: `body` is a Python snippet with
    `path` bound to the temp YAML file's location (as a string); the
    factory writes it into a standalone executable script and returns its
    path, suitable for $VISUAL/$EDITOR."""

    def make(body: str) -> Path:
        script = tmp_path / f"fake_editor_{len(list(tmp_path.glob('fake_editor_*')))}.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "path = sys.argv[-1]\n"
            f"{body}\n",
            encoding="utf-8",
        )
        script.chmod(0o700)
        return script

    return make
