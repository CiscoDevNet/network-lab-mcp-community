# SPDX-License-Identifier: Apache-2.0
"""Discovery disables terminal paging (`terminal length 0`)
immediately after successful login, before any show command, for IOS XE
and classic IOS collectors (IOS XR already did this -- see
`_bootstrap_collect()`). Fixes a real observed failure: a C9200L (IOS XE)
ran `show version` before pagination was disabled, its output stopped at
the device's own `--More--` pager prompt, and the whole device's
collection timed out.

This file has two kinds of tests:

- Pure unit-level ordering/count/fail-closed tests (`terminal.*` primitives
  monkeypatched directly -- no real tmux, no real waiting) proving the
  *sequencing* contract: paging is sent first, exactly once, and a paging
  failure never lets any Discovery show command run afterward.
- A real (fake-command) tmux integration test directly modeling the
  C9200L regression itself: the fake device behaves exactly
  like the real one -- if the first command isn't `terminal length 0`, it
  stalls at `--More--` forever, exactly reproducing the original bug;
  Discovery's own collector, unmodified, must not trigger that path."""

from __future__ import annotations

import pytest

from network_lab_mcp import discovery, terminal


@pytest.fixture(autouse=True)
def isolated_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")


@pytest.fixture(autouse=True)
def _cleanup_discovery_sessions():
    yield
    for device_id in terminal.list_discovery_device_ids():
        terminal.close_bootstrap_terminal(device_id)


# ---- Unit-level: exact ordering / exactly-once / fail-closed ----


def _fake_wait_always_succeeds(prompt_text):
    def fake_wait(device_id, pattern, timeout, baseline_text=None):
        return prompt_text

    return fake_wait


@pytest.mark.parametrize(
    "collector_name,prompt_text",
    [("_bootstrap_collect_iosxe", "SW3#"), ("_bootstrap_collect_ios", "PAGENT#")],
)
def test_paging_is_sent_immediately_after_login_before_any_show_command(monkeypatch, collector_name, prompt_text):
    monkeypatch.setattr(discovery, "_login_ios_style", lambda device_id, cfg: "SW3")
    sent_commands = []

    def fake_send(device_id, text, keys, enter):
        sent_commands.append(text)

    monkeypatch.setattr(terminal, "open_bootstrap_terminal", lambda *a, **k: None)
    monkeypatch.setattr(terminal, "send_to_bootstrap", fake_send)
    monkeypatch.setattr(terminal, "wait_for_bootstrap_pattern", _fake_wait_always_succeeds(prompt_text))
    monkeypatch.setattr(terminal, "read_bootstrap", lambda device_id, lines=terminal.HISTORY_LIMIT: "")

    getattr(discovery, collector_name)("SW3", {"transport": "ssh", "address": "192.0.2.1"})

    assert sent_commands[0] == "terminal length 0"
    assert sent_commands[1] == "show version"


def test_paging_command_is_sent_exactly_once_per_collection(monkeypatch):
    monkeypatch.setattr(discovery, "_login_ios_style", lambda device_id, cfg: "SW3")
    sent_commands = []

    def fake_send(device_id, text, keys, enter):
        sent_commands.append(text)

    monkeypatch.setattr(terminal, "open_bootstrap_terminal", lambda *a, **k: None)
    monkeypatch.setattr(terminal, "send_to_bootstrap", fake_send)
    monkeypatch.setattr(terminal, "wait_for_bootstrap_pattern", _fake_wait_always_succeeds("SW3#"))
    monkeypatch.setattr(terminal, "read_bootstrap", lambda device_id, lines=terminal.HISTORY_LIMIT: "")

    discovery._bootstrap_collect_iosxe("SW3", {"transport": "ssh", "address": "192.0.2.1"})

    assert sent_commands.count("terminal length 0") == 1


