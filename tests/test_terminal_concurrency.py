# SPDX-License-Identifier: Apache-2.0
"""Per-device terminal concurrency.

Core invariant under test: different devices execute concurrently; the
same device's operations are serialized through one lock per underlying
tmux session name (terminal._session_lock()). Uses real tmux sessions (the
isolated network-lab-mcp socket, same as test_terminal_logging.py /
test_terminal_stale_prompt.py) so the actual check-then-act session
creation path is genuinely exercised -- never a real router:
`_build_transport_command` is monkeypatched to a safe local command
instead of ssh/telnet, so no network binary or connectivity is required.

Never touches the real repository's logs/ (LOGS_ROOT is monkeypatched to
an isolated temp directory, same convention as test_terminal_logging.py)."""

from __future__ import annotations

import threading
import time

import pytest

from network_lab_mcp import terminal


@pytest.fixture(autouse=True)
def isolated_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")


@pytest.fixture(autouse=True)
def _fake_transport(monkeypatch):
    # A safe local command in place of real ssh/telnet -- proves the
    # session-management/locking path without any network dependency.
    monkeypatch.setattr(
        terminal,
        "_build_transport_command",
        lambda cfg, **kwargs: ("ssh", ["bash", "-c", "sleep 5"]),
    )


@pytest.fixture(autouse=True)
def _cleanup_device_sessions():
    yield
    for session in terminal.list_device_sessions():
        terminal.close_device_terminal(session["device"])


def _open(device: str) -> dict:
    return terminal.open_device_terminal(device, {})


# ==========================================================================
# Same-device serialization
# ==========================================================================


def test_same_device_operations_never_overlap():
    """Two concurrent open_device_terminal("R1") calls must never both be
    inside _ensure_managed_session() at once -- proven by a high-water-mark
    concurrency counter around the real function, not by timing alone. A
    small sleep widens the race window so a real regression would reliably
    be caught, not just proven correct by luck."""
    active = 0
    max_active = 0
    state_lock = threading.Lock()
    real_ensure = terminal._ensure_managed_session

    def tracked(*args, **kwargs):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.1)
            return real_ensure(*args, **kwargs)
        finally:
            with state_lock:
                active -= 1

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "_ensure_managed_session", tracked)
        threads = [threading.Thread(target=_open, args=("R1",)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

    assert max_active == 1


def test_different_devices_max_concurrency_exceeds_one():
    """The same instrumentation as above, but for two *different* devices --
    proves the lock is per-device, not a hidden global lock: both calls are
    allowed to be inside _ensure_managed_session() at the same time."""
    active = 0
    max_active = 0
    state_lock = threading.Lock()
    real_ensure = terminal._ensure_managed_session

    def tracked(*args, **kwargs):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.1)
            return real_ensure(*args, **kwargs)
        finally:
            with state_lock:
                active -= 1

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "_ensure_managed_session", tracked)
        threads = [
            threading.Thread(target=_open, args=("R1",)),
            threading.Thread(target=_open, args=("R2",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

    assert max_active > 1


# ==========================================================================
# Open-vs-open same-device idempotency
# ==========================================================================


def test_concurrent_open_same_device_creates_exactly_one_session():
    results: list[dict] = []
    results_lock = threading.Lock()

    def run():
        result = _open("R1")
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 2
    reused_flags = sorted(r["reused"] for r in results)
    assert reused_flags == [False, True]  # exactly one creator, one reuser
    sessions = [s for s in terminal.list_device_sessions() if s["device"] == "R1"]
    assert len(sessions) == 1


# ==========================================================================
# Open different devices concurrently
# ==========================================================================


def test_open_different_devices_concurrently_both_succeed():
    results: dict[str, dict] = {}

    def run(device):
        results[device] = _open(device)

    threads = [threading.Thread(target=run, args=(d,)) for d in ("R1", "R2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert set(results) == {"R1", "R2"}
    devices = {s["device"] for s in terminal.list_device_sessions()}
    assert {"R1", "R2"} <= devices


# ==========================================================================
# Cross-device isolation
# ==========================================================================


def test_send_routes_to_the_correct_devices_own_session():
    sent: list[tuple[str, str]] = []
    real_send = terminal._send_literal_text

    def tracked_send(session_name, text):
        sent.append((session_name, text))
        return real_send(session_name, text)

    _open("R1")
    _open("R2")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "_send_literal_text", tracked_send)
        terminal.send_to_device("R1", "show version", None, False)
        terminal.send_to_device("R2", "show route", None, False)

    assert ("network-lab-device-R1", "show version") in sent
    assert ("network-lab-device-R2", "show route") in sent
    # Neither device's text was ever sent to the other's session.
    assert ("network-lab-device-R1", "show route") not in sent
    assert ("network-lab-device-R2", "show version") not in sent


# ==========================================================================
# Close-vs-operation race
# ==========================================================================


def test_close_and_read_same_device_do_not_race():
    _open("R1")
    errors: list[Exception] = []

    def do_read():
        try:
            terminal.read_device("R1")
        except terminal.TerminalError:
            pass  # acceptable: session may already be closed -- fails closed, not corrupted
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def do_close():
        try:
            terminal.close_device_terminal("R1")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=do_read), threading.Thread(target=do_close)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert errors == []  # no uncaught/unexpected exception either way


def test_close_and_send_same_device_do_not_race():
    _open("R1")
    errors: list[Exception] = []

    def do_send():
        try:
            terminal.send_to_device("R1", "show version", None, True)
        except terminal.TerminalError:
            pass
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def do_close():
        try:
            terminal.close_device_terminal("R1")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=do_send), threading.Thread(target=do_close)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert errors == []


# ==========================================================================
# Lock release on exception
# ==========================================================================


def test_lock_is_released_after_an_exception():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            terminal,
            "_ensure_managed_session",
            lambda *a, **k: (_ for _ in ()).throw(terminal.TerminalError("boom")),
        )
        with pytest.raises(terminal.TerminalError):
            _open("R1")

    # The lock must not still be held -- a subsequent call for the same
    # device must succeed immediately rather than hanging.
    result = _open("R1")
    assert result["device"] == "R1"
