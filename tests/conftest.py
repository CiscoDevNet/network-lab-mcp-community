# SPDX-License-Identifier: Apache-2.0
"""Shared pytest fixtures: an isolated lab/ directory tree per test, and an
isolated tmux socket for the whole test session.

Tests never touch the real repository lab/ directory; each test gets its own
temporary lab root with the same layout (settings.yaml, principles.yaml,
topologies/, scenarios/, references/).
"""

from __future__ import annotations

import os
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
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


def _wait_until_tmux_server_gone(socket_name: str, timeout: float = 10.0) -> bool:
    """True once no tmux server answers on `socket_name` (polled, with a
    timeout). `kill-server` returning only means the request was accepted:
    server exit is observed separately, via a client connection failing."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = subprocess.run(["tmux", "-L", socket_name, "list-sessions"], capture_output=True, text=True)
        if probe.returncode != 0:
            return True
        time.sleep(0.05)
    return False


def _remove_owned_tmux_tmpdir(tmpdir: str, created_real_path: str, created_inode: int) -> None:
    """Remove the tmux directory this test session itself created, and only
    that: the real path, owner, type and inode must still match what was
    recorded at creation, and the tree must hold nothing but the tmux
    socket directory and Unix sockets. Anything unexpected is left in place
    rather than deleted. tmux never unlinks its socket file when a server
    exits (stale socket files are normal tmux behavior on macOS and Linux),
    so removing the whole private directory is the only complete cleanup."""
    real = os.path.realpath(tmpdir)
    if real != created_real_path or not os.path.isdir(real) or os.path.islink(tmpdir):
        return
    st = os.lstat(real)
    if st.st_uid != os.getuid() or st.st_ino != created_inode:
        return
    if not shutil.rmtree.avoids_symlink_attacks:
        return
    for dirpath, dirnames, filenames in os.walk(real):
        for name in dirnames + filenames:
            mode = os.lstat(os.path.join(dirpath, name)).st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISSOCK(mode)):
                return
    shutil.rmtree(real)


@pytest.fixture(scope="session", autouse=True)
def _isolated_tmux_socket():
    """Redirect every test in the suite onto its own, disposable tmux
    server for the whole pytest run -- ordinary pytest must never be able
    to list, read, send to, or close a real production managed session
    (or a Discovery/validation session) on `network-lab-mcp`, the socket
    real operation uses.

    Two independent layers:

    * Socket name: `terminal._tmux_base()` reads `terminal.TMUX_SOCKET_NAME`
      as a plain module-global lookup at call time, so reassigning it here
      redirects every tmux invocation in terminal.py. The name carries a
      random token, not just the PID, so it is never reused.
    * Directory: `TMUX_TMPDIR` points at a private, freshly created
      directory, so even a socket name that equals the production one
      (test_tmux_socket_isolation.py) resolves *inside* that directory and
      cannot reach the real `/tmp/tmux-<uid>/network-lab-mcp`. `TMUX` and
      `TMUX_PANE` are removed so a developer running pytest from inside
      their own tmux session cannot be addressed by any child process.
      Because the directory is private, teardown can remove the leftover
      socket file as well as the server.

    Session-scoped (one shared test socket for the whole run, not one per
    test): see the per-file cleanup fixtures for in-socket isolation.

    Real-lab-gated tests (`NETWORK_LAB_REAL_TESTS=1`) are unaffected in
    substance: they still open real ssh/telnet sessions to real devices --
    only the *local* tmux socket multiplexing those sessions is
    redirected, which has no bearing on which remote device is reached."""
    # /tmp (not the platform default under /var/folders on macOS): keeps
    # the Unix socket path well under the ~104-byte sun_path limit.
    tmpdir = tempfile.mkdtemp(prefix="nlm-", dir="/tmp")
    os.chmod(tmpdir, 0o700)
    created_real_path = os.path.realpath(tmpdir)
    created_inode = os.lstat(created_real_path).st_ino
    saved_env = {key: os.environ.get(key) for key in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR")}
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_PANE", None)
    os.environ["TMUX_TMPDIR"] = created_real_path

    test_socket_name = f"network-lab-mcp-test-{os.getpid()}-{secrets.token_hex(4)}"
    terminal.TMUX_SOCKET_NAME = test_socket_name
    try:
        yield
    finally:
        terminal.TMUX_SOCKET_NAME = PRODUCTION_TMUX_SOCKET_NAME
        server_gone = True
        if shutil.which("tmux") is not None:
            subprocess.run(["tmux", "-L", test_socket_name, "kill-server"], capture_output=True)
            server_gone = _wait_until_tmux_server_gone(test_socket_name)
        if server_gone:
            _remove_owned_tmux_tmpdir(tmpdir, created_real_path, created_inode)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(autouse=True)
def _isolated_terminal_logs_root(monkeypatch, tmp_path_factory):
    """Point `terminal.LOGS_ROOT` at a per-test temporary directory so no
    test can write session logs into the repository's own `logs/`. Tests
    that need a specific location still monkeypatch it themselves (their
    own setattr runs later and wins)."""
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path_factory.mktemp("terminal-logs"))


@pytest.fixture(autouse=True)
def _no_real_tofu_probe_without_isolated_runtime():
    """TOFU opens a real ssh connection to the device's address. Unit tests
    use fake devices (documentation addresses) and must never reach the
    network, so the managed-terminal TOFU hook is a no-op unless the test
    installed the isolated OpenSSH runtime (loopback sshd, forced wrapper) --
    the tests that exercise TOFU for real do exactly that. Real-lab runs
    (`NETWORK_LAB_REAL_TESTS=1`, explicitly approved by the user) keep it."""
    if os.environ.get("NETWORK_LAB_REAL_TESTS") == "1":
        yield
        return
    from network_lab_mcp import ssh_isolation

    real = terminal._ensure_host_keys

    def guarded(device_name, device_config):
        if ssh_isolation.active_runtime() is None:
            return None
        return real(device_name, device_config)

    patch = pytest.MonkeyPatch()  # its own instance: a test's blanket monkeypatch.undo() must not remove it
    patch.setattr(terminal, "_ensure_host_keys", guarded)

    # Same for the CLI's Discovery coordinator (it would probe the fake devices' addresses and read the real
    # user's known_hosts). Tests of the coordinator build it themselves or call the unwrapped factory.
    import functools

    from network_lab_mcp.cli import main as climain

    real_factory = climain._make_hostkey_coordinator

    @functools.wraps(real_factory)
    def guarded_factory():
        return None if ssh_isolation.active_runtime() is None else real_factory()

    patch.setattr(climain, "_make_hostkey_coordinator", guarded_factory)
    yield
    patch.undo()


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


@pytest.fixture(autouse=True)
def _utf8_locale_validation_stub(monkeypatch):
    """Keep editor tests independent of the host locale: the real libc check
    is exercised only by the UTF-8 locale test modules, which override this
    fixture by name."""
    from network_lab_mcp.cli import utf8_locale

    monkeypatch.setattr(utf8_locale, "validate_utf8_locale", lambda env, timeout=3.0: utf8_locale.LocaleCheck(True, "ok"))


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


@pytest.fixture(scope="session", autouse=True)
def _real_user_ssh_area_untouched():
    """Metadata-only (stat) check that the whole test session never changed
    the real user's ~/.ssh or ~/.ssh/known_hosts. Contents are never read."""
    from network_lab_mcp import ssh_isolation

    before = ssh_isolation.snapshot_real_ssh_area()
    yield
    assert ssh_isolation.snapshot_real_ssh_area() == before, "real ~/.ssh metadata changed during tests"


@pytest.fixture(scope="session", autouse=True)
def _no_orphan_sshd_after_session(tmp_path_factory):
    """After the whole session no loopback sshd started from this session's
    temporary root may remain (only this session's own processes are considered)."""
    yield
    root = str(tmp_path_factory.getbasetemp())
    import subprocess as _sp

    out = _sp.run(["ps", "-axo", "pid,command"], capture_output=True, text=True).stdout
    leftovers = [ln for ln in out.splitlines() if "sshiso/etc/sshd_" in ln and root in ln]
    for ln in leftovers:  # reap (only ours: the command line carries this session's temp root), then fail
        try:
            os.kill(int(ln.split()[0]), 15)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    assert not leftovers, f"orphan sshd left by tests: {len(leftovers)}"
