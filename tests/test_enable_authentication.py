# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Privileged EXEC (enable-password) authentication for managed terminals,
against a synthetic IOS/IOS XE device in a real isolated tmux pane.

Requirement IDs (E/S series) are in each test's name or comment. Counts of
`enable` commands and enable-secret attempts are asserted numerically from
the device's own event log. All credentials are synthetic sentinels; no
assertion message ever prints one (only booleans/counts are asserted)."""

from __future__ import annotations

import os
import stat
import threading
import time

import pytest

from network_lab_mcp import terminal

import enable_support as dev

ENABLE_PW = "SENT-ENABLE-pw-4f7a91"
LOGIN_PW = "SENT-LOGIN-pw-83c2de"
FIXED = terminal.ENABLE_FAILED_MESSAGE


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


def _open(monkeypatch, argv, name="L3SW1", **cfg):
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", argv))
    config = {"transport": "ssh", "address": "192.0.2.10", "type": "iosxe", **cfg}
    return terminal.open_device_terminal(name, config)


def _no_secret(*objs):
    text = " ".join(repr(o) + str(o) for o in objs)
    return ENABLE_PW not in text and LOGIN_PW not in text


def _failure(monkeypatch, argv, **cfg):
    with pytest.raises(terminal.TerminalError) as info:
        _open(monkeypatch, argv, **cfg)
    return info.value


# ---------------- success paths (E01-E06, E21, E22, E26) ----------------


@pytest.mark.parametrize("device_type", ["ios", "iosxe"])
@pytest.mark.parametrize("transport", ["ssh", "telnet"])
def test_e02_e05_enable_success(tmp_path, monkeypatch, device_type, transport):
    argv = dev.write_device(tmp_path, "d", secret=ENABLE_PW)
    result = _open(monkeypatch, argv, type=device_type, transport=transport, password=LOGIN_PW, enable_password=ENABLE_PW)
    assert result["reused"] is False
    assert dev.count(argv, "enable") == 1
    assert dev.count(argv, "secret_ok") == 1 and dev.count(argv, "secret_bad") == 0
    assert dev.count(argv, "other:") == 0
    assert "L3SW1#" in terminal.read_device("L3SW1")["content"].splitlines()[-1]
    assert _no_secret(result)


def test_e01_e21_already_privileged_sends_nothing(tmp_path, monkeypatch):
    argv = dev.write_device(tmp_path, "d", secret=ENABLE_PW, start_priv=True)
    _open(monkeypatch, argv, enable_password=ENABLE_PW)
    assert dev.events_of(argv) == []


def test_e06_enable_without_password_prompt(tmp_path, monkeypatch):
    argv = dev.write_device(tmp_path, "d", secret=ENABLE_PW, mode="nopass")
    _open(monkeypatch, argv, enable_password=ENABLE_PW)
    assert dev.events_of(argv) == ["enable"]


def test_e07_e17_e25_no_enable_password_no_attempt(tmp_path, monkeypatch):
    # login password configured, enable_password not: nothing is sent, in
    # particular the login password is never used as the enable password.
    argv = dev.write_device(tmp_path, "d", secret=LOGIN_PW)
    _open(monkeypatch, argv, password=LOGIN_PW)
    assert dev.events_of(argv) == []


@pytest.mark.parametrize("cfg", [{"type": "iosxr"}, {"type": "nxos"}, {"type": "host"}, {"type": None}])
def test_e18_e19_e20_not_applicable_types(tmp_path, monkeypatch, cfg):
    argv = dev.write_device(tmp_path, "d", secret=ENABLE_PW)
    _open(monkeypatch, argv, enable_password=ENABLE_PW, **cfg)
    assert dev.events_of(argv) == []


# ---------------- ordinary failures (E08-E12, E23, E34-E37) ----------------


@pytest.mark.parametrize(
    "scenario",
    [
        {"mode": "reject"},
        {"mode": "reject", "fail_text": "% Bad secrets"},
        {"mode": "reject", "fail_text": "Authorization failed"},
        {"mode": "no_password_set"},
        {"mode": "reject", "attempts": 3, "reprompt": True},  # re-prompt: never answered twice (E09)
    ],
    ids=["denied", "bad-secrets", "authz", "no-password-set", "reprompt"],
)
def test_e08_e09_e10_e23_e34_e35_rejections(tmp_path, monkeypatch, scenario):
    argv = dev.write_device(tmp_path, "d", secret=ENABLE_PW, **scenario)
    error = _failure(monkeypatch, argv, enable_password=ENABLE_PW)
    assert isinstance(error, terminal.EnableAuthenticationError) and str(error) == FIXED
    assert dev.count(argv, "enable") == 1
    assert dev.count(argv, "secret_") <= 1  # at most one password send, never a retry
    assert dev.count(argv, "other:") == 0  # no follow-up command
    assert terminal.list_device_sessions() == []  # the new session was closed
    assert error.__cause__ is None and error.__context__ is None and _no_secret(error)


def test_e11_timeout_and_e40_total_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "ENABLE_STAGE_TIMEOUT_SECONDS", 100)
    monkeypatch.setattr(terminal, "ENABLE_TOTAL_TIMEOUT_SECONDS", 3)
    argv = dev.write_device(tmp_path, "d", mode="silent")
    started = time.monotonic()
    error = _failure(monkeypatch, argv, enable_password=ENABLE_PW)
    assert time.monotonic() - started < 3 + 12  # total bound + login/prompt waits
    assert error.reason == "timeout" and dev.count(argv, "secret_") == 0
    assert terminal.list_device_sessions() == []


def test_e12_session_ends_midway(tmp_path, monkeypatch):
    argv = dev.write_device(tmp_path, "d", mode="disconnect")
    error = _failure(monkeypatch, argv, enable_password=ENABLE_PW)
    assert isinstance(error, terminal.EnableAuthenticationError)
    assert dev.count(argv, "secret_") == 0 and dev.count(argv, "other:") == 0


@pytest.mark.parametrize("mode", ["diffhost", "config_prompt"])
def test_e14_e15_e27_wrong_prompt_never_success(tmp_path, monkeypatch, mode):
    argv = dev.write_device(tmp_path, "d", mode=mode, secret=ENABLE_PW)
    _failure(monkeypatch, argv, enable_password=ENABLE_PW)
    assert dev.count(argv, "secret_") == 0


def test_e16_banner_text_does_not_trigger(tmp_path, monkeypatch):
    banner = ["Password:", "Router#", "% Access denied", "Authorization failed", "OTHER>"]
    argv = dev.write_device(tmp_path, "d", secret=ENABLE_PW, banner=banner)
    _open(monkeypatch, argv, enable_password=ENABLE_PW)
    assert dev.count(argv, "secret_ok") == 1 and dev.count(argv, "enable") == 1


def test_e28_jump_shell_prompt_is_not_an_exec_prompt(tmp_path, monkeypatch):
    argv = dev.write_device(tmp_path, "d", host="root@jump:~", start_priv=True, secret=ENABLE_PW)
    _failure(monkeypatch, argv, enable_password=ENABLE_PW)
    assert dev.events_of(argv) == []


def test_e30_incomplete_prompt_receives_nothing(tmp_path, monkeypatch):
    argv = dev.write_device(tmp_path, "d", raw_prompt="L3SW", secret=ENABLE_PW)
    _failure(monkeypatch, argv, enable_password=ENABLE_PW)
    assert dev.events_of(argv) == []


# ---------------- reuse (E24, E36, S15) ----------------


def test_e24_e36_reused_session_never_gets_credentials(tmp_path, monkeypatch):
    argv = dev.write_device(tmp_path, "d", secret=ENABLE_PW)
    _open(monkeypatch, argv)  # no enable_password: stays at user EXEC
    result = _open(monkeypatch, argv, enable_password=ENABLE_PW)
    assert result["reused"] is True
    assert dev.events_of(argv) == []
    assert [s["device"] for s in terminal.list_device_sessions()] == ["L3SW1"]


# ---------------- abnormal credential echo (E43, S23, S42, S43) ----------------


@pytest.mark.parametrize("scenario", [{"mode": "echo_full"}, {"mode": "echo_split"}, {"mode": "echo_then_exit"}, {"mode": "echo_with_prompt"}, {"local_echo": True}],
                         ids=["full", "split-chunks", "then-disconnect", "echo-then-priv-prompt", "local-echo"])
def test_e43_s23_echo_is_fatal(tmp_path, monkeypatch, scenario):
    argv = dev.write_device(tmp_path, "d", secret=ENABLE_PW, **scenario)
    error = _failure(monkeypatch, argv, enable_password=ENABLE_PW)
    assert isinstance(error, terminal.CredentialEchoError) and str(error) == terminal.ENABLE_ECHO_MESSAGE
    assert dev.count(argv, "secret_") == 1 and dev.count(argv, "other:") == 0
    assert terminal.list_device_sessions() == []
    assert error.__cause__ is None and error.__context__ is None and _no_secret(error)
    # residual copy only in the private transcript: 0600 file, 0700 directory
    logs = list((terminal.LOGS_ROOT / "L3SW1").iterdir())
    assert len(logs) == 1
    assert stat.S_IMODE(logs[0].stat().st_mode) == 0o600
    assert stat.S_IMODE((terminal.LOGS_ROOT / "L3SW1").stat().st_mode) == 0o700


# ---------------- credential protection (S03, S06, S07, S27) ----------------


def test_s03_s06_s07_s27_normal_path_leaves_no_secret(tmp_path, monkeypatch):
    argv = dev.write_device(tmp_path, "d", secret=ENABLE_PW)
    result = _open(monkeypatch, argv, password=LOGIN_PW, enable_password=ENABLE_PW)
    time.sleep(0.5)
    assert _no_secret(result)
    starts = terminal._run(["list-panes", "-a", "-F", "#{pane_start_command}"]).stdout
    assert ENABLE_PW not in starts and LOGIN_PW not in starts
    assert terminal._run(["list-buffers"], check=False).stdout.strip() == ""
    transcript = "".join(p.read_text(errors="replace") for p in (terminal.LOGS_ROOT / "L3SW1").iterdir())
    assert ENABLE_PW not in transcript
    pane = terminal._capture_pane("network-lab-device-L3SW1", 500)
    assert ENABLE_PW not in pane


# ---------------- state machine / pane ownership (E31-E33, E38, E39, E42) ----------------


def _managed_session(monkeypatch, argv, tmp_path):
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", argv))
    terminal._ensure_managed_session("network-lab-device-L3SW1", argv, log_device_name="L3SW1")
    host, level = terminal.wait_for_stable_exec_prompt("network-lab-device-L3SW1")
    assert (host, level) == ("L3SW1", "user")
    return "network-lab-device-L3SW1"


@pytest.mark.parametrize("state", [terminal.EnableState.DONE, terminal.EnableState.FAILED, terminal.EnableState.PASSWORD_SENT])
def test_e31_e32_e33_no_restart_from_other_states(tmp_path, monkeypatch, state):
    argv = dev.write_device(tmp_path, "d", secret=ENABLE_PW)
    session = _managed_session(monkeypatch, argv, tmp_path)
    attempt = terminal.EnableAttempt(state=state)
    with pytest.raises(terminal.EnableAuthenticationError):
        terminal.run_privileged_exec_authentication(session, "L3SW1", ENABLE_PW, attempt=attempt)
    assert attempt.enable_sends == 0 and attempt.password_sends == 0 and dev.events_of(argv) == []


@pytest.mark.parametrize("swap_on_call", [1, 2], ids=["before-enable", "before-password"])
def test_e38_e39_s39_replaced_pane_never_receives_credential(tmp_path, monkeypatch, swap_on_call):
    argv = dev.write_device(tmp_path, "d", secret=ENABLE_PW)
    other = dev.write_device(tmp_path, "other", secret=ENABLE_PW)
    session = _managed_session(monkeypatch, argv, tmp_path)
    calls = []

    def admit():
        calls.append(1)
        if len(calls) == swap_on_call:
            terminal._close_session(session)
            terminal._ensure_managed_session(session, other, log_device_name="L3SW1")

    attempt = terminal.EnableAttempt()
    with pytest.raises(terminal.TerminalError):
        terminal.run_privileged_exec_authentication(session, "L3SW1", ENABLE_PW, admit=admit, attempt=attempt)
    time.sleep(0.8)
    assert dev.events_of(other) == []  # nothing reached the replacement pane
    assert attempt.state is terminal.EnableState.FAILED
    assert attempt.enable_sends <= 1 and attempt.password_sends <= 1


def test_e42_failed_credential_send_is_not_retried(tmp_path, monkeypatch):
    argv = dev.write_device(tmp_path, "d", secret=ENABLE_PW)
    session = _managed_session(monkeypatch, argv, tmp_path)
    real = terminal._send_text_via_stdin
    sent = []

    def flaky(name, text, **kw):
        sent.append(text == ENABLE_PW)
        if text == ENABLE_PW:
            raise terminal.TerminalError("tmux paste-buffer timed out; delivery result is unknown")
        return real(name, text, **kw)

    monkeypatch.setattr(terminal, "_send_text_via_stdin", flaky)
    attempt = terminal.EnableAttempt()
    with pytest.raises(terminal.TerminalError):
        terminal.run_privileged_exec_authentication(session, "L3SW1", ENABLE_PW, attempt=attempt)
    assert sent.count(True) == 1 and attempt.password_sends == 1
    assert attempt.state is terminal.EnableState.FAILED


def test_admission_refusal_sends_nothing(tmp_path, monkeypatch):
    argv = dev.write_device(tmp_path, "d", secret=ENABLE_PW)
    session = _managed_session(monkeypatch, argv, tmp_path)

    class Refused(Exception):
        pass

    def admit():
        raise Refused()

    attempt = terminal.EnableAttempt()
    with pytest.raises(Refused):
        terminal.run_privileged_exec_authentication(session, "L3SW1", ENABLE_PW, admit=admit, attempt=attempt)
    assert attempt.enable_sends == 0 and attempt.password_sends == 0 and dev.events_of(argv) == []


# ---------------- concurrency (S08, S09, S10) ----------------


def test_s08_s09_s10_parallel_devices_use_their_own_secret(tmp_path, monkeypatch):
    a = dev.write_device(tmp_path, "a", host="SWA", secret="SENT-A-pw-1111")
    b = dev.write_device(tmp_path, "b", host="SWB", secret="SENT-B-pw-2222")
    argvs = {"SWA": a, "SWB": b}
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", argvs[config["address_name"]]))
    results = {}

    def go(name, secret):
        cfg = {"transport": "ssh", "address": "192.0.2.10", "address_name": name, "type": "iosxe", "enable_password": secret}
        try:
            results[name] = terminal.open_device_terminal(name, cfg)
        except terminal.TerminalError as exc:
            results[name] = exc

    threads = [threading.Thread(target=go, args=("SWA", "SENT-A-pw-1111")), threading.Thread(target=go, args=("SWB", "WRONG-pw-for-b"))]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert isinstance(results["SWA"], dict) and isinstance(results["SWB"], terminal.EnableAuthenticationError)
    assert dev.count(a, "secret_ok") == 1 and dev.count(a, "secret_bad") == 0
    assert dev.count(b, "secret_bad") == 1 and dev.count(b, "secret_ok") == 0


def test_s09_concurrent_open_same_device_sends_enable_once(tmp_path, monkeypatch):
    argv = dev.write_device(tmp_path, "d", secret=ENABLE_PW)
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", argv))
    cfg = {"transport": "ssh", "address": "192.0.2.10", "type": "iosxe", "enable_password": ENABLE_PW}
    out = []
    threads = [threading.Thread(target=lambda: out.append(terminal.open_device_terminal("L3SW1", cfg))) for _ in range(3)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert len(out) == 3 and sorted(r["reused"] for r in out) == [False, True, True]
    assert dev.count(argv, "enable") == 1 and dev.count(argv, "secret_ok") == 1
