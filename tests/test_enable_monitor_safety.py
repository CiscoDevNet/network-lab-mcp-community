# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Enable authentication versus the read-only Terminal Monitor (and MCP tool results).

A real isolated tmux pane runs a synthetic IOS XE device; a polling thread
runs the Monitor's own read-only step (`_monitor_stream_step`) the whole time,
i.e. it sees exactly what `terminal monitor` would print. All credentials are
synthetic sentinels; assertions only compare booleans and counts, never print a
secret. Items: M1 no interference, M2 no exposure on a normal (non-echo)
authentication, M3 echo: the echo is detected, and whether the monitor showed the
echoed copy is only *recorded* (pytest property), never asserted -- these tests do
NOT guarantee non-exposure to a monitor on an abnormal echo; that is the accepted,
documented residual risk (SECURITY.md 3a). M4 no further send after detection, M5
parallel devices, M6 MCP tool results and errors."""

from __future__ import annotations

import json
import threading
import time

import pytest
from network_lab_mcp import lab, mcp_server, terminal
from network_lab_mcp.mcp_server import ToolError
from network_lab_mcp.cli import main as climain

import enable_support as dev

ENABLE_PW = "SENT-ENABLE-pw-4f7a91"
LOGIN_PW = "SENT-LOGIN-pw-83c2de"
JOIN_TIMEOUT = 60


@pytest.fixture(autouse=True)
def _fast(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")
    monkeypatch.setattr(terminal, "_MANAGED_LOGIN_TIMEOUT_SECONDS", 3)
    monkeypatch.setattr(terminal, "_MANAGED_AUTH_SETTLE_TIMEOUT_SECONDS", 3)
    monkeypatch.setattr(terminal, "ENABLE_STAGE_TIMEOUT_SECONDS", 4)
    monkeypatch.setattr(terminal, "ENABLE_TOTAL_TIMEOUT_SECONDS", 8)
    yield
    for session in terminal.list_device_sessions():
        terminal.close_device_terminal(session["device"])


class Monitor:
    """Runs the Monitor's read-only step in a thread and keeps what it printed."""

    def __init__(self, device: str):
        self.device = device
        self.lines: list[str] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._cursor = climain._MonitorStreamCursor()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _step(self):
        try:
            printed, _ = climain._monitor_stream_step(self.device, self._cursor)
            self.lines.extend(printed)
        except Exception as exc:  # recorded, asserted empty
            self.errors.append(type(exc).__name__)

    def _run(self):
        while not self._stop.is_set():
            self._step()
            self._stop.wait(0.02)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(10)
        self._step()  # final observation
        return False

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _open(monkeypatch, argv, name="L3SW1", **cfg):
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", argv))
    config = {"transport": "ssh", "address": "192.0.2.10", "type": "iosxe", **cfg}
    return terminal.open_device_terminal(name, config)


# ---------------- M1 / M2: normal authentication with a monitor attached ----------------


def test_m1_m2_monitor_does_not_interfere_and_never_shows_the_password(tmp_path, monkeypatch):
    argv = dev.write_device(tmp_path, "d", secret=ENABLE_PW)
    with Monitor("L3SW1") as monitor:
        result = _open(monkeypatch, argv, password=LOGIN_PW, enable_password=ENABLE_PW)
        time.sleep(0.6)  # let the monitor take a few more looks at the finished session
    # M1: authentication behaved exactly as without a monitor
    assert result["reused"] is False
    assert dev.count(argv, "enable") == 1 and dev.count(argv, "secret_ok") == 1
    assert dev.count(argv, "secret_bad") == 0 and dev.count(argv, "other:") == 0
    assert monitor.errors == []
    # the monitor really watched the authentication (not a vacuous pass)
    assert "terminal activity started" in monitor.text and "Password:" in monitor.text
    # M2: no credential in anything the monitor printed
    assert ENABLE_PW not in monitor.text and LOGIN_PW not in monitor.text


# ---------------- M3 / M4: echoing device ----------------

ECHO_SCENARIOS = [
    ({"mode": "echo_full"}, "echo_full"),
    ({"mode": "echo_split"}, "echo_split"),
    ({"mode": "echo_then_exit"}, "echo_then_exit"),
    ({"mode": "echo_with_prompt"}, "echo_with_prompt"),
    ({"local_echo": True}, "local_echo"),
]


@pytest.mark.parametrize("scenario,tag", ECHO_SCENARIOS, ids=[t for _, t in ECHO_SCENARIOS])
def test_m3_m4_echo_is_detected_nothing_is_resent_and_monitor_exposure_is_recorded(tmp_path, monkeypatch, record_property, scenario, tag):
    repeats, exposed = 3, 0
    for attempt in range(repeats):
        argv = dev.write_device(tmp_path, f"d{attempt}", secret=ENABLE_PW, **scenario)
        with Monitor("L3SW1") as monitor:
            with pytest.raises(terminal.TerminalError) as info:
                _open(monkeypatch, argv, password=LOGIN_PW, enable_password=ENABLE_PW)
        error = info.value
        # M3: the existing detection fires, with the fixed message and no chained secret
        assert isinstance(error, terminal.CredentialEchoError) and str(error) == terminal.ENABLE_ECHO_MESSAGE
        assert error.__cause__ is None and error.__context__ is None
        assert ENABLE_PW not in (repr(error) + str(error)) and LOGIN_PW not in (repr(error) + str(error))
        # M4: exactly one enable and one password send, nothing else, session closed
        assert dev.count(argv, "enable") == 1 and dev.count(argv, "secret_") == 1 and dev.count(argv, "other:") == 0
        assert terminal.list_device_sessions() == []
        assert monitor.errors == []
        assert LOGIN_PW not in monitor.text  # the login secret is never part of an enable echo
        exposed += ENABLE_PW in monitor.text
    # Detection alone does not prove the monitor never showed the echoed copy. What happened is
    # recorded, not asserted: exposure is an accepted residual risk on an abnormal echo.
    record_property("monitor_showed_echoed_secret", f"{exposed}/{repeats}")


