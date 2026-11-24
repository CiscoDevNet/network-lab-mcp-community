# SPDX-License-Identifier: Apache-2.0
"""Enable authentication inside Topology Discovery: real tmux + synthetic
IOS/IOS XE devices (D-series IDs in names), plus deterministic cancellation
tests with fake collectors. Credentials are synthetic sentinels."""

from __future__ import annotations

import threading

import pytest

from network_lab_mcp import discovery, lab, terminal
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain

import enable_support as dev

PW_A = "SENT-DISC-A-pw-31"
PW_B = "SENT-DISC-B-pw-52"
NAME = "fresh_lab"


@pytest.fixture(autouse=True)
def _env(lab_root, tmp_path, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")
    monkeypatch.setattr(discovery, "LOGIN_TIMEOUT_SECONDS", 6)
    monkeypatch.setattr(discovery, "COMMAND_TIMEOUT_SECONDS", 6)
    monkeypatch.setattr(terminal, "ENABLE_STAGE_TIMEOUT_SECONDS", 4)
    monkeypatch.setattr(terminal, "ENABLE_TOTAL_TIMEOUT_SECONDS", 8)
    yield
    for device_id in terminal.list_discovery_device_ids():
        terminal.close_bootstrap_terminal(device_id)


def _setup(lab_root, monkeypatch, devices):
    """devices: {id: (type, enable_password|None, argv)}; transport is mapped
    by address -> argv, so nothing ever leaves the machine."""
    by_address = {}
    entries = {}
    for i, (device_id, (dtype, enable_pw, argv)) in enumerate(devices.items()):
        address = f"192.0.2.{10 + i}"
        by_address[address] = argv
        entry = {"address": address, "transport": "ssh", "password": "SENT-LOGIN-unused", "type": dtype}
        if enable_pw is not None:
            entry["enable_password"] = enable_pw
        entries[device_id] = entry
    lab.write_access_info(NAME, {"name": NAME, "devices": entries}, lab_root)
    settings = lab.read_settings(lab_root)
    settings["active_access_info"] = NAME
    lab.write_settings(settings, lab_root)
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", by_address[config["address"]]))


def _discover(lab_root):
    return discovery.discover_topology(lab_root)


def _outcome(result, device_id):
    return next(o for o in result.outcomes if o.device_id == device_id)


def _sentinel_free(result):
    text = repr(result.warnings)
    return PW_A not in text and PW_B not in text


def test_d01_d03_d13_d14_d21_d27_success_and_unconfigured_paths(lab_root, monkeypatch, tmp_path):
    a = dev.write_device(tmp_path, "a", host="SWA", secret=PW_A)
    b = dev.write_device(tmp_path, "b", host="SWB", secret=PW_B, mode="nopass")
    c = dev.write_device(tmp_path, "c", host="SWC", start_priv=True)
    d = dev.write_device(tmp_path, "d", host="SWD", secret="x")  # user EXEC, no enable_password configured
    _setup(lab_root, monkeypatch, {"SWA": ("iosxe", PW_A, a), "SWB": ("ios", PW_B, b), "SWC": ("iosxe", None, c), "SWD": ("iosxe", None, d)})
    result = _discover(lab_root)
    assert all(_outcome(result, x).connection == "connected" for x in ("SWA", "SWB", "SWC", "SWD"))
    assert (dev.count(a, "enable"), dev.count(a, "secret_ok"), dev.count(a, "secret_bad")) == (1, 1, 0)  # D27: own password
    assert (dev.count(b, "enable"), dev.count(b, "secret_")) == (1, 0)  # D13: no password requested
    assert dev.count(c, "enable") == 0 and dev.count(d, "enable") == 0  # D14/D21: no new attempt
    assert dev.count(a, "other:terminal length 0") == 1 and dev.count(a, "other:show version") == 1
    assert terminal.list_discovery_device_ids() == []  # sessions closed
    assert _sentinel_free(result)


def test_d06_d07_d22_d23_d25_d28_enable_failure_is_partial_for_that_device_only(lab_root, monkeypatch, tmp_path):
    bad = dev.write_device(tmp_path, "bad", host="SWA", secret="SENT-DISC-right-pw", mode="reject")
    good = dev.write_device(tmp_path, "good", host="SWB", secret=PW_B)
    _setup(lab_root, monkeypatch, {"SWA": ("iosxe", PW_A, bad), "SWB": ("iosxe", PW_B, good)})
    result = _discover(lab_root)
    a, b = _outcome(result, "SWA"), _outcome(result, "SWB")
    assert a.connection == "connected" and a.lldp == "unknown" and a.cdp == "unknown"  # D24/D25
    assert b.connection == "connected" and dev.count(good, "secret_ok") == 1  # D07
    assert "SWA: Privileged EXEC authentication failed." in result.warnings
    assert dev.count(bad, "enable") == 1 and dev.count(bad, "secret_") == 1  # D28: no retry
    assert dev.count(bad, "other:") == 0  # D22: no discovery command after failure
    assert terminal.list_discovery_device_ids() == []  # D23
    assert _sentinel_free(result)


def test_d06_timeout_warning(lab_root, monkeypatch, tmp_path):
    silent = dev.write_device(tmp_path, "s", host="SWA", mode="silent")
    _setup(lab_root, monkeypatch, {"SWA": ("iosxe", PW_A, silent)})
    result = _discover(lab_root)
    assert "SWA: Timed out while waiting for privileged exec prompt." in result.warnings
    assert dev.count(silent, "other:") == 0


@pytest.mark.parametrize("mode", ["echo_full", "echo_split", "echo_then_exit", "echo_with_prompt"])
def test_d33_s23_echo_is_fatal_for_the_whole_run(lab_root, monkeypatch, tmp_path, mode):
    echo = dev.write_device(tmp_path, "e", host="SWA", secret=PW_A, mode=mode)
    ok = dev.write_device(tmp_path, "o", host="SWB", secret=PW_B)
    _setup(lab_root, monkeypatch, {"SWA": ("iosxe", PW_A, echo), "SWB": ("iosxe", PW_B, ok)})
    with pytest.raises(discovery.DiscoveryError) as info:
        _discover(lab_root)
    assert "Unexpected credential echo during authentication." in str(info.value)
    assert PW_A not in str(info.value) and info.value.__cause__ is None
    assert dev.count(echo, "secret_") == 1 and dev.count(echo, "other:") == 0
    assert terminal.list_discovery_device_ids() == []


def test_d16_d30_fatal_error_leaves_candidate_untouched(lab_root, monkeypatch, tmp_path, capsys):
    echo = dev.write_device(tmp_path, "e", host="SWA", secret=PW_A, mode="echo_full")
    _setup(lab_root, monkeypatch, {"SWA": ("iosxe", PW_A, echo)})
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    before = repr(session.definition_candidate)
    ok = climain.execute_command_line(session, "discover topology")
    out = capsys.readouterr().out
    assert repr(session.definition_candidate) == before
    assert PW_A not in out and "Unexpected credential echo during authentication." in out


# ---------------- cancellation: deterministic, no sleeps for ordering ----------------


def test_run_control_gate_is_ordered():
    run = discovery._RunControl()
    run.admit()
    run.cancel()
    for _ in range(3):
        with pytest.raises(discovery._RunCancelled):
            run.admit()
    assert (run.admitted, run.refused) == (1, 3)


def _fake_run(monkeypatch, lab_root, tmp_path, fatal):
    """Three devices, three workers. A raises `fatal` once B and C are both
    inside their collectors; B and C only continue after they *observe* the
    shared cancellation, then try to start a credential send."""
    ready = threading.Barrier(3, timeout=20)
    seen = {}

    def collector_a(device_id, cfg, include_l3=False):
        ready.wait()
        raise fatal

    def make_waiter(name):
        def collector(device_id, cfg, include_l3=False):
            ready.wait()
            # the other worker is still running (we have not returned), yet the
            # cancellation must become visible to us
            for _ in range(2000):
                if discovery._ACTIVE_RUN.cancelled:
                    break
                threading.Event().wait(0.005)
            seen[name] = {"cancelled_seen": discovery._ACTIVE_RUN.cancelled}
            try:
                discovery._admit_credential_send(device_id)
                seen[name]["send_started"] = True
            except discovery._RunCancelled:
                seen[name]["send_started"] = False
            return {"hostname": f"HOST-{name}"}

        return collector

    monkeypatch.setattr(discovery, "_bootstrap_collect", lambda d, c, include_l3=False: {"A": collector_a, "B": make_waiter("B"), "C": make_waiter("C")}[d](d, c))
    monkeypatch.setattr(discovery, "DISCOVERY_MAX_WORKERS", 3)
    monkeypatch.setattr(terminal, "close_bootstrap_terminal", lambda d: True)
    entries = {x: {"address": f"192.0.2.{i + 10}", "transport": "ssh", "type": "iosxr"} for i, x in enumerate("ABC")}
    lab.write_access_info(NAME, {"name": NAME, "devices": entries}, lab_root)
    settings = lab.read_settings(lab_root)
    settings["active_access_info"] = NAME
    lab.write_settings(settings, lab_root)
    return seen


def test_d31_d32_s41_fatal_cancellation_blocks_new_sends(lab_root, monkeypatch, tmp_path):
    seen = _fake_run(monkeypatch, lab_root, tmp_path, discovery.DiscoveryError("Device 'A': host key problem"))
    with pytest.raises(discovery.DiscoveryError, match="host key problem"):
        _discover(lab_root)
    assert seen["B"] == {"cancelled_seen": True, "send_started": False}
    assert seen["C"] == {"cancelled_seen": True, "send_started": False}  # new send starts after cancel: 0


def test_d34_s16_keyboard_interrupt_blocks_new_sends(lab_root, monkeypatch, tmp_path):
    seen = _fake_run(monkeypatch, lab_root, tmp_path, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        _discover(lab_root)
    assert seen["B"]["send_started"] is False and seen["C"]["send_started"] is False
