# SPDX-License-Identifier: Apache-2.0
"""Access-info validation and ssh/telnet argv safety (no network)."""

from __future__ import annotations

import pytest

from network_lab_mcp import connection_safety as cs
from network_lab_mcp import lab, terminal

BAD_VALUES = ["-oProxyCommand=touch x", ";command", "$(command)", "`command`", "a b", "a\nb", "a\x00b", ""]


@pytest.mark.parametrize("bad", BAD_VALUES + ["1.2.3", "300.1.1.1", "::g", "[::1]"])
def test_bad_hosts_rejected(bad):
    with pytest.raises(cs.UnsafeConnectionValue):
        cs.validate_host(bad)


@pytest.mark.parametrize("good", ["192.0.2.1", "r1.example.net", "router-1", "2001:db8::1"])
def test_good_hosts_accepted(good):
    assert cs.validate_host(good) == good


@pytest.mark.parametrize("bad", [0, 65536, -1, "x", "22; id", True, 1.5, None])
def test_bad_ports_rejected(bad):
    with pytest.raises(cs.UnsafeConnectionValue):
        cs.validate_port(bad)


def test_ports_accepted():
    assert cs.validate_port(22) == 22 and cs.validate_port("65535") == 65535


@pytest.mark.parametrize("bad", BAD_VALUES + ["a@b", "-l"])
def test_bad_usernames_rejected(bad):
    if bad == "":
        pytest.skip("empty username means 'unset' at endpoint level")
    with pytest.raises(cs.UnsafeConnectionValue):
        cs.validate_username(bad)


def test_error_never_echoes_value():
    with pytest.raises(cs.UnsafeConnectionValue) as exc:
        cs.validate_host("-oSECRET-SENTINEL")
    assert "SECRET-SENTINEL" not in str(exc.value)


@pytest.mark.parametrize("bad", ["-oProxyCommand=x", "a b", "$(x)"])
def test_argv_builder_rejects_unsafe_values(bad):
    with pytest.raises(terminal.TerminalError):
        terminal._build_transport_command({"transport": "ssh", "address": bad, "username": "u"})
    with pytest.raises(terminal.TerminalError):
        terminal._build_transport_command({"transport": "ssh", "address": "192.0.2.1", "username": bad})
    with pytest.raises(terminal.TerminalError):
        terminal._build_transport_command({"transport": "ssh", "address": "192.0.2.1", "port": bad})
    with pytest.raises(terminal.TerminalError):
        terminal._build_transport_command(
            {"transport": "ssh", "address": "192.0.2.1", "jump_host_config": {"address": bad}}
        )


def test_ipv6_jump_is_bracketed_single_argv_element():
    _, cmd = terminal._build_transport_command(
        {"transport": "ssh", "address": "2001:db8::2", "username": "u",
         "jump_host_config": {"address": "2001:db8::1", "username": "j"}}
    )
    proxy = next(a for a in cmd if a.startswith("ProxyCommand="))
    assert "j@2001:db8::1" in proxy and "-p 22" in proxy and "-J" not in cmd and "-W %h:%p" in proxy


def test_commit_validation_rejects_unsafe_access_info(lab_root):
    data = {"name": "x", "devices": {"R1": {"type": "iosxr", "transport": "ssh", "address": "-oProxyCommand=x"}}}
    with pytest.raises(lab.LabConfigError) as exc:
        lab.write_access_info("x", data, lab_root)
    assert "ProxyCommand" not in str(exc.value)
    data = {"name": "x", "jump_hosts": {"j": {"type": "host", "address": "a b"}}, "devices": {}}
    with pytest.raises(lab.LabConfigError):
        lab.write_access_info("x", data, lab_root)


def test_telnet_with_jump_host_commit_allowed(lab_root):
    data = {
        "name": "x",
        "jump_hosts": {"jump1": {"type": "host", "transport": "ssh", "address": "192.0.2.10"}},
        "devices": {"PAGENT": {"type": "ios", "transport": "telnet", "address": "192.0.2.20", "jump_host": "jump1"}},
    }
    lab.write_access_info("x", data, lab_root)


def test_telnet_via_jump_guard_is_off_and_not_overridable(monkeypatch):
    # Enabled in code only after the tunnel safety tests (test_telnet_via_jump.py)
    # pass; a disabled flag stays disabled whatever the environment says.
    assert terminal.TELNET_VIA_JUMP_ENABLED is True
    monkeypatch.setattr(terminal, "TELNET_VIA_JUMP_ENABLED", False)
    monkeypatch.setenv("NETWORK_LAB_TELNET_VIA_JUMP_ENABLED", "1")
    import pathlib
    src = pathlib.Path(terminal.__file__).parent
    import re
    code_use = re.compile(r"TELNET_VIA_JUMP_ENABLED\s*=|\.TELNET_VIA_JUMP_ENABLED|import .*TELNET_VIA_JUMP_ENABLED")
    others = [p.name for p in src.rglob("*.py") if p.name != "terminal.py" and code_use.search(p.read_text())]
    assert others == []  # no other module (CLI, MCP server, lab) can touch the flag
    assert "environ" not in "".join(
        l for l in open(terminal.__file__) if "TELNET_VIA_JUMP" in l)
    cfg = {"transport": "telnet", "address": "192.0.2.20", "jump_host_config": {"address": "192.0.2.10"}}
    with pytest.raises(terminal.TerminalError, match="telnet via jump host is not enabled"):
        terminal._build_transport_command(cfg)


def test_guard_does_not_touch_other_transports():
    assert terminal._build_transport_command({"transport": "telnet", "address": "192.0.2.20"})[1] == [
        "telnet", "192.0.2.20", "23"]
    assert terminal._build_transport_command({"transport": "ssh", "address": "192.0.2.20"})[1][0] == "ssh"


def test_guard_blocks_before_any_process_or_binary_lookup(monkeypatch):
    called = []
    monkeypatch.setattr(terminal, "TELNET_VIA_JUMP_ENABLED", False)
    monkeypatch.setattr(terminal, "_require_binary", lambda b: called.append(b))
    monkeypatch.setattr(terminal, "_ensure_managed_session", lambda *a, **k: called.append("session"))
    with pytest.raises(terminal.TerminalError):
        terminal.open_device_terminal(
            "PAGENT", {"transport": "telnet", "address": "192.0.2.20", "password": "S3NTINEL",
                       "jump_host_config": {"address": "192.0.2.10"}})
    with pytest.raises(terminal.TerminalError):
        terminal.open_bootstrap_terminal(
            "PAGENT", {"transport": "telnet", "address": "192.0.2.20",
                       "jump_host_config": {"address": "192.0.2.10"}})
    assert called == []