def test_paging_command_is_sent_exactly_once_for_iosxr_too(monkeypatch):
    """IOS XR already disabled paging -- confirm the
    shared _disable_terminal_paging() helper preserves that exactly-once
    behavior for IOS XR as well."""
    monkeypatch.setattr(discovery, "_login", lambda device_id, cfg: "R1")
    sent_commands = []

    def fake_send(device_id, text, keys, enter):
        sent_commands.append(text)

    monkeypatch.setattr(terminal, "open_bootstrap_terminal", lambda *a, **k: None)
    monkeypatch.setattr(terminal, "send_to_bootstrap", fake_send)
    monkeypatch.setattr(terminal, "wait_for_bootstrap_pattern", _fake_wait_always_succeeds("RP/0/RP0/CPU0:R1#"))
    monkeypatch.setattr(terminal, "read_bootstrap", lambda device_id, lines=terminal.HISTORY_LIMIT: "")

    discovery._bootstrap_collect("R1", {"transport": "ssh", "address": "192.0.2.1"})

    assert sent_commands.count("terminal length 0") == 1
    assert sent_commands[0] == "terminal length 0"
    assert sent_commands[1] == "show version"


def test_paging_initialization_failure_is_bounded_and_no_show_command_follows(monkeypatch):
    """If the prompt never returns after `terminal length
    0`, the whole device fails closed -- no Discovery show command is
    ever attempted while terminal (page-length) state is unknown."""
    monkeypatch.setattr(discovery, "_login_ios_style", lambda device_id, cfg: "SW3")
    sent_commands = []

    def fake_send(device_id, text, keys, enter):
        sent_commands.append(text)

    def fake_wait(device_id, pattern, timeout, baseline_text=None):
        raise terminal.TerminalError(f"Timed out after {timeout:.0f}s waiting for expected output on session 'x'.")

    monkeypatch.setattr(terminal, "open_bootstrap_terminal", lambda *a, **k: None)
    monkeypatch.setattr(terminal, "send_to_bootstrap", fake_send)
    monkeypatch.setattr(terminal, "wait_for_bootstrap_pattern", fake_wait)
    monkeypatch.setattr(terminal, "read_bootstrap", lambda device_id, lines=terminal.HISTORY_LIMIT: "")

    with pytest.raises(discovery.DiscoveryError, match="disabling terminal paging") as exc_info:
        discovery._bootstrap_collect_iosxe("SW3", {"transport": "ssh", "address": "192.0.2.1"})

    # The diagnostic identifies *this* phase, not a generic/other-command
    # failure, and never leaks any credential.
    assert "SW3" in str(exc_info.value)
    assert sent_commands == ["terminal length 0"]  # never reached "show version"


def test_paging_initialization_error_never_leaks_credentials(monkeypatch):
    _sentinel_password = "SENTINEL_PAGING_PASSWORD_XYZ"
    monkeypatch.setattr(discovery, "_login_ios_style", lambda device_id, cfg: "SW3")

    def fake_wait(device_id, pattern, timeout, baseline_text=None):
        raise terminal.TerminalError("Timed out after 25s waiting for expected output on session 'x'.")

    monkeypatch.setattr(terminal, "open_bootstrap_terminal", lambda *a, **k: None)
    monkeypatch.setattr(terminal, "send_to_bootstrap", lambda *a, **k: None)
    monkeypatch.setattr(terminal, "wait_for_bootstrap_pattern", fake_wait)
    monkeypatch.setattr(terminal, "read_bootstrap", lambda device_id, lines=terminal.HISTORY_LIMIT: "")

    with pytest.raises(discovery.DiscoveryError) as exc_info:
        discovery._bootstrap_collect_iosxe(
            "SW3", {"transport": "ssh", "address": "192.0.2.1", "password": _sentinel_password}
        )

    assert _sentinel_password not in str(exc_info.value)


# ---- Real (fake-tmux) integration: delayed prompt + the C9200L pager regression model ----


def _write_script(tmp_path, name: str, body: str) -> list[str]:
    path = tmp_path / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)
    return ["bash", str(path)]


def _monkeypatch_transport(monkeypatch, command: list[str]) -> None:
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", command))


_CONFIG = {"transport": "ssh", "address": "192.0.2.99", "username": "u", "password": "SENTINEL_PAGING_XYZ"}


