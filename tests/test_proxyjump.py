# SPDX-License-Identifier: Apache-2.0
"""Tests for single-hop OpenSSH ProxyJump support: the access-info
jump_hosts schema/validation (lab.py), device.get_device() resolution
attaching the right jump host, and terminal.py's argv construction. No real
network access or external dependency is used -- direct calls into the
existing command-builder seam are enough to prove argv correctness."""

from __future__ import annotations

import pytest

from network_lab_mcp import discovery, lab, terminal


# ---- jump_hosts schema / validation ----


def test_jump_hosts_mapping_accepted(lab_root):
    data = {
        "name": "jh",
        "jump_hosts": {"jump1": {"type": "host", "address": "192.0.2.10", "transport": "ssh"}},
        "devices": {},
    }
    lab.write_access_info("jh", data, lab_root)
    assert lab.load_access_info("jh", lab_root) == data


def test_jump_host_type_host_accepted_case_insensitive_and_abbreviated(lab_root):
    for raw_type in ("host", "HOST", "Host", "h"):
        data = {"name": "jh", "jump_hosts": {"jump1": {"type": raw_type}}, "devices": {}}
        lab.write_access_info("jh", data, lab_root)
        assert lab.load_access_info("jh", lab_root)["jump_hosts"]["jump1"]["type"] == raw_type


def test_jump_host_network_device_types_rejected(lab_root):
    for bad_type in ("iosxr", "iosxe", "nxos"):
        data = {"name": "jh", "jump_hosts": {"jump1": {"type": bad_type}}, "devices": {}}
        with pytest.raises(lab.LabConfigError, match="must have type 'host'"):
            lab.write_access_info("jh", data, lab_root)


def test_jump_host_ssh_transport_accepted(lab_root):
    data = {"name": "jh", "jump_hosts": {"jump1": {"type": "host", "transport": "ssh"}}, "devices": {}}
    lab.write_access_info("jh", data, lab_root)


def test_jump_host_non_ssh_transport_rejected(lab_root):
    data = {"name": "jh", "jump_hosts": {"jump1": {"type": "host", "transport": "telnet"}}, "devices": {}}
    with pytest.raises(lab.LabConfigError, match="transport 'ssh'"):
        lab.write_access_info("jh", data, lab_root)


def test_device_jump_host_reference_accepted(lab_root):
    data = {
        "name": "jh",
        "jump_hosts": {"jump1": {"type": "host", "transport": "ssh"}},
        "devices": {"R1": {"type": "iosxr", "transport": "ssh", "jump_host": "jump1"}},
    }
    lab.write_access_info("jh", data, lab_root)


def test_device_jump_host_missing_reference_rejected(lab_root):
    data = {
        "name": "jh",
        "jump_hosts": {},
        "devices": {"R1": {"type": "iosxr", "transport": "ssh", "jump_host": "does-not-exist"}},
    }
    with pytest.raises(lab.LabConfigError, match="unknown jump host"):
        lab.write_access_info("jh", data, lab_root)


def test_device_telnet_with_jump_host_rejected(lab_root):
    data = {
        "name": "jh",
        "jump_hosts": {"jump1": {"type": "host", "transport": "ssh"}},
        "devices": {"R1": {"type": "iosxr", "transport": "telnet", "jump_host": "jump1"}},
    }
    with pytest.raises(lab.LabConfigError, match="transport is not 'ssh'"):
        lab.write_access_info("jh", data, lab_root)


