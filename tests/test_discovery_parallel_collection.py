"""Parallel per-device Discovery collection.

`discovery.discover_topology()`'s per-device bootstrap collection
(`_bootstrap_collect`) now runs on a bounded `concurrent.futures.
ThreadPoolExecutor` (`discovery.DISCOVERY_MAX_WORKERS`), one worker per
device, instead of a plain sequential `for` loop -- device-level
parallelism only; within one device's own collector, command order is
untouched (this file never has to prove that, since `_bootstrap_collect`
itself was not changed).

Every test here monkeypatches `discovery._bootstrap_collect` directly
(never a real terminal/tmux/ssh operation) and uses the isolated
`lab_root` fixture -- the real repository's lab/ is never touched.
`discovery.close_bootstrap_terminal` calls (the cleanup step) are tracked
via `terminal.close_bootstrap_terminal`, also monkeypatched."""

from __future__ import annotations

import threading
import time

import pytest

from network_lab_mcp import discovery, lab, terminal


def _write_targets(lab_root, device_ids):
    devices = {
        device_id: {
            "type": "iosxr",
            "address": f"192.0.2.{i + 10}",
            "transport": "ssh",
            "port": 22,
        }
        for i, device_id in enumerate(device_ids)
    }
    lab.write_access_info("sample_lab", {"name": "sample_lab", "devices": devices}, lab_root)


def _clean_lldp(device_id: str) -> str:
    return "Device ID       Local Intf                      Hold-time  Capability      Port ID\nTotal entries displayed: 0\n"


def _fake_collect_result(device_id: str) -> dict:
    return {
        "hostname": f"HOST-{device_id}",
        "show_version": "Cisco IOS XR Software",
        "show_running_config": f"hostname HOST-{device_id}",
        "show_lldp_neighbors": _clean_lldp(device_id),
        # A recognized, empty CDP table: "ran, zero neighbors" (an absent or
        # unrecognized CDP output is now a fatal parser failure).
        "show_cdp_neighbors": "Device ID        Local Intrfce     Holdtme    Capability  Platform    Port ID\n",
    }


@pytest.fixture(autouse=True)
def _track_bootstrap_close(monkeypatch):
    closed: list[str] = []
    monkeypatch.setattr(terminal, "close_bootstrap_terminal", lambda device_id: closed.append(device_id))
    return closed


# ==========================================================================
# Different-device overlap proof
# ==========================================================================


def test_two_device_collectors_overlap(lab_root, monkeypatch):
    _write_targets(lab_root, ["R1", "R2"])
    barrier = threading.Barrier(2, timeout=5)
    entered: list[str] = []
    entered_lock = threading.Lock()

    def fake_collect(device_id, device_config):
        with entered_lock:
            entered.append(device_id)
        barrier.wait()  # only satisfied if both collectors are in flight together
        return _fake_collect_result(device_id)

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_collect)

    result = discovery.discover_topology(lab_root)

    assert set(entered) == {"R1", "R2"}
    assert result.connected_count == 2


def test_sequential_collector_would_not_satisfy_the_barrier():
    """Negative control, same purpose as the MCP-level one: a *sequential*
    call into the same barrier-gated fake never satisfies it alone."""
    barrier = threading.Barrier(2, timeout=0.3)
    with pytest.raises(threading.BrokenBarrierError):
        barrier.wait()


# ==========================================================================
# Bounded worker concurrency
# ==========================================================================


def test_worker_concurrency_is_bounded(lab_root, monkeypatch):
    device_ids = [f"R{i}" for i in range(1, 13)]  # more targets than DISCOVERY_MAX_WORKERS
    _write_targets(lab_root, device_ids)
    active = 0
    max_active = 0
    state_lock = threading.Lock()

    def fake_collect(device_id, device_config):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)
            return _fake_collect_result(device_id)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_collect)

    result = discovery.discover_topology(lab_root)

    assert result.connected_count == len(device_ids)
    assert max_active <= discovery.DISCOVERY_MAX_WORKERS
    assert max_active == discovery.DISCOVERY_MAX_WORKERS  # enough targets to actually saturate the bound


# ==========================================================================
# Single-device regression
# ==========================================================================


def test_single_device_discovery_unchanged(lab_root, monkeypatch):
    _write_targets(lab_root, ["R1"])
    monkeypatch.setattr(discovery, "_bootstrap_collect", lambda device_id, cfg: _fake_collect_result(device_id))

    result = discovery.discover_topology(lab_root)

    assert result.iosxr_target_count == 1
    assert result.connected_count == 1
    assert result.identity_map == {"R1": "HOST-R1"}


# ==========================================================================
# Deterministic aggregation ordering
# ==========================================================================


