# SPDX-License-Identifier: Apache-2.0
"""Boundary tests: host-key approval/removal is reachable only from the human
CLI; MCP and managed terminals cannot reach it; test hooks cannot be enabled
from env/YAML/CLI/MCP; production defaults are the real binaries."""

from __future__ import annotations

import os
import re
import types
from pathlib import Path

import pytest

from network_lab_mcp import hostkey_flow as hf
from network_lab_mcp import ssh_isolation as iso
from network_lab_mcp import terminal

SRC = Path(iso.__file__).parent
HOSTKEY_MODULES = ("hostkey_approval", "hostkey_flow", "hostkey_store")


def test_mcp_server_and_terminal_never_import_hostkey_modules():
    for name in ("mcp_server.py", "terminal.py", "lab.py", "connection_safety.py", "tunnel_safety.py"):
        text = (SRC / name).read_text()
        for mod in HOSTKEY_MODULES:
            assert not re.search(rf"\b{mod}\b", text), f"{name} references {mod}"


def test_only_cli_and_discovery_import_hostkey_flow():
    users = set()
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        if re.search(r"(import|from)[^\n]*\b(hostkey_flow|hostkey_approval)\b", text) and not path.name.startswith("hostkey_"):
            users.add(str(path.relative_to(SRC)))
    assert users <= {"discovery.py", "cli/main.py"}, users


def test_seven_mcp_tools_unchanged():
    text = (SRC / "mcp_server.py").read_text()
    assert len(re.findall(r"@mcp\.tool", text)) == 7


def test_test_injection_hook_is_not_reachable_from_production_modules():
    for path in SRC.rglob("*.py"):
        if path.name == "ssh_isolation.py":
            continue
        text = path.read_text()
        assert "install_for_tests" not in text, path.name
    assert iso.active_runtime() is None


def test_environment_cannot_enable_isolation_or_inject_paths(monkeypatch):
    for key in ("NETWORK_LAB_SSH_RUNTIME", "NETWORK_LAB_SSH", "SSH_RUNTIME", "NLM_SSH", "NETWORK_LAB_TEST_SSH"):
        monkeypatch.setenv(key, "/tmp/evil")
    assert iso.active_runtime() is None
    _, cmd = terminal._build_transport_command({"transport": "ssh", "address": "192.0.2.10", "port": 22})
    assert cmd[0] == "ssh" and "/tmp/evil" not in " ".join(cmd)


def test_production_defaults_use_pwd_database_not_home(monkeypatch, tmp_path):
    fake = types.SimpleNamespace(pw_dir=str(tmp_path / "pwhome"))
    monkeypatch.setattr("pwd.getpwuid", lambda uid: fake)
    monkeypatch.setenv("HOME", str(tmp_path / "otherhome"))
    assert hf.default_known_hosts() == str(tmp_path / "pwhome" / ".ssh" / "known_hosts")
    assert hf.known_hosts_path() == hf.default_known_hosts()
    assert not (tmp_path / "pwhome").exists()  # nothing was created or opened


def test_production_ssh_prefix_is_absolute_without_isolation_options():
    prefix = hf.ssh_prefix()
    assert os.path.isabs(prefix[0]) and prefix[0].endswith("/ssh")
    assert prefix[1:] == []


def test_production_command_enforces_strict_on_both_hops_and_no_J():
    cfg = {"transport": "ssh", "address": "192.0.2.20", "port": 22, "username": "u",
           "jump_host_config": {"address": "192.0.2.1", "port": 22, "username": "j"}}
    cmd = hf.build_ssh_command(cfg, kh="/tmp/known_hosts_example")
    assert "-J" not in cmd
    proxy = next(a for a in cmd if a.startswith("ProxyCommand="))
    assert "StrictHostKeyChecking=yes" in proxy and "-W" in proxy
    assert cmd.count("StrictHostKeyChecking=yes") == 1  # target
    assert "StrictHostKeyChecking=ask" not in " ".join(cmd)
    assert "UpdateHostKeys=no" in cmd and "CheckHostIP=no" in cmd and "VerifyHostKeyDNS=no" in cmd


def test_no_credential_ever_in_ssh_argv():
    cfg = {"transport": "ssh", "address": "192.0.2.20", "port": 22, "username": "u", "password": "SENTINEL_TARGET_PW",
           "jump_host_config": {"address": "192.0.2.1", "port": 22, "username": "j", "password": "SENTINEL_JUMP_PW"}}
    joined = " ".join(hf.build_ssh_command(cfg, kh="/tmp/k"))
    assert "SENTINEL" not in joined


def test_registration_command_disables_every_auth_method_on_the_hop():
    cfg = {"transport": "ssh", "address": "192.0.2.20", "port": 22, "username": "u",
           "jump_host_config": {"address": "192.0.2.1", "port": 22, "username": "j"}}
    cmd = hf.build_ssh_command(cfg, kh="/tmp/k", target_policy="ask", target_label="ED25519", no_auth_hop="target")
    for opt in ("PreferredAuthentications=none", "PasswordAuthentication=no", "PubkeyAuthentication=no",
                "GSSAPIAuthentication=no", "NumberOfPasswordPrompts=0"):
        assert opt in cmd
    proxy = next(a for a in cmd if a.startswith("ProxyCommand="))
    assert "PreferredAuthentications=none" not in proxy  # the jump hop may authenticate
    assert "HostKeyAlgorithms=ssh-ed25519" in cmd


def test_rsa_registration_never_enables_sha1():
    cmd = hf.build_ssh_command({"transport": "ssh", "address": "192.0.2.20", "port": 22, "username": "u"},
                               kh="/tmp/k", target_policy="ask", target_label="RSA")
    alg = next(a for a in cmd if a.startswith("HostKeyAlgorithms="))
    assert alg == "HostKeyAlgorithms=rsa-sha2-512,rsa-sha2-256"


def test_noninteractive_cli_gets_no_coordinator():
    from network_lab_mcp.cli import main as climain

    assert climain._make_hostkey_coordinator() is None  # pytest stdin/stdout are not a TTY


# ---- state machine -----------------------------------------------------------


def test_auth_refused_until_host_key_trusted():
    hop = hf.Hop("target", "192.0.2.20", 22, "u")
    for state in (hf.HostKeyState.PENDING, hf.HostKeyState.FAILED, hf.HostKeyState.APPROVED, hf.HostKeyState.VERIFIED):
        hop.host_state = state
        with pytest.raises(hf.HostKeyError):
            hop.advance_auth(hf.AuthState.TARGET_AUTH)
    hop.host_state = hf.HostKeyState.TRUSTED
    hop.advance_auth(hf.AuthState.TARGET_AUTH)
    assert hop.auth_state == hf.AuthState.TARGET_AUTH


def test_hops_for_each_connection_style():
    ssh = {"transport": "ssh", "address": "192.0.2.20", "port": 22, "username": "u"}
    assert [h.role for h in hf.hops_for(ssh)] == ["target"]
    jump = dict(ssh, jump_host_config={"address": "192.0.2.1", "port": 2222, "username": "j"})
    assert [(h.role, h.port) for h in hf.hops_for(jump)] == [("jump", 2222), ("target", 22)]
    tel = {"transport": "telnet", "address": "192.0.2.30", "port": 23}
    assert hf.hops_for(tel) == []
    telj = dict(tel, jump_host_config={"address": "192.0.2.1", "port": 22, "username": "j"})
    assert [h.role for h in hf.hops_for(telj)] == ["jump"]
