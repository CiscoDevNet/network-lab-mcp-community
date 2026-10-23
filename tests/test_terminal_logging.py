# SPDX-License-Identifier: Apache-2.0
"""Persistent terminal transcript logging (logs/terminal/<device-id>/*.log).

Uses real tmux sessions (in the isolated network-lab-mcp tmux server, via
the same validation-session namespace other tests use) so pipe-pane
behavior is genuinely exercised, but never a real router -- sessions run a
safe local command instead of ssh/telnet. `terminal.LOGS_ROOT` is
monkeypatched to a temporary directory so nothing here ever touches the
real repository's logs/."""

from __future__ import annotations

import time

import pytest

from network_lab_mcp import terminal


@pytest.fixture()
def isolated_logs(tmp_path, monkeypatch):
    logs_root = tmp_path / "logs" / "terminal"
    monkeypatch.setattr(terminal, "LOGS_ROOT", logs_root)
    return logs_root


@pytest.fixture(autouse=True)
def _no_managed_auth_wait(monkeypatch):
    """This file exercises persistent logging, never SSH authentication --
    `open_device_terminal(..., {"transport": "ssh", ...})` below is only
    ever a convenient stand-in for "some active managed session", against
    a fake, non-routable address (managed-session authentication would
    otherwise poll for the bounded _MANAGED_LOGIN_TIMEOUT_SECONDS on every
    such call for nothing)."""
    monkeypatch.setattr(terminal, "_authenticate_managed_session", lambda *a, **k: None)


def _open_validation(validation_id: str, script: str) -> str:
    result = terminal.open_validation_session(validation_id, ["bash", "-c", script])
    return result["session_name"]


def _wait_until(predicate, timeout=3.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture(autouse=True)
def _cleanup_validation_sessions():
    yield
    for session_name in list(terminal.list_validation_sessions()):
        validation_id = session_name[len(terminal.VALIDATION_PREFIX) :]
        terminal.close_validation_session(validation_id)


def test_pipe_pane_creates_expected_log_file_and_captures_output(isolated_logs):
    # A small delay before the first output mirrors a real SSH connection's
    # handshake time, giving pipe-pane a realistic window to attach before
    # anything is printed (tmux pipe-pane never replays pane history).
    session_name = _open_validation("logtest-basic", "sleep 0.3; echo HELLO-LOG; sleep 2")
    terminal._start_session_logging(session_name, "R9")

    log_dir = isolated_logs / "R9"
    assert _wait_until(lambda: log_dir.is_dir() and any(log_dir.iterdir()))

    files = list(log_dir.iterdir())
    assert len(files) == 1
    assert terminal._LOG_FILENAME_RE.match(files[0].name)

    assert _wait_until(lambda: "HELLO-LOG" in files[0].read_text())

    terminal.close_validation_session("logtest-basic")


def test_multiple_devices_get_separate_log_directories(isolated_logs):
    s1 = _open_validation("logtest-multi-a", "sleep 2")
    s2 = _open_validation("logtest-multi-b", "sleep 2")
    terminal._start_session_logging(s1, "R9")
    terminal._start_session_logging(s2, "R10")

    assert _wait_until(lambda: list((isolated_logs / "R9").glob("*.log")) != [])
    assert _wait_until(lambda: list((isolated_logs / "R10").glob("*.log")) != [])

    terminal.close_validation_session("logtest-multi-a")
    terminal.close_validation_session("logtest-multi-b")


def test_reused_session_does_not_start_a_second_log_file(isolated_logs, monkeypatch):
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["cat"]))
    terminal.open_device_terminal("R9", {"transport": "ssh", "address": "192.0.2.1"})
    terminal.open_device_terminal("R9", {"transport": "ssh", "address": "192.0.2.1"})  # reused

    log_dir = isolated_logs / "R9"
    assert _wait_until(lambda: list(log_dir.glob("*.log")) != [])
    assert len(list(log_dir.iterdir())) == 1

    terminal.close_device_terminal("R9")


# ---- logging attaches before the transport command's earliest output
# (see terminal._create_logged_session()) ----


def test_logging_attached_before_earliest_immediate_output(isolated_logs, monkeypatch):
    # No startup delay at all -- the banner is echoed the instant the
    # pane's process runs, stress-testing the exact race this guards
    # against (pipe-pane must already be attached before this line runs).
    monkeypatch.setattr(
        terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["echo", "IMMEDIATE-BANNER"])
    )
    terminal.open_device_terminal("R9", {"transport": "ssh", "address": "192.0.2.1"})

    log_dir = isolated_logs / "R9"
    assert _wait_until(lambda: log_dir.is_dir() and any(log_dir.iterdir()))
    files = list(log_dir.iterdir())
    assert len(files) == 1
    assert _wait_until(lambda: "IMMEDIATE-BANNER" in files[0].read_text())

    terminal.close_device_terminal("R9")