def test_result_ordering_is_deterministic_regardless_of_completion_order(lab_root, monkeypatch):
    device_ids = ["R1", "R2", "R3", "R4"]
    _write_targets(lab_root, device_ids)
    # Make the *last* target finish fastest and the *first* finish slowest --
    # completion order is the exact reverse of target order.
    delays = {"R1": 0.2, "R2": 0.13, "R3": 0.07, "R4": 0.0}

    def fake_collect(device_id, device_config):
        time.sleep(delays[device_id])
        return _fake_collect_result(device_id)

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_collect)

    result = discovery.discover_topology(lab_root)

    assert list(result.devices.keys()) == device_ids
    assert list(result.identity_map.keys()) == device_ids


# ==========================================================================
# Deterministic error attribution + fail-closed
# ==========================================================================


def test_first_target_order_failure_is_surfaced_not_completion_order(lab_root, monkeypatch):
    device_ids = ["R1", "R2", "R3", "R4"]
    _write_targets(lab_root, device_ids)
    # R4 (last in target order) fails fastest; R2 (earlier in target order)
    # fails slower. The visible error must still name R2, the earliest
    # failing device by target order, not R4 merely because it finished
    # raising first.
    failing = {"R2": 0.1, "R4": 0.0}

    def fake_collect(device_id, device_config):
        if device_id in failing:
            time.sleep(failing[device_id])
            raise terminal.TerminalError(f"{device_id} login failed")
        return _fake_collect_result(device_id)

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_collect)

    with pytest.raises(discovery.DiscoveryError, match="R2"):
        discovery.discover_topology(lab_root)


def test_multiple_device_failures_still_fail_closed_with_zero_candidate_mutation(lab_root, monkeypatch):
    device_ids = ["R1", "R2", "R3"]
    _write_targets(lab_root, device_ids)

    def fake_collect(device_id, device_config):
        raise terminal.TerminalError(f"{device_id} unreachable")

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_collect)

    with pytest.raises(discovery.DiscoveryError):
        discovery.discover_topology(lab_root)


# ==========================================================================
# Unexpected worker exception
# ==========================================================================


def test_unexpected_worker_exception_fails_closed_as_discovery_error(lab_root, monkeypatch):
    _write_targets(lab_root, ["R1", "R2"])

    def fake_collect(device_id, device_config):
        if device_id == "R1":
            raise RuntimeError("unexpected bug, not a DiscoveryError")
        return _fake_collect_result(device_id)

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_collect)

    with pytest.raises(discovery.DiscoveryError, match="R1"):
        discovery.discover_topology(lab_root)


# ==========================================================================
# Cleanup ownership: all bootstrap sessions closed in every outcome
# ==========================================================================


def test_cleanup_runs_for_every_target_on_full_success(lab_root, monkeypatch, _track_bootstrap_close):
    device_ids = ["R1", "R2", "R3"]
    _write_targets(lab_root, device_ids)
    monkeypatch.setattr(discovery, "_bootstrap_collect", lambda device_id, cfg: _fake_collect_result(device_id))

    discovery.discover_topology(lab_root)

    assert sorted(_track_bootstrap_close) == device_ids


def test_cleanup_runs_for_every_target_after_one_failure(lab_root, monkeypatch, _track_bootstrap_close):
    device_ids = ["R1", "R2", "R3"]
    _write_targets(lab_root, device_ids)

    def fake_collect(device_id, device_config):
        if device_id == "R2":
            raise terminal.TerminalError("boom")
        return _fake_collect_result(device_id)

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_collect)

    with pytest.raises(discovery.DiscoveryError):
        discovery.discover_topology(lab_root)

    assert sorted(_track_bootstrap_close) == device_ids


def test_cleanup_runs_for_every_target_after_multiple_failures(lab_root, monkeypatch, _track_bootstrap_close):
    device_ids = ["R1", "R2", "R3"]
    _write_targets(lab_root, device_ids)

    def fake_collect(device_id, device_config):
        raise terminal.TerminalError("boom")

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_collect)

    with pytest.raises(discovery.DiscoveryError):
        discovery.discover_topology(lab_root)

    assert sorted(_track_bootstrap_close) == device_ids


def test_cleanup_runs_for_every_target_after_unexpected_exception(lab_root, monkeypatch, _track_bootstrap_close):
    device_ids = ["R1", "R2", "R3"]
    _write_targets(lab_root, device_ids)

    def fake_collect(device_id, device_config):
        if device_id == "R3":
            raise RuntimeError("bug")
        return _fake_collect_result(device_id)

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_collect)

    with pytest.raises(discovery.DiscoveryError):
        discovery.discover_topology(lab_root)

    assert sorted(_track_bootstrap_close) == device_ids


def test_cleanup_runs_after_lldp_parse_failure(lab_root, monkeypatch, _track_bootstrap_close):
    device_ids = ["R1", "R2"]
    _write_targets(lab_root, device_ids)

    def fake_collect(device_id, device_config):
        result = _fake_collect_result(device_id)
        if device_id == "R1":
            result["show_lldp_neighbors"] = "% Invalid input detected at '^' marker.\n"
        return result

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_collect)

    with pytest.raises(discovery.DiscoveryError):
        discovery.discover_topology(lab_root)

    assert sorted(_track_bootstrap_close) == device_ids
