"""`discovery._login_ios_style()` -- the shared classic-IOS-style login
used by *both* IOS XE and classic IOS (named generically rather than
`_login_iosxe()`, since the same login sequence genuinely applies to
both). Needed
because IOS XR's `_login()` waits for an IOS-XR-specific prompt shape
(`RP/.../CPU0:hostname#`) that classic IOS/IOS XE never produces. Reuses
the same shared, already-tested primitives as everything else here
(terminal.wait_for_bootstrap_pattern, terminal.PASSWORD_PROMPT_RE,
terminal.resolve_target_password_prompt) -- this file proves the login
*sequencing* (optional Username: prompt, then Password:, then the exec
prompt) actually works end-to-end against a real (fake-command) tmux
session, not just by code inspection.

Fake login scripts are script *files* (never `bash -c "<inline text>"`),
exactly for the reason documented at length in
tests/test_managed_terminal_auth.py's module docstring: a shell only ever
echoes the *invocation* line, never a script file's own contents, which is
what avoids a false-positive early match against words like "password"
appearing in the echoed-but-not-yet-executed command line itself."""

from __future__ import annotations

import pytest

from network_lab_mcp import discovery, terminal

_SENTINEL_PASSWORD = "SENTINEL_IOS_STYLE_PASSWORD_XYZ"

_DIRECT_CONFIG = {
    "transport": "telnet",
    "address": "192.0.2.30",
    "password": _SENTINEL_PASSWORD,
}

_USERNAME_CONFIG = {
    "transport": "ssh",
    "address": "192.0.2.31",
    "username": "pagent-user",
    "password": _SENTINEL_PASSWORD,
}


@pytest.fixture(autouse=True)
def isolated_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")


@pytest.fixture(autouse=True)
def _short_timeouts(monkeypatch):
    monkeypatch.setattr(discovery, "LOGIN_TIMEOUT_SECONDS", 6)


@pytest.fixture(autouse=True)
def _cleanup_discovery_sessions():
    yield
    for device_id in terminal.list_discovery_device_ids():
        terminal.close_bootstrap_terminal(device_id)


def _write_script(tmp_path, name: str, body: str) -> list[str]:
    path = tmp_path / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)
    return ["bash", str(path)]


def _script_direct_password(tmp_path, hostname: str = "PAGENT") -> list[str]:
    body = (
        'stty -echo\n'
        'printf "Password: "\n'
        "read x\n"
        "stty echo\n"
        "echo\n"
        f"echo {hostname}#\n"
        "sleep 5\n"
    )
    return _write_script(tmp_path, "direct_password.sh", body)


def _script_username_then_password(tmp_path, hostname: str = "PAGENT") -> list[str]:
    body = (
        'printf "Username: "\n'
        "read u\n"
        'stty -echo\n'
        'printf "Password: "\n'
        "read x\n"
        "stty echo\n"
        "echo\n"
        f"echo {hostname}#\n"
        "sleep 5\n"
    )
    return _write_script(tmp_path, "username_then_password.sh", body)


def _script_never_prompts(tmp_path) -> list[str]:
    return _write_script(tmp_path, "never_prompts.sh", "echo booting...\nsleep 8\n")


def _script_full_session(tmp_path, hostname: str = "PAGENT") -> list[str]:
    """Answers the password prompt once, then keeps echoing the exec
    prompt back after every subsequent command line -- enough to let a
    *whole* bootstrap collector (login + several `show` commands) run to
    completion against a fake session, not just the login step alone."""
    body = (
        'stty -echo\n'
        'printf "Password: "\n'
        "read x\n"
        "stty echo\n"
        "echo\n"
        f"echo {hostname}#\n"
        "while read -r _cmd; do\n"
        f'  echo "{hostname}#"\n'
        "done\n"
    )
    return _write_script(tmp_path, "full_session.sh", body)


def _script_password_reject_if_empty(tmp_path) -> list[str]:
    """Models a real device rejecting an empty/no-configured password: it
    never reaches the exec prompt in that case, unlike the always-succeed
    fake scripts above."""
    body = (
        'stty -echo\n'
        'printf "Password: "\n'
        "read x\n"
        "stty echo\n"
        "echo\n"
        'if [ -z "$x" ]; then echo "%% Login invalid"; sleep 8; else echo PAGENT#; sleep 5; fi\n'
    )
    return _write_script(tmp_path, "password_reject_if_empty.sh", body)