def test_later_output_still_reaches_the_log_after_the_banner(isolated_logs, monkeypatch):
    monkeypatch.setattr(
        terminal,
        "_build_transport_command",
        lambda config, **kw: ("ssh", ["bash", "-c", "echo BANNER; sleep 0.3; echo LATER-OUTPUT"]),
    )
    terminal.open_device_terminal("R9", {"transport": "ssh", "address": "192.0.2.1"})

    log_dir = isolated_logs / "R9"
    assert _wait_until(lambda: log_dir.is_dir() and any(log_dir.iterdir()))
    log_file = next(iter(log_dir.iterdir()))
    assert _wait_until(lambda: "BANNER" in log_file.read_text())
    assert _wait_until(lambda: "LATER-OUTPUT" in log_file.read_text())

    terminal.close_device_terminal("R9")


def test_discovery_bootstrap_logging_also_attaches_before_earliest_output(isolated_logs, monkeypatch):
    monkeypatch.setattr(
        terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["echo", "BOOTSTRAP-IMMEDIATE"])
    )
    terminal.open_bootstrap_terminal("R9", {"transport": "ssh", "address": "192.0.2.1"})

    log_dir = isolated_logs / "R9"
    assert _wait_until(lambda: log_dir.is_dir() and any(log_dir.iterdir()))
    files = list(log_dir.iterdir())
    assert len(files) == 1
    assert _wait_until(lambda: "BOOTSTRAP-IMMEDIATE" in files[0].read_text())

    terminal.close_bootstrap_terminal("R9")


def test_normal_terminal_session_still_works_end_to_end(isolated_logs, monkeypatch):
    """Public terminal_open()/terminal_send()/terminal_read() behavior is
    unaffected by the neutral-shell-then-typed-command logging change."""
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["cat"]))
    result = terminal.open_device_terminal("R9", {"transport": "ssh", "address": "192.0.2.1"})
    assert result["reused"] is False

    terminal.send_to_device("R9", "hello-from-test", None, True)
    assert _wait_until(lambda: "hello-from-test" in terminal.read_device("R9")["content"])

    terminal.close_device_terminal("R9")


def test_failed_connection_cleanup_remains_correct(isolated_logs, monkeypatch):
    """A transport that exits immediately (simulating a failed connection)
    must not leave a broken session/log state; close_device_terminal()
    still cleanly reports closed=True."""
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["false"]))
    terminal.open_device_terminal("R9", {"transport": "ssh", "address": "192.0.2.1"})

    log_dir = isolated_logs / "R9"
    assert _wait_until(lambda: log_dir.is_dir() and any(log_dir.iterdir()))

    result = terminal.close_device_terminal("R9")
    assert result["closed"] is True


def test_terminal_read_behavior_is_unaffected_by_logging(isolated_logs):
    session_name = _open_validation("logtest-read", "cat")
    terminal._start_session_logging(session_name, "R9")
    terminal._send_literal_text(session_name, "ping")
    terminal._send_enter(session_name)

    assert _wait_until(lambda: "ping" in terminal._capture_pane(session_name, 100))

    terminal.close_validation_session("logtest-read")


def test_sent_text_is_not_duplicated_into_a_second_application_log(isolated_logs):
    session_name = _open_validation("logtest-dup", "cat")
    terminal._start_session_logging(session_name, "R9")
    for value in ("one", "two", "three"):
        terminal._send_literal_text(session_name, value)
        terminal._send_enter(session_name)

    log_dir = isolated_logs / "R9"
    assert _wait_until(lambda: log_dir.is_dir() and any(log_dir.iterdir()))
    # Exactly one log file for this device, regardless of how many sends
    # happened on the pane -- there is no separate per-send application log.
    assert len(list(log_dir.iterdir())) == 1

    terminal.close_validation_session("logtest-dup")


# ---- listing / reading (used by `show logging`) ----


def test_list_logged_device_ids_empty_when_no_logs_dir(isolated_logs):
    assert terminal.list_logged_device_ids() == []


def test_list_device_logs_empty_for_unknown_device(isolated_logs):
    assert terminal.list_device_logs("R9") == []


def test_list_device_logs_newest_first(isolated_logs):
    device_dir = isolated_logs / "R9"
    device_dir.mkdir(parents=True)
    (device_dir / "20260101T090000.log").write_text("old\n")
    (device_dir / "20260101T100000.log").write_text("new\n")

    entries = terminal.list_device_logs("R9")
    assert [name for _, name in entries] == ["20260101T100000.log", "20260101T090000.log"]
    assert terminal.list_logged_device_ids() == ["R9"]


