# SPDX-License-Identifier: Apache-2.0
"""Stage B: jump-server password authentication for SSH ProxyJump, against
local fake ssh scripts in an isolated tmux (no network, no real ssh)."""

from __future__ import annotations

import pytest

from network_lab_mcp import terminal

JUMP, TARGET = "192.0.2.10", "192.0.2.11"


def _cfg(jump_password="JUMP-SENTINEL", **jump_extra):
    jump = {"address": JUMP, "username": "jump-user", **jump_extra}
    if jump_password:
        jump["password"] = jump_password
    return {"transport": "ssh", "address": TARGET, "username": "target-user",
            "password": "TARGET-SENTINEL", "jump_host_config": jump}


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")
    monkeypatch.setattr(terminal, "_MANAGED_AUTH_SETTLE_TIMEOUT_SECONDS", 4)
    yield
    for s in terminal.list_device_sessions():
        terminal.close_device_terminal(s["device"])


def _fake(tmp_path, monkeypatch, body):
    path = tmp_path / "fake_ssh.sh"
    path.write_text("#!/usr/bin/env bash\nstty -echo\n" + body)
    path.chmod(0o755)
    monkeypatch.setattr(terminal, "_build_transport_command", lambda c, **k: ("ssh", ["bash", str(path)]))
    return tmp_path / "got.txt"


def _prompt(user, host):
    return f"printf \"{user}@{host}'s password: \"\n"


def test_jump_then_target_each_answered_once(tmp_path, monkeypatch):
    got = _fake(tmp_path, monkeypatch,
                _prompt("jump-user", JUMP) + f"read a; echo jump=$a >> {tmp_path}/got.txt\n"
                + _prompt("target-user", TARGET) + f"read b; echo target=$b >> {tmp_path}/got.txt\n"
                + "stty echo\necho\necho AUTH-OK\nsleep 5\n")
    terminal.open_device_terminal("PJ", _cfg())
    assert got.read_text().split() == ["jump=JUMP-SENTINEL", "target=TARGET-SENTINEL"]
    pane = terminal.capture_device_terminal_view("PJ").pane_text
    assert "AUTH-OK" in pane and "SENTINEL" not in pane


def test_keyboard_interactive_prompt_format(tmp_path, monkeypatch):
    got = _fake(tmp_path, monkeypatch,
                'printf "(jump-user@192.0.2.10) Password: "\n' + f"read a; echo jump=$a >> {tmp_path}/got.txt\n"
                + "stty echo\necho\necho AUTH-OK\nsleep 5\n")
    terminal.open_device_terminal("PJ", _cfg())
    assert got.read_text().split() == ["jump=JUMP-SENTINEL"]


def test_jump_reprompt_fails_closed_without_second_send(tmp_path, monkeypatch):
    got = _fake(tmp_path, monkeypatch,
                _prompt("jump-user", JUMP) + f"read a; echo n=$a >> {tmp_path}/got.txt\n"
                + 'echo; echo "Permission denied, please try again."\n' + _prompt("jump-user", JUMP)
                + f"read b; echo second=$b >> {tmp_path}/got.txt\nsleep 5\n")
    with pytest.raises(terminal.TerminalError, match="jump host authentication failed"):
        terminal.open_device_terminal("PJ", _cfg())
    assert got.read_text().split() == ["n=JUMP-SENTINEL"]
    assert "PJ" not in {s["device"] for s in terminal.list_device_sessions()}


@pytest.mark.parametrize("prompt", [
    "jump-user@alias-host's password: ",       # name not equal to configured address
    "other-user@192.0.2.10's password: ",      # user mismatch
    "Password: ",                              # no hop identity at all
])
def test_unattributable_prompt_sends_nothing(tmp_path, monkeypatch, prompt):
    got = _fake(tmp_path, monkeypatch, f'printf "{prompt}"\nread a; echo $a >> {tmp_path}/got.txt\nsleep 5\n')
    with pytest.raises(terminal.TerminalError, match="cannot safely complete authentication"):
        terminal.open_device_terminal("PJ", _cfg())
    assert not got.exists()


def test_same_user_and_host_on_both_hops_is_ambiguous(tmp_path, monkeypatch):
    cfg = _cfg()
    cfg["jump_host_config"].update(address=TARGET, username="target-user")
    got = _fake(tmp_path, monkeypatch, _prompt("target-user", TARGET) + f"read a; echo $a >> {tmp_path}/got.txt\nsleep 5\n")
    with pytest.raises(terminal.TerminalError, match="cannot safely"):
        terminal.open_device_terminal("PJ", cfg)
    assert not got.exists()


def test_jump_prompt_without_jump_password_fails_closed(tmp_path, monkeypatch):
    got = _fake(tmp_path, monkeypatch, _prompt("jump-user", JUMP) + f"read a; echo $a >> {tmp_path}/got.txt\nsleep 5\n")
    with pytest.raises(terminal.TerminalError, match="jump-host password prompt"):
        terminal.open_device_terminal("PJ", _cfg(jump_password=None))
    assert not got.exists()


def test_target_password_never_sent_to_jump_prompt(tmp_path, monkeypatch):
    got = _fake(tmp_path, monkeypatch, _prompt("jump-user", JUMP) + f"read a; echo $a >> {tmp_path}/got.txt\nsleep 5\n")
    try:
        terminal.open_device_terminal("PJ", _cfg())
    except terminal.TerminalError:
        pass
    assert "TARGET-SENTINEL" not in (got.read_text() if got.exists() else "")


def test_state_change_before_send_aborts(monkeypatch):
    sent = []
    monkeypatch.setattr(terminal, "_send_secret_text", lambda *a: sent.append(a))
    monkeypatch.setattr(terminal, "_send_enter", lambda *a: sent.append(a))
    line = f"jump-user@{JUMP}'s password: "
    monkeypatch.setattr(terminal, "_pane_state", lambda s: "running")
    monkeypatch.setattr(terminal, "_capture_pane", lambda s, n: "changed output\n")
    with pytest.raises(terminal.TerminalError, match="state changed"):
        terminal._authenticate_jump_stage("PJ", _cfg(), "s", line, line)
    monkeypatch.setattr(terminal, "_capture_pane", lambda s, n: line)
    monkeypatch.setattr(terminal, "_pane_state", lambda s: "exited")
    with pytest.raises(terminal.TerminalError, match="state changed"):
        terminal._authenticate_jump_stage("PJ", _cfg(), "s", line, line)
    assert sent == []


def test_strict_host_key_for_every_hop_with_or_without_jump_password():
    _, cmd = terminal._build_transport_command(_cfg())
    assert "StrictHostKeyChecking=yes" in cmd and "StrictHostKeyChecking=no" not in " ".join(cmd)
    _, cmd = terminal._build_transport_command(_cfg(jump_password=None))
    # policy since Step 5.3.3a: strict on every hop regardless of password configuration
    assert "StrictHostKeyChecking=yes" in cmd and "accept-new" not in " ".join(cmd)
    joined = " ".join(terminal._build_transport_command(_cfg())[1])
    assert "SENTINEL" not in joined and "UserKnownHostsFile" not in joined