def _monkeypatch_transport(monkeypatch, command: list[str], transport: str = "telnet") -> None:
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: (transport, command))


def test_direct_password_login_reaches_exec_prompt(tmp_path, monkeypatch):
    _monkeypatch_transport(monkeypatch, _script_direct_password(tmp_path))

    hostname = discovery._login_ios_style("PAGENT", _DIRECT_CONFIG)

    assert hostname == "PAGENT"


def test_username_then_password_login_reaches_exec_prompt(tmp_path, monkeypatch):
    _monkeypatch_transport(monkeypatch, _script_username_then_password(tmp_path), transport="ssh")

    hostname = discovery._login_ios_style("PAGENT", _USERNAME_CONFIG)

    assert hostname == "PAGENT"


def test_username_prompt_without_configured_username_fails_closed(tmp_path, monkeypatch):
    _monkeypatch_transport(monkeypatch, _script_username_then_password(tmp_path), transport="ssh")
    config = {"transport": "ssh", "address": "192.0.2.31", "password": _SENTINEL_PASSWORD}  # no username

    with pytest.raises(discovery.DiscoveryError, match="username"):
        discovery._login_ios_style("PAGENT", config)


def test_no_password_configured_is_rejected_by_the_device_and_fails_closed(tmp_path, monkeypatch):
    _monkeypatch_transport(monkeypatch, _script_password_reject_if_empty(tmp_path))
    config = {"transport": "telnet", "address": "192.0.2.30"}  # no password

    with pytest.raises(terminal.TerminalError):
        discovery._login_ios_style("PAGENT", config)


def test_login_never_reaching_a_prompt_times_out_closed(tmp_path, monkeypatch):
    """Mirrors discovery._login()'s own structure exactly: the initial
    wait's timeout surfaces as terminal.TerminalError, uncaught by
    _login_ios_style() itself -- the caller (_bootstrap_collect_iosxe()/
    _bootstrap_collect_ios()) converts it to a DiscoveryError, exactly
    like _login()/_bootstrap_collect() does for IOS XR."""
    _monkeypatch_transport(monkeypatch, _script_never_prompts(tmp_path))

    with pytest.raises(terminal.TerminalError, match="Timed out"):
        discovery._login_ios_style("PAGENT", _DIRECT_CONFIG)


def test_password_never_appears_in_the_raised_error(tmp_path, monkeypatch):
    _monkeypatch_transport(monkeypatch, _script_never_prompts(tmp_path))

    with pytest.raises(terminal.TerminalError) as exc_info:
        discovery._login_ios_style("PAGENT", _DIRECT_CONFIG)

    assert _SENTINEL_PASSWORD not in str(exc_info.value)


def test_bootstrap_collect_iosxe_converts_login_timeout_to_discovery_error(tmp_path, monkeypatch):
    _monkeypatch_transport(monkeypatch, _script_never_prompts(tmp_path))

    with pytest.raises(discovery.DiscoveryError, match="PAGENT"):
        discovery._bootstrap_collect_iosxe("PAGENT", _DIRECT_CONFIG)


def test_bootstrap_collect_ios_uses_the_same_shared_login(tmp_path, monkeypatch):
    """Classic IOS reuses _login_ios_style() exactly like IOS
    XE does, proven end-to-end (login through every collected command)
    rather than just by code inspection."""
    _monkeypatch_transport(monkeypatch, _script_full_session(tmp_path, hostname="PAGENT"))

    info = discovery._bootstrap_collect_ios("PAGENT", _DIRECT_CONFIG, include_l3=True)

    assert info["hostname"] == "PAGENT"
    assert "show_cdp_neighbors" in info
    assert "show_vrf" in info
    assert "show_ip_interface_brief" in info


def test_bootstrap_collect_ios_login_timeout_converts_to_discovery_error(tmp_path, monkeypatch):
    _monkeypatch_transport(monkeypatch, _script_never_prompts(tmp_path))

    with pytest.raises(discovery.DiscoveryError, match="PAGENT"):
        discovery._bootstrap_collect_ios("PAGENT", _DIRECT_CONFIG)