def test_show_version_is_not_sent_before_the_delayed_paging_prompt_returns(tmp_path, monkeypatch):
    """The paging-disable prompt is delayed by a couple of
    (real, short) seconds -- prove Discovery genuinely waited for it
    (rather than racing ahead) by measuring elapsed time and confirming
    `show version` still completes correctly afterward.

    Prints several non-prompt filler lines ("...no prompt yet...") before
    the sleep, so the *stale* first "SW3#" (still printed right after
    Password:) scrolls out of `_wait_for_pattern()`'s own last-5-lines
    match window well before the genuinely new prompt appears -- this
    test is about proving the *paging* wait blocks correctly, not about
    exercising `_wait_for_pattern()`'s own separate stale-prompt-window
    tolerance (see tests/test_terminal_stale_prompt.py for that)."""
    body = (
        'stty -echo\n'
        'printf "Password: "\n'
        "read x\n"
        "stty echo\n"
        "echo\n"
        "echo SW3#\n"
        "read cmd1\n"
        'echo "...no prompt yet 1..."\n'
        'echo "...no prompt yet 2..."\n'
        'echo "...no prompt yet 3..."\n'
        'echo "...no prompt yet 4..."\n'
        'echo "...no prompt yet 5..."\n'
        "sleep 1.5\n"  # the device is slow to acknowledge `terminal length 0`
        "echo SW3#\n"
        "while read -r cmd; do\n"
        '  if [ "$cmd" = "show version" ]; then echo "Cisco IOS XE Software, Version 17.18.02"; fi\n'
        "  echo SW3#\n"
        "done\n"
    )
    _monkeypatch_transport(monkeypatch, _write_script(tmp_path, "delayed_paging_prompt.sh", body))

    import time

    started = time.monotonic()
    info = discovery._bootstrap_collect_iosxe("SW3", _CONFIG)
    elapsed = time.monotonic() - started

    assert elapsed >= 1.4  # genuinely waited for the delayed prompt
    assert "Cisco IOS XE Software" in info["show_version"]


def _fake_c9200l_script(tmp_path) -> list[str]:
    """Models the real C9200L failure precisely: if the *first* command
    after login isn't `terminal length 0`, whatever was sent instead
    stalls at the device's own `--More--` pager forever (no further
    output, no prompt -- a true stall, exactly like the real device). If
    `terminal length 0` *is* sent first, the device behaves normally for
    every subsequent command.

    The "bug" branch prints a few extra filler lines before `--More--` so
    the *stale* login prompt ("SW3#", already outside this branch's own
    new output) scrolls past `_wait_for_pattern()`'s last-5-lines match
    window before the stall -- otherwise the still-nearby stale prompt
    line (an orthogonal, pre-existing characteristic of the shared
    tail-based matcher) could satisfy the pattern
    prematurely and mask the very stall this model exists to prove."""
    body = (
        'stty -echo\n'
        'printf "Password: "\n'
        "read x\n"
        "stty echo\n"
        "echo\n"
        "echo SW3#\n"
        "read cmd1\n"
        'if [ "$cmd1" != "terminal length 0" ]; then\n'
        '  echo "Cisco IOS XE Software, Version 17.18.02"\n'
        '  echo "-- filler line a --"\n'
        '  echo "-- filler line b --"\n'
        '  echo "-- filler line c --"\n'
        '  echo "-- filler line d --"\n'
        '  echo "--More--"\n'
        "  while read -r _c; do :; done\n"  # true stall: never responds again
        "fi\n"
        "echo SW3#\n"
        "while read -r cmd; do\n"
        '  if [ "$cmd" = "show version" ]; then echo "Cisco IOS XE Software, Version 17.18.02 (full)"; fi\n'
        "  echo SW3#\n"
        "done\n"
    )
    return _write_script(tmp_path, "c9200l_pager_model.sh", body)


def test_real_pager_regression_model_stalls_without_paging_disabled_first(tmp_path, monkeypatch):
    """Control test: proves the fake script faithfully reproduces the
    real C9200L bug when paging is *not* disabled first -- confirms
    the model itself is meaningful, not merely that our fix happens to
    pass against a script we designed to always succeed."""
    _monkeypatch_transport(monkeypatch, _fake_c9200l_script(tmp_path))

    terminal.open_bootstrap_terminal("SW3", _CONFIG)
    terminal.wait_for_bootstrap_pattern("SW3", terminal.PASSWORD_PROMPT_RE, 10)
    terminal.send_to_bootstrap("SW3", "SENTINEL_PAGING_XYZ", None, True)
    terminal.wait_for_bootstrap_pattern("SW3", discovery._IOS_STYLE_PROMPT_RE, 10)

    # Sending `show version` *first* (skipping paging-disable) reproduces
    # the exact real failure: stuck at `--More--`, prompt never returns.
    with pytest.raises(terminal.TerminalError, match="Timed out"):
        discovery._run_command("SW3", "show version", discovery._IOS_STYLE_PROMPT_RE, timeout=2)


