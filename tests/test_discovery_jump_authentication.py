# SPDX-License-Identifier: Apache-2.0
"""Discovery SSH ProxyJump: the jump-server password prompt is answered once
via the same stage logic as managed terminal_open (terminal.authenticate_
bootstrap_jump_stage). Local fake ssh scripts, isolated tmux, fictional
sentinels only."""

from __future__ import annotations

import pytest

from network_lab_mcp import discovery, terminal

JUMP, TARGET = "192.0.2.10", "192.0.2.11"
XR_PROMPT = "RP/0/RP0/CPU0:R1#"


def _cfg(jump_user="jump-user", target_user="target-user", jump_pw="JUMP-SENTINEL"):
    jump = {"address": JUMP, "username": jump_user}
    if jump_pw:
        jump["password"] = jump_pw
    return {"transport": "ssh", "address": TARGET, "username": target_user, "password": "TARGET-SENTINEL",
            "jump_host_config": jump}


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")
    monkeypatch.setattr(discovery, "LOGIN_TIMEOUT_SECONDS", 6)
    monkeypatch.setattr(terminal, "_MANAGED_AUTH_SETTLE_TIMEOUT_SECONDS", 4)
    yield
    for d in terminal.list_discovery_device_ids():
        terminal.close_bootstrap_terminal(d)


def _fake(tmp_path, monkeypatch, body):
    path = tmp_path / "fake_ssh.sh"
    path.write_text("#!/usr/bin/env bash\nstty -echo\n" + body)
    path.chmod(0o755)
    monkeypatch.setattr(terminal, "_build_transport_command", lambda c, **k: ("ssh", ["bash", str(path)]))
    return tmp_path / "got.txt"


def _p(user, host, kbd=False):
    return (f'printf "({user}@{host}) Password: "\n' if kbd else f"printf \"{user}@{host}'s password: \"\n")


def _rec(tmp_path, name):
    return f"read x; echo {name}=$x >> {tmp_path}/got.txt\n"


TAIL = f'stty echo\necho\necho "{XR_PROMPT}"\nsleep 5\n'


@pytest.mark.parametrize("kbd", [False, True])
def test_jump_then_target_each_once(tmp_path, monkeypatch, kbd):
    got = _fake(tmp_path, monkeypatch,
                _p("jump-user", JUMP, kbd) + _rec(tmp_path, "jump")
                + _p("target-user", TARGET, kbd) + _rec(tmp_path, "target") + TAIL)
    assert discovery._login("R1", _cfg()) == "R1"
    assert got.read_text().split() == ["jump=JUMP-SENTINEL", "target=TARGET-SENTINEL"]
    assert "SENTINEL" not in terminal.read_bootstrap("R1")


def test_same_username_on_both_hops(tmp_path, monkeypatch):
    got = _fake(tmp_path, monkeypatch,
                _p("same", JUMP) + _rec(tmp_path, "jump") + _p("same", TARGET) + _rec(tmp_path, "target") + TAIL)
    assert discovery._login("R1", _cfg("same", "same")) == "R1"
    assert got.read_text().split() == ["jump=JUMP-SENTINEL", "target=TARGET-SENTINEL"]


def test_target_key_auth_after_jump_password(tmp_path, monkeypatch):
    got = _fake(tmp_path, monkeypatch, _p("jump-user", JUMP) + _rec(tmp_path, "jump") + TAIL)
    assert discovery._login("R1", _cfg()) == "R1"
    assert got.read_text().split() == ["jump=JUMP-SENTINEL"]


def test_ios_style_login_also_supports_jump(tmp_path, monkeypatch):
    got = _fake(tmp_path, monkeypatch,
                _p("jump-user", JUMP) + _rec(tmp_path, "jump") + _p("target-user", TARGET)
                + _rec(tmp_path, "target") + 'stty echo\necho\necho "LegacyRouter#"\nsleep 5\n')
    assert discovery._login_ios_style("LegacyRouter", _cfg()) == "LegacyRouter"
    assert got.read_text().split() == ["jump=JUMP-SENTINEL", "target=TARGET-SENTINEL"]


def test_jump_reprompt_fails_closed_single_send(tmp_path, monkeypatch):
    got = _fake(tmp_path, monkeypatch,
                _p("jump-user", JUMP) + _rec(tmp_path, "n1") + 'echo; echo "Permission denied, please try again."\n'
                + _p("jump-user", JUMP) + _rec(tmp_path, "n2") + "sleep 5\n")
    with pytest.raises(Exception, match="jump host authentication failed"):
        discovery._login("R1", _cfg())
    assert got.read_text().split() == ["n1=JUMP-SENTINEL"]


def test_target_reprompt_fails_closed_single_send(tmp_path, monkeypatch):
    got = _fake(tmp_path, monkeypatch,
                _p("jump-user", JUMP) + _rec(tmp_path, "jump") + _p("target-user", TARGET) + _rec(tmp_path, "t1")
                + 'echo; echo "Permission denied, please try again."\n' + _p("target-user", TARGET)
                + _rec(tmp_path, "t2") + "sleep 5\n")
    with pytest.raises(Exception):
        discovery._login("R1", _cfg())
    assert got.read_text().split() == ["jump=JUMP-SENTINEL", "t1=TARGET-SENTINEL"]


@pytest.mark.parametrize("prompt", [
    "jump-user@alias-host's password: ", "other@192.0.2.10's password: ", "Password: ",
    "target-user@192.0.2.99's password: "])
def test_unattributable_prompt_sends_nothing(tmp_path, monkeypatch, prompt):
    got = _fake(tmp_path, monkeypatch, f'printf "{prompt}"\n' + _rec(tmp_path, "x") + "sleep 5\n")
    with pytest.raises(Exception):
        discovery._login("R1", _cfg())
    assert not got.exists()


def test_order_anomaly_target_prompt_first_never_gets_jump_password(tmp_path, monkeypatch):
    got = _fake(tmp_path, monkeypatch,
                _p("target-user", TARGET) + _rec(tmp_path, "first") + _p("jump-user", JUMP)
                + _rec(tmp_path, "second") + "sleep 5\n")
    with pytest.raises(Exception):
        discovery._login("R1", _cfg())
    assert "JUMP-SENTINEL" not in (got.read_text() if got.exists() else "")


def test_no_jump_password_configured_still_fails_closed(tmp_path, monkeypatch):
    got = _fake(tmp_path, monkeypatch, _p("jump-user", JUMP) + _rec(tmp_path, "x") + "sleep 5\n")
    with pytest.raises(Exception, match="jump"):
        discovery._login("R1", _cfg(jump_pw=None))
    assert not got.exists()


def test_connection_closed_before_prompt(tmp_path, monkeypatch):
    _fake(tmp_path, monkeypatch, "echo 'Connection closed by 192.0.2.10 port 22'\nexit 255\n")
    with pytest.raises(Exception):
        discovery._login("R1", _cfg())


def test_discovery_argv_is_strict_and_secret_free_when_jump_password_set():
    _, cmd = terminal._build_transport_command(_cfg())
    joined = " ".join(cmd)
    assert "StrictHostKeyChecking=yes" in cmd and "accept-new" not in joined
    assert "SENTINEL" not in joined and "UserKnownHostsFile" not in joined
    assert "-J" not in cmd and "ProxyCommand=" in joined


def test_discovery_without_jump_password_is_also_strict_and_never_accept_new():
    _, cmd = terminal._build_transport_command(_cfg(jump_pw=None))
    joined = " ".join(cmd)
    assert "accept-new" not in joined and joined.count("StrictHostKeyChecking=yes") == 2  # target + jump child