def test_no_multi_hop_support_is_structurally_impossible(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    # jump_hosts entries have no 'jump_host' field of their own in the
    # schema/validator -- even if manually written into YAML, resolution
    # (get_device()) never looks at it, so chaining silently has no effect.
    data = {
        "name": "jh",
        "jump_hosts": {
            "jump1": {"type": "host", "transport": "ssh", "jump_host": "jump2"},
            "jump2": {"type": "host", "transport": "ssh"},
        },
        "devices": {"R1": {"type": "iosxr", "transport": "ssh", "jump_host": "jump1"}},
    }
    lab.write_access_info("jh", data, lab_root)  # validator does not reject the stray field...
    _, resolved = _resolve(lab_root, "sample_lab", "jh", "R1")
    # ...but the resolved jump host config has no second hop attached.
    assert "jump_host_config" not in resolved["jump_host_config"]


def _resolve(lab_root, topology_name, access_info_name, device_name):
    settings = lab.read_settings(lab_root)
    settings["active_access_info"] = access_info_name
    lab.write_settings(settings, lab_root)
    return lab.get_device(device_name)


# ---- manually invalid committed YAML fails closed at get_device() time ----


def test_manually_invalid_jump_host_yaml_fails_closed_at_load(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    (lab_root / "access-info" / "sample_lab.yaml").write_text(
        "name: sample_lab\n"
        "jump_hosts:\n"
        "  jump1:\n"
        "    type: iosxr\n"  # manually edited to an invalid jump-host type
        "devices:\n"
        "  R1:\n"
        "    type: iosxr\n"
        "    jump_host: jump1\n",
        encoding="utf-8",
    )
    with pytest.raises(lab.LabConfigError, match="must have type 'host'"):
        lab.get_device("R1")


# ---- get_device() resolution attaches the right jump host ----


def test_get_device_attaches_resolved_jump_host_config(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    access_data = {
        "name": "sample_lab",
        "jump_hosts": {
            "jump1": {
                "type": "host",
                "address": "192.0.2.10",
                "transport": "ssh",
                "port": 22,
                "username": "jump-user",
                "password": "jump-pass",
            },
        },
        "devices": {
            "R1": {
                "type": "iosxr",
                "address": "192.0.2.11",
                "transport": "ssh",
                "port": 22,
                "username": "target-user",
                "password": "target-pass",
                "jump_host": "jump1",
            },
            "R2": {"type": "iosxr", "address": "192.0.2.12", "transport": "ssh"},
        },
    }
    lab.write_access_info("sample_lab", access_data, lab_root)

    _, resolved = lab.get_device("R1")
    assert resolved["jump_host_config"]["address"] == "192.0.2.10"
    assert resolved["jump_host_config"]["username"] == "jump-user"
    assert resolved["jump_host_config"]["password"] == "jump-pass"

    # Distinct credential scopes: the device's own username/password are
    # never overwritten by, or confused with, the jump host's.
    assert resolved["username"] == "target-user"
    assert resolved["password"] == "target-pass"

    # A device with no jump_host resolves with no jump_host_config at all.
    _, resolved_r2 = lab.get_device("R2")
    assert "jump_host_config" not in resolved_r2


# ---- terminal.py argv construction ----


def test_direct_ssh_has_no_proxyjump_argv():
    transport, command = terminal._build_transport_command(
        {"transport": "ssh", "address": "192.0.2.11", "port": 22, "username": "cisco"}
    )
    assert transport == "ssh"
    assert command == ["ssh", "-p", "22", "cisco@192.0.2.11"]
    assert "-J" not in command


def test_proxyjump_ssh_argv_has_native_dash_j_option():
    transport, command = terminal._build_transport_command(
        {
            "transport": "ssh",
            "address": "192.0.2.11",
            "port": 22,
            "username": "cisco",
            "jump_host_config": {
                "address": "192.0.2.10",
                "port": 22,
                "username": "jump-user",
            },
        }
    )
    assert transport == "ssh"
    assert command == ["ssh", "-J", "jump-user@192.0.2.10:22", "-p", "22", "cisco@192.0.2.11"]


def test_proxyjump_preserves_non_default_ports():
    _, command = terminal._build_transport_command(
        {
            "transport": "ssh",
            "address": "10.0.0.5",
            "port": 2222,
            "username": "target",
            "jump_host_config": {"address": "10.0.0.1", "port": 2200, "username": "jump"},
        }
    )
    assert command == ["ssh", "-J", "jump@10.0.0.1:2200", "-p", "2222", "target@10.0.0.5"]


def test_proxyjump_argv_never_contains_passwords():
    _, command = terminal._build_transport_command(
        {
            "transport": "ssh",
            "address": "192.0.2.11",
            "username": "cisco",
            "password": "target-secret",
            "jump_host_config": {"address": "192.0.2.10", "username": "jump-user", "password": "jump-secret"},
        }
    )
    joined = " ".join(command)
    assert "target-secret" not in joined
    assert "jump-secret" not in joined


def test_telnet_transport_unaffected_by_jump_host_key_presence():
    # A jump_host_config key should never be attached for telnet in
    # practice (validate_device_jump_host_references() rejects that
    # combination at commit time), but the argv builder itself only looks
    # at it for the ssh branch -- direct telnet behavior is unchanged.
    transport, command = terminal._build_transport_command({"transport": "telnet", "address": "192.0.2.1", "port": 23})
    assert transport == "telnet"
    assert command == ["telnet", "192.0.2.1", "23"]


# ---- Target-vs-jump-host password-prompt attribution must never send the
# wrong hop's password. This logic lives once in
# terminal.resolve_target_password_prompt() and is reused by both
# discovery._resolve_login_password() (a thin wrapper, tested directly
# below) and terminal.open_device_terminal()'s own private authentication
# (see tests/test_managed_terminal_auth.py for the full managed-open
# authentication suite; the ProxyJump-specific regression tests at the end
# of this section exercise the same shared attribution through
# open_device_terminal() directly). ----

_TARGET_CONFIG = {
    "transport": "ssh",
    "address": "192.0.2.11",
    "username": "target-user",
    "password": "target-secret",
    "jump_host_config": {"address": "192.0.2.10", "username": "jump-user", "password": "jump-secret"},
}

_DIRECT_CONFIG = {"transport": "ssh", "address": "192.0.2.11", "username": "target-user", "password": "target-secret"}


def test_login_password_direct_ssh_uses_target_password():
    password = discovery._resolve_login_password("R1", _DIRECT_CONFIG, "target-user@192.0.2.11's password: ")
    assert password == "target-secret"


def test_login_password_proxyjump_prompt_matching_target_uses_target_password():
    password = discovery._resolve_login_password("R1", _TARGET_CONFIG, "target-user@192.0.2.11's password: ")
    assert password == "target-secret"


def test_login_password_proxyjump_prompt_matching_jump_host_fails_closed():
    with pytest.raises(discovery.DiscoveryError, match="jump host prompted"):
        discovery._resolve_login_password("R1", _TARGET_CONFIG, "jump-user@192.0.2.10's password: ")


def test_login_password_proxyjump_unrecognized_prompt_format_fails_closed():
    with pytest.raises(discovery.DiscoveryError, match="could not safely determine"):
        discovery._resolve_login_password("R1", _TARGET_CONFIG, "Password: ")


def test_login_password_never_guesses_target_password_for_jump_prompt():
    # The specific bug this guards against: target-secret must never be
    # the value returned for a prompt that is actually the jump host's.
    with pytest.raises(discovery.DiscoveryError):
        password = discovery._resolve_login_password("R1", _TARGET_CONFIG, "jump-user@192.0.2.10's password: ")
        assert password != "target-secret"  # unreachable if it raised, kept for clarity


# ---- normal terminal_open() ProxyJump path remains fully unaffected ----


def test_terminal_open_proxyjump_argv_construction_unaffected_by_discovery_login_fix():
    """discovery._resolve_login_password() is never consulted by the
    production terminal_open() path -- it is only called from
    discovery._login(). Confirm terminal_open()'s own argv construction
    still produces the correct native ProxyJump command with distinct
    jump/target credentials, exactly as before this fix."""
    transport, command = terminal._build_transport_command(_TARGET_CONFIG)
    assert transport == "ssh"
    assert command == ["ssh", "-J", "jump-user@192.0.2.10:22", "-p", "22", "target-user@192.0.2.11"]
    joined = " ".join(command)
    assert "target-secret" not in joined
    assert "jump-secret" not in joined


# ---- terminal_open()'s own private authentication reuses the
# exact same ProxyJump target-vs-jump-host attribution -- see
# tests/test_managed_terminal_auth.py for the full managed-open
# authentication suite (direct SSH, no-password-configured, repeated
# prompt, failure, timeout, existing-session states, concurrency, secret
# non-leak). These three tests cover only the ProxyJump-specific
# regression: the jump host's own prompt must never receive the target's
# password, and vice versa, through open_device_terminal() itself (real
# isolated tmux, never a real router). ----


@pytest.fixture(autouse=True)
def _isolated_logs_for_this_file(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")


@pytest.fixture(autouse=True)
def _cleanup_proxyjump_sessions():
    yield
    for session in terminal.list_device_sessions():
        terminal.close_device_terminal(session["device"])


def _write_fake_ssh_script(tmp_path, name: str, body: str) -> list[str]:
    """A script *file* (never `bash -c "<inline text>"`): a shell only
    ever echoes the *invocation* line, never a script file's own
    contents, so this lets the body below freely use words like
    "password" without that literal text ever appearing in the pane
    before the script actually runs and prints it -- avoiding a
    false-positive early match against the echoed command source itself
    (see tests/test_managed_terminal_auth.py's module docstring for the
    full explanation)."""
    path = tmp_path / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)
    return ["bash", str(path)]


def test_terminal_open_proxyjump_target_prompt_sends_target_password_once(monkeypatch, tmp_path):
    # `stty -echo` + `read` mimics real OpenSSH's own password-entry
    # behavior (the terminal never echoes what is typed at a password
    # prompt); this is what makes "the secret is absent from the pane"
    # a meaningful assertion here, rather than an artifact of a fake
    # script that happens not to echo anything at all.
    script = _write_fake_ssh_script(
        tmp_path,
        "target_succeed.sh",
        'stty -echo\nprintf "target-user@192.0.2.11'"'"'s password: "\nread x\nstty echo\necho\necho AUTH-OK\nsleep 5\n',
    )
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", script))
    result = terminal.open_device_terminal("PJ1", _TARGET_CONFIG)
    assert result["transport"] == "ssh"
    snapshot = terminal.capture_device_terminal_view("PJ1")
    assert "AUTH-OK" in snapshot.pane_text
    assert "target-secret" not in snapshot.pane_text
    assert "jump-secret" not in snapshot.pane_text


def test_terminal_open_proxyjump_jump_host_prompt_never_sends_target_password(monkeypatch, tmp_path):
    script = _write_fake_ssh_script(
        tmp_path, "jump_prompt.sh", 'printf "jump-user@192.0.2.10'"'"'s password: "\nsleep 5\n'
    )
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", script))
    with pytest.raises(terminal.TerminalError, match="jump-host password prompt"):
        terminal.open_device_terminal("PJ1", _TARGET_CONFIG)
    # The newly-created, now-unusable session is cleaned up -- and,
    # either way, neither credential ever appears anywhere observable.
    assert "PJ1" not in {s["device"] for s in terminal.list_device_sessions()}


def test_terminal_open_key_auth_success_sends_no_password(monkeypatch, tmp_path):
    # No password/failure signal ever appears, so the initial wait runs to
    # its full bound before proceeding -- shrink it so this genuinely
    # timeout-bound case stays fast in tests (production keeps
    # _MANAGED_LOGIN_TIMEOUT_SECONDS unchanged).
    monkeypatch.setattr(terminal, "_MANAGED_LOGIN_TIMEOUT_SECONDS", 2)
    script = _write_fake_ssh_script(tmp_path, "key_auth.sh", "echo already-authenticated\nsleep 5\n")
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", script))
    result = terminal.open_device_terminal("PJ1", _TARGET_CONFIG)
    assert result["transport"] == "ssh"
    snapshot = terminal.capture_device_terminal_view("PJ1")
    assert "target-secret" not in snapshot.pane_text
    assert "already-authenticated" in snapshot.pane_text