def test_bootstrap_collect_iosxe_disables_paging_and_never_stalls_at_more(tmp_path, monkeypatch):
    """The actual fix, proven against the same real regression model: with
    _disable_terminal_paging() in place, the collector's own real command
    sequence never triggers the `--More--` stall and completes with the
    full `show version` output."""
    _monkeypatch_transport(monkeypatch, _fake_c9200l_script(tmp_path))

    info = discovery._bootstrap_collect_iosxe("SW3", _CONFIG)

    assert "Cisco IOS XE Software, Version 17.18.02 (full)" in info["show_version"]


# ---- paging is Discovery-only, never sent by managed terminal_open() ----


@pytest.fixture(autouse=True)
def _cleanup_managed_sessions():
    yield
    for session in terminal.list_device_sessions():
        terminal.close_device_terminal(session["device"])


def test_terminal_open_never_sends_terminal_length_0(tmp_path, monkeypatch):
    body = (
        'stty -echo\n'
        'printf "%s\'s password: " "r1-user@192.0.2.20"\n'
        "read x\n"
        "stty echo\n"
        "echo\n"
        "echo AUTH-OK\n"
        "while read -r _cmd; do echo ignored; done\n"
    )
    _monkeypatch_transport(monkeypatch, _write_script(tmp_path, "managed_open.sh", body))
    monkeypatch.setattr(terminal, "_MANAGED_LOGIN_TIMEOUT_SECONDS", 6)
    monkeypatch.setattr(terminal, "_MANAGED_AUTH_SETTLE_TIMEOUT_SECONDS", 6)

    sent = []
    real_send = terminal._send_literal_text

    def tracked(session_name, text):
        sent.append(text)
        return real_send(session_name, text)

    monkeypatch.setattr(terminal, "_send_literal_text", tracked)

    terminal.open_device_terminal(
        "R1",
        {"transport": "ssh", "address": "192.0.2.20", "username": "r1-user", "password": "SENTINEL_PAGING_MGMT_XYZ"},
    )

    assert "terminal length 0" not in sent


# ---- per-device paging init, no global state ----


def test_two_devices_disable_paging_concurrently_no_global_lock(tmp_path, monkeypatch):
    """Two different devices' paging-disable steps happen concurrently,
    proving there is no accidental global pager lock/flag serializing
    them -- only the existing, per-device Discovery bootstrap session
    lock applies."""
    import threading

    barrier = threading.Barrier(2, timeout=5)
    real_disable = discovery._disable_terminal_paging

    def synced_disable(device_id, prompt_re):
        real_disable(device_id, prompt_re)
        barrier.wait()  # only satisfied if both devices reach this point together

    monkeypatch.setattr(discovery, "_disable_terminal_paging", synced_disable)

    def make_script(name):
        body = (
            'stty -echo\n'
            'printf "Password: "\n'
            "read x\n"
            "stty echo\n"
            "echo\n"
            "echo DEV#\n"
            "while read -r _cmd; do echo DEV#; done\n"
        )
        return _write_script(tmp_path, f"{name}.sh", body)

    address_to_script = {"192.0.2.201": make_script("dev1"), "192.0.2.202": make_script("dev2")}

    def fake_transport(config, **kw):
        return "ssh", address_to_script[config["address"]]

    monkeypatch.setattr(terminal, "_build_transport_command", fake_transport)

    results: dict[str, dict] = {}
    errors: dict[str, BaseException] = {}

    def run(device_id, address):
        try:
            results[device_id] = discovery._bootstrap_collect_iosxe(
                device_id, {"transport": "ssh", "address": address, "password": "x"}
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors` for the assertion below
            errors[device_id] = exc

    t1 = threading.Thread(target=run, args=("DEV1", "192.0.2.201"))
    t2 = threading.Thread(target=run, args=("DEV2", "192.0.2.202"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, errors
    assert set(results) == {"DEV1", "DEV2"}