def test_read_device_log_returns_exact_content(isolated_logs):
    device_dir = isolated_logs / "R9"
    device_dir.mkdir(parents=True)
    (device_dir / "20260101T090000.log").write_text("transcript content\n")

    assert terminal.read_device_log("R9", "20260101T090000.log") == "transcript content\n"


def test_read_device_log_rejects_unknown_filename(isolated_logs):
    device_dir = isolated_logs / "R9"
    device_dir.mkdir(parents=True)
    (device_dir / "20260101T090000.log").write_text("x")

    with pytest.raises(terminal.TerminalError):
        terminal.read_device_log("R9", "20260101T999999.log")


@pytest.mark.parametrize(
    "traversal",
    ["../../../etc/passwd", "../secret.log", "/etc/passwd", "..\\..\\secret.log"],
)
def test_read_device_log_rejects_path_traversal(isolated_logs, traversal):
    device_dir = isolated_logs / "R9"
    device_dir.mkdir(parents=True)
    (device_dir / "20260101T090000.log").write_text("x")

    with pytest.raises(terminal.TerminalError):
        terminal.read_device_log("R9", traversal)


def test_read_device_log_rejects_invalid_device_name(isolated_logs):
    with pytest.raises(terminal.TerminalError):
        terminal.read_device_log("../etc", "20260101T090000.log")


# ---- same-second logfile collision (terminal._unique_log_path()) ----


def test_unique_log_path_returns_base_name_when_no_collision(tmp_path):
    log_dir = tmp_path / "R9"
    log_dir.mkdir()
    path = terminal._unique_log_path(log_dir, "20260921T091500")
    assert path.name == "20260921T091500.log"


def test_unique_log_path_appends_suffix_on_first_collision(tmp_path):
    log_dir = tmp_path / "R9"
    log_dir.mkdir()
    (log_dir / "20260921T091500.log").write_text("first session\n")
    path = terminal._unique_log_path(log_dir, "20260921T091500")
    assert path.name == "20260921T091500_2.log"


def test_unique_log_path_increments_suffix_for_repeated_collisions(tmp_path):
    log_dir = tmp_path / "R9"
    log_dir.mkdir()
    (log_dir / "20260921T091500.log").write_text("x")
    (log_dir / "20260921T091500_2.log").write_text("x")
    (log_dir / "20260921T091500_3.log").write_text("x")
    path = terminal._unique_log_path(log_dir, "20260921T091500")
    assert path.name == "20260921T091500_4.log"


def test_two_sessions_starting_in_the_same_second_get_separate_log_files(isolated_logs, monkeypatch):
    monkeypatch.setattr(terminal, "_session_start_timestamp", lambda: "20260921T091500")
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["cat"]))

    terminal.open_device_terminal("R9", {"transport": "ssh", "address": "192.0.2.1"})
    terminal.close_device_terminal("R9")
    terminal.open_device_terminal("R9", {"transport": "ssh", "address": "192.0.2.1"})

    log_dir = isolated_logs / "R9"
    assert _wait_until(lambda: len(list(log_dir.glob("*.log"))) == 2)
    names = sorted(p.name for p in log_dir.glob("*.log"))
    assert names == ["20260921T091500.log", "20260921T091500_2.log"]

    terminal.close_device_terminal("R9")


def test_list_device_logs_derives_session_start_from_prefix_for_collision_files(isolated_logs):
    log_dir = isolated_logs / "R9"
    log_dir.mkdir(parents=True)
    (log_dir / "20260921T091500.log").write_text("first\n")
    (log_dir / "20260921T091500_2.log").write_text("second\n")

    entries = terminal.list_device_logs("R9")
    assert {name for _, name in entries} == {"20260921T091500.log", "20260921T091500_2.log"}
    for started, _name in entries:
        assert started.strftime(terminal._SESSION_START_FORMAT) == "20260921T091500"


def test_list_device_logs_orders_same_second_collisions_newest_suffix_first(isolated_logs):
    log_dir = isolated_logs / "R9"
    log_dir.mkdir(parents=True)
    (log_dir / "20260921T091500.log").write_text("first\n")
    (log_dir / "20260921T091500_2.log").write_text("second\n")
    (log_dir / "20260921T091500_3.log").write_text("third\n")

    entries = terminal.list_device_logs("R9")
    assert [name for _, name in entries] == [
        "20260921T091500_3.log",
        "20260921T091500_2.log",
        "20260921T091500.log",
    ]


def test_read_device_log_works_for_collision_suffixed_filename(isolated_logs):
    log_dir = isolated_logs / "R9"
    log_dir.mkdir(parents=True)
    (log_dir / "20260921T091500_2.log").write_text("collision content\n")

    assert terminal.read_device_log("R9", "20260921T091500_2.log") == "collision content\n"
