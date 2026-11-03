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
    # The terminal layer (used by MCP) may use only hostkey_tofu: first-use registration, no removal/approval.
    for name in ("mcp_server.py", "terminal.py", "lab.py", "connection_safety.py", "tunnel_safety.py"):
        text = (SRC / name).read_text()
        for mod in HOSTKEY_MODULES:
            assert not re.search(rf"\b{mod}\b", text), f"{name} references {mod}"
    assert not re.search(r"\bhostkey_tofu\b", (SRC / "mcp_server.py").read_text())


def test_tofu_module_cannot_remove_replace_or_approve():
    """What MCP/managed terminals can reach (via terminal.py) is only: add a key for an *unknown* host."""
    import ast

    tree = ast.parse((SRC / "hostkey_tofu.py").read_text())
    for node in ast.walk(tree):  # drop docstrings: prose may name what the module does not do
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and ast.get_docstring(node, clean=False):
            node.body = node.body[1:] or [ast.Pass()]
    code = ast.unparse(tree)
    for forbidden in ("remove_host", "make_backup", "hostkey_approval", "hostkey_flow", "TtyChannel", "/dev/tty",
                      "ApprovalRegistry", "'-R'", "StrictHostKeyChecking=no", "StrictHostKeyChecking=off",
                      "/dev/null"):
        assert forbidden not in code, forbidden
    assert sorted(set(re.findall(r"StrictHostKeyChecking=([a-z-]+)", code))) == ["accept-new", "yes"]


def test_terminal_reaches_tofu_only_for_hops_of_the_given_access_info_device(monkeypatch):
    """ensure_trusted derives its targets from the device config only (no host/port parameter at all)."""
    import inspect

    from network_lab_mcp import hostkey_tofu as tofu

    assert list(inspect.signature(tofu.ensure_trusted).parameters) == ["device_name", "device_config", "counters"]
    seen = []
    monkeypatch.setattr(tofu, "register_unknown", lambda dev, cfg, hop, **kw: seen.append((hop.role, hop.host, hop.port)))
    tofu.ensure_trusted("Router2", {"transport": "ssh", "address": "192.0.2.20", "port": 2222, "username": "u",
                                    "jump_host_config": {"address": "192.0.2.1", "port": 22, "username": "j"}})
    assert seen == [("jump", "192.0.2.1", 22), ("target", "192.0.2.20", 2222)]
    seen.clear()
    tofu.ensure_trusted("Legacy", {"transport": "telnet", "address": "192.0.2.30", "port": 23})
    assert seen == []  # Telnet-only device: no SSH host key applies


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
    prod = hf.known_hosts_path()
    assert prod.startswith(str(tmp_path / "pwhome")) and prod != hf.default_known_hosts()  # product file, not the user's
    assert prod.endswith(os.path.join("ssh", "known_hosts"))
    assert not (tmp_path / "pwhome").exists()  # nothing was created or opened


def test_production_ssh_prefix_is_absolute_without_isolation_options():
    from network_lab_mcp import hostkey_tofu
    prefix = hostkey_tofu.ssh_prefix()
    assert os.path.isabs(prefix[0]) and prefix[0].endswith("/ssh")
    assert prefix[1:] == []


def _opt_values(cmd, key):
    return [cmd[i + 1].split("=", 1)[1] for i, a in enumerate(cmd[:-1]) if a == "-o" and cmd[i + 1].startswith(key + "=")]


def test_production_command_enforces_strict_on_both_hops_and_no_J(monkeypatch, tmp_path):
    monkeypatch.setattr("pwd.getpwuid", lambda uid: types.SimpleNamespace(pw_dir=str(tmp_path / "h")))
    cfg = {"transport": "ssh", "address": "192.0.2.20", "port": 22, "username": "u",
           "jump_host_config": {"address": "192.0.2.1", "port": 22, "username": "j"}}
    cmd = hf.build_ssh_command(cfg)
    assert "-J" not in cmd
    proxy = next(a for a in cmd if a.startswith("ProxyCommand="))
    assert "StrictHostKeyChecking=yes" in proxy and "-W" in proxy
    assert _opt_values(cmd, "StrictHostKeyChecking") == ["yes"]  # target; the jump hop is in the ProxyCommand
    assert "StrictHostKeyChecking=accept-new" not in " ".join(cmd) and "StrictHostKeyChecking=ask" not in " ".join(cmd)
    for key, want in (("UpdateHostKeys", "no"), ("CheckHostIP", "no"), ("VerifyHostKeyDNS", "no"),
                      ("ControlMaster", "no"), ("ControlPath", "none"), ("ControlPersist", "no")):
        assert _opt_values(cmd, key) == [want], key
        assert f"{key}={want}" in proxy, key
    # both known_hosts files (product first, then the user's) on every hop, quoted
    (khv,) = _opt_values(cmd, "UserKnownHostsFile")
    assert khv.count('"') == 4 and "Application Support" in khv or "/.local/state/" in khv
    assert khv.endswith('.ssh/known_hosts"')