# ---------------- M5: parallel devices ----------------


def test_m5_parallel_authentication_never_mixes_credentials_into_monitors(tmp_path, monkeypatch):
    secrets = {"SWA": "SENT-A-pw-1111", "SWB": "SENT-B-pw-2222", "SWC": "SENT-C-pw-3333"}
    wrong = "SENT-WRONG-pw-9999"  # SWC is sent a wrong password (rejection path)
    argvs = {n: dev.write_device(tmp_path, n.lower(), host=n, secret=s) for n, s in secrets.items()}
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", argvs[config["address_name"]]))
    sent = {"SWA": secrets["SWA"], "SWB": secrets["SWB"], "SWC": wrong}
    results: dict = {}

    def go(name):
        cfg = {"transport": "ssh", "address": "192.0.2.10", "address_name": name, "type": "iosxe", "enable_password": sent[name]}
        try:
            results[name] = terminal.open_device_terminal(name, cfg)
        except terminal.TerminalError as exc:
            results[name] = exc

    monitors = {name: Monitor(name) for name in secrets}
    for monitor in monitors.values():
        monitor.__enter__()
    try:
        threads = [threading.Thread(target=go, args=(n,)) for n in secrets]
        [t.start() for t in threads]
        [t.join(JOIN_TIMEOUT) for t in threads]
        assert not any(t.is_alive() for t in threads)
        time.sleep(0.5)
    finally:
        for monitor in monitors.values():
            monitor.__exit__(None, None, None)
    assert isinstance(results["SWA"], dict) and isinstance(results["SWB"], dict)
    assert isinstance(results["SWC"], terminal.EnableAuthenticationError)
    for name, argv in argvs.items():
        assert dev.count(argv, "enable") == 1 and dev.count(argv, "secret_") == 1
    assert dev.count(argvs["SWC"], "secret_bad") == 1
    everything = [*secrets.values(), wrong, LOGIN_PW]
    for name, monitor in monitors.items():
        assert monitor.errors == []
        assert not any(value in monitor.text for value in everything), name  # own secret and every other device's
    assert not any(value in (repr(r) + str(r)) for r in results.values() for value in everything)


# ---------------- M6: MCP tool results and errors ----------------


@pytest.fixture()
def mcp_device(tmp_path, monkeypatch):
    state = {"argv": None, "config": None}

    def get_device(name):
        return "topology", {"transport": "ssh", "address": "192.0.2.10", "type": "iosxe", "password": LOGIN_PW,
                            "enable_password": ENABLE_PW}

    monkeypatch.setattr(lab, "get_device", get_device)
    monkeypatch.setattr(lab, "verify_device_in_active_topology", lambda name: None)  # synthetic: gate only
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", state["argv"]))
    return state


def test_m6_mcp_tool_results_and_errors_never_contain_credentials(tmp_path, monkeypatch, mcp_device):
    # success path: terminal_open / terminal_read / terminal_list / terminal_close
    mcp_device["argv"] = dev.write_device(tmp_path, "ok", secret=ENABLE_PW)
    with Monitor("L3SW1") as monitor:
        opened = mcp_server.terminal_open("L3SW1")
        time.sleep(0.4)
        read = mcp_server.terminal_read("L3SW1")
        listed = mcp_server.terminal_list()
        closed = mcp_server.terminal_close("L3SW1")
    blob = json.dumps([opened, read, listed, closed], default=str)
    assert ENABLE_PW not in blob and LOGIN_PW not in blob and ENABLE_PW not in monitor.text
    # rejection: wrong password on the device side
    mcp_device["argv"] = dev.write_device(tmp_path, "bad", secret="SENT-OTHER-pw-0000")
    with pytest.raises(ToolError) as bad:
        mcp_server.terminal_open("L3SW1")
    assert str(bad.value) == terminal.ENABLE_FAILED_MESSAGE
    # echo: fixed message, no chained detail
    mcp_device["argv"] = dev.write_device(tmp_path, "echo", secret=ENABLE_PW, mode="echo_full")
    with pytest.raises(ToolError) as echo:
        mcp_server.terminal_open("L3SW1")
    assert str(echo.value) == terminal.ENABLE_ECHO_MESSAGE
    for error in (bad.value, echo.value):
        chain = []
        link = error
        while link is not None:
            chain.append(repr(link) + str(link))
            link = link.__cause__ or link.__context__
        assert ENABLE_PW not in " ".join(chain) and LOGIN_PW not in " ".join(chain) and "SENT-OTHER-pw-0000" not in " ".join(chain)
    assert terminal.list_device_sessions() == []