def test_no_credential_ever_in_ssh_argv():
    cfg = {"transport": "ssh", "address": "192.0.2.20", "port": 22, "username": "u", "password": "SENTINEL_TARGET_PW",
           "jump_host_config": {"address": "192.0.2.1", "port": 22, "username": "j", "password": "SENTINEL_JUMP_PW"}}
    from network_lab_mcp import hostkey_tofu as tofu

    joined = " ".join(hf.build_ssh_command(cfg)) + " ".join(tofu.build_probe_command(cfg, "target", "/tmp/k")) \
        + " ".join(tofu.build_probe_command(cfg, "jump", "/tmp/k"))
    assert "SENTINEL" not in joined


def test_probe_command_disables_every_auth_method_uses_dummy_user_and_no_multiplexing():
    from network_lab_mcp import hostkey_tofu as tofu

    cfg = {"transport": "ssh", "address": "192.0.2.20", "port": 22, "username": "realuser",
           "jump_host_config": {"address": "192.0.2.1", "port": 22, "username": "jumpuser"}}
    cmd = tofu.build_probe_command(cfg, "target", "/tmp/probe_kh")
    for key, want in (("PreferredAuthentications", "none"), ("PasswordAuthentication", "no"), ("PubkeyAuthentication", "no"),
                      ("GSSAPIAuthentication", "no"), ("KbdInteractiveAuthentication", "no"), ("HostbasedAuthentication", "no"),
                      ("NumberOfPasswordPrompts", "0"), ("IdentityAgent", "none"), ("BatchMode", "yes"),
                      ("UpdateHostKeys", "no"), ("HashKnownHosts", "no"), ("CheckHostIP", "no"), ("VerifyHostKeyDNS", "no"),
                      ("ControlMaster", "no"), ("ControlPath", "none"), ("ControlPersist", "no"),
                      ("SessionType", "none"), ("PermitLocalCommand", "no"), ("StrictHostKeyChecking", "accept-new")):
        assert _opt_values(cmd, key) == [want], key
    assert "-N" in cmd and "-T" in cmd and any(o.startswith("ConnectTimeout=") for o in cmd)
    assert cmd[-1] == "nlm-hostkey-probe@192.0.2.20" and "realuser" not in " ".join(cmd).split("ProxyCommand=")[0]
    (khv,) = _opt_values(cmd, "UserKnownHostsFile")
    assert khv == '"/tmp/probe_kh"'  # only the temporary file: never the product or user file
    proxy = next(a for a in cmd if a.startswith("ProxyCommand="))
    assert "jumpuser@192.0.2.1" in proxy and "StrictHostKeyChecking=yes" in proxy
    assert "PreferredAuthentications" not in proxy and "nlm-hostkey-probe" not in proxy  # jump hop: real auth, strict
    jump_probe = tofu.build_probe_command(cfg, "jump", "/tmp/probe_kh")
    assert jump_probe[-1] == "nlm-hostkey-probe@192.0.2.1" and not any(a.startswith("ProxyCommand=") for a in jump_probe)


def test_noninteractive_cli_still_gets_a_coordinator_without_approval_channel():
    from network_lab_mcp.cli import main as climain

    import inspect

    co = inspect.unwrap(climain._make_hostkey_coordinator)()  # the real factory; pytest stdin/stdout are not a TTY
    assert co.channel is None  # first-use TOFU needs no human; replacement review is impossible without a TTY
    assert co.recover_changed(hf.HostKeyChanged("R", "target", "192.0.2.20", 22), {"transport": "ssh", "address": "192.0.2.20"}) is False


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
