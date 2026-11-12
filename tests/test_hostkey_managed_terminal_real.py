# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Managed Terminal (the MCP-facing path) with REAL OpenSSH over loopback:
an unknown hop is registered first (TOFU: authentication-free probe, key
persisted in the product known_hosts) and then verified strictly; a changed
or other-type key is refused with no prompt that terminal_send could answer
and nothing is replaced or added."""

from __future__ import annotations

import getpass
import os
import time
from pathlib import Path

import pytest

from network_lab_mcp import ssh_isolation as iso, terminal
from hostkey_support import Sshd, make_host_key, make_real_runtime, real_binaries_available

pytestmark = pytest.mark.skipif(not real_binaries_available(), reason="OpenSSH binaries unavailable")
USER = getpass.getuser()
ROUTER = "#!/bin/sh\nprintf 'RP/0/RP0/CPU0:R1#'\nwhile read l; do printf '\\r\\nRP/0/RP0/CPU0:R1#'; done\n"


@pytest.fixture
def lab(tmp_path):
    before = iso.snapshot_real_ssh_area()
    rt = make_real_runtime(tmp_path)
    router = Path(rt.sandbox) / "router.sh"
    router.write_text(ROUTER)
    router.chmod(0o700)
    servers = []

    def server(name, key, *, forced=False, port=None):
        s = Sshd(rt, name, key, port=port)
        if forced:
            auth = Path(rt.sandbox) / f"authorized_{name}"
            auth.write_text(f'command="{router}" ' + Path(rt.identity + ".pub").read_text())
            os.chmod(auth, 0o600)
        s.start()
        servers.append(s)
        return s

    yield rt, server
    try:
        terminal.close_device_terminal("R1")
    except Exception:
        pass
    for s in servers:
        s.stop()
    iso.uninstall_for_tests()
    assert all(s.stopped() for s in servers)
    assert iso.snapshot_real_ssh_area() == before


def trust(rt, key, port, *, wrong=None):
    k = wrong or key
    with open(rt.known_hosts, "a") as fh:
        fh.write(f"[127.0.0.1]:{port} {k['type']} {k['blob']}\n")
    os.chmod(rt.known_hosts, 0o600)


def cfg(tport, jport=None):
    c = {"transport": "ssh", "address": "127.0.0.1", "port": tport, "username": USER}
    if jport:
        c["jump_host_config"] = {"address": "127.0.0.1", "port": jport, "username": USER}
    return c


def wait_for(read, needles, timeout=25):
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = read()
        if any(n in text for n in needles):
            return text
        time.sleep(0.3)
    return text


def open_terminal(config):
    """terminal_open as the MCP tool would call it. A refused connection ends
    the pane before login: terminal_open reports that as an error and the
    pane stays readable (this is the refusal we expect for unknown/changed keys)."""
    try:
        terminal.open_device_terminal("R1", config)
        return None
    except terminal.TerminalError as exc:
        return str(exc)


def log(rt, name):
    return Path(rt.sandbox, f"sshd_{name}.log").read_text()


# state of (jump, target): "T"=trusted, "U"=unknown (TOFU), "C"=changed (refused)
CASES = [
    ("E", "T", "T", True),
    ("A_F", "U", "T", True),
    ("B_H", "C", "T", False),
    ("C_G", "T", "U", True),
    ("D", "T", "C", False),
    ("UU", "U", "U", True),
]


def read_or_none(path):
    return Path(path).read_bytes() if Path(path).exists() else None


@pytest.mark.parametrize("name,jstate,tstate,should_open", CASES)
def test_proxyjump_matrix(lab, name, jstate, tstate, should_open):
    rt, server = lab
    J, T, WRONG = make_host_key(rt, "J"), make_host_key(rt, "T"), make_host_key(rt, "W")
    sj, st = server("j", J), server("t", T, forced=True)
    if jstate == "T":
        trust(rt, J, sj.port)
    elif jstate == "C":
        trust(rt, J, sj.port, wrong=WRONG)
    if tstate == "T":
        trust(rt, T, st.port)
    elif tstate == "C":
        trust(rt, T, st.port, wrong=WRONG)
    user_before = read_or_none(rt.known_hosts)
    prod_before = read_or_none(rt.product_known_hosts)
    err = open_terminal(cfg(st.port, sj.port))
    assert (err is None) == should_open, err
    text = wait_for(lambda: terminal.read_device("R1")["content"], ["RP/0/RP0/CPU0:R1#", "verification failed", "Host key"])
    assert read_or_none(rt.known_hosts) == user_before, "the user's known_hosts must never change"
    expected_new = "".join(
        f"[127.0.0.1]:{port} {k['type']} {k['blob']}\n"
        for state, k, port in (("U" == jstate, J, sj.port), ("U" == tstate, T, st.port)) if state
    )
    assert (read_or_none(rt.product_known_hosts) or b"").decode() == (prod_before or b"").decode() + expected_new
    assert "authenticity of host" not in text and "yes/no" not in text  # nothing for terminal_send to answer
    if should_open:
        assert "RP/0/RP0/CPU0:R1#" in text
        assert "Accepted" in log(rt, "t")
        return
    assert "RP/0/RP0/CPU0:R1#" not in text
    assert "Accepted" not in log(rt, "t")  # the target never authenticated
    if jstate != "T":
        assert "Accepted" not in log(rt, "j")  # a changed jump key was never authenticated against
    # the pane is dead: a `yes` cannot reach any shell (disconnect-safe launch)
    wait_for(lambda: terminal.read_device("R1")["content"], ["verification failed"], 10)
    with pytest.raises(terminal.TerminalError):
        terminal.send_to_device("R1", "yes", None, True)
    assert read_or_none(rt.known_hosts) == user_before


@pytest.mark.parametrize("tstate", ["T", "U", "C"])
def test_direct_ssh_tofu_and_strict(lab, tstate):
    rt, server = lab
    T, WRONG = make_host_key(rt, "T"), make_host_key(rt, "W")
    st = server("t", T, forced=True)
    if tstate == "T":
        trust(rt, T, st.port)
    elif tstate == "C":
        trust(rt, T, st.port, wrong=WRONG)
    user_before = read_or_none(rt.known_hosts)
    err = open_terminal(cfg(st.port))
    assert (err is None) == (tstate != "C"), err
    text = wait_for(lambda: terminal.read_device("R1")["content"], ["RP/0/RP0/CPU0:R1#", "verification failed"])
    assert read_or_none(rt.known_hosts) == user_before
    assert "authenticity of host" not in text
    if tstate == "C":
        assert "RP/0/RP0/CPU0:R1#" not in text and "Accepted" not in log(rt, "t")
        assert not Path(rt.product_known_hosts).exists() or Path(rt.product_known_hosts).read_text() == ""
    else:
        assert "RP/0/RP0/CPU0:R1#" in text
        if tstate == "U":
            assert Path(rt.product_known_hosts).read_text() == f"[127.0.0.1]:{st.port} {T['type']} {T['blob']}\n"


def test_tofu_failure_in_managed_terminal_starts_no_session_and_sends_nothing(lab, monkeypatch):
    from network_lab_mcp import hostkey_tofu

    rt, server = lab
    T = make_host_key(rt, "T")
    st = server("t", T, forced=True)

    def boom(*a, **kw):
        raise hostkey_tofu.HostKeyError("disk full", hostkey_tofu.TOFU_STORAGE_ERROR)

    monkeypatch.setattr(hostkey_tofu, "append_entry", boom)
    err = open_terminal(cfg(st.port))
    assert err and "TOFU_STORAGE_ERROR" in err and "Nothing was sent" in err
    assert terminal.list_device_sessions() == [] or all(x["device"] != "R1" for x in terminal.list_device_sessions())
    assert "Accepted" not in log(rt, "t")


def test_known_hosts_paths_with_spaces_work_end_to_end(tmp_path):
    """The macOS product path contains a space ("Application Support"): quoting must survive the tmux launch,
    the ProxyCommand child and the wrapper, for the TOFU probe and for the strict connection."""
    import dataclasses
    import json

    before = iso.snapshot_real_ssh_area()
    rt0 = make_real_runtime(tmp_path, install=False)
    spaced = Path(rt0.sandbox) / "Application Support" / "Network Lab MCP" / "ssh"
    spaced.mkdir(parents=True, mode=0o700)
    rt = dataclasses.replace(rt0, product_known_hosts=str(spaced / "known_hosts"))
    Path(rt.policy_file).write_text(json.dumps(dataclasses.asdict(rt)))
    iso.install_for_tests(rt)
    assert " " in rt.product_known_hosts
    router = Path(rt.sandbox) / "router.sh"
    router.write_text(ROUTER)
    router.chmod(0o700)
    J, T = make_host_key(rt, "J"), make_host_key(rt, "T")
    servers = []
    try:
        for name, key in (("j", J), ("t", T)):
            s = Sshd(rt, name, key)
            if name == "t":
                auth = Path(rt.sandbox) / "authorized_t"
                auth.write_text(f'command="{router}" ' + Path(rt.identity + ".pub").read_text())
                os.chmod(auth, 0o600)
            s.start()
            servers.append(s)
        sj, st = servers
        assert open_terminal(cfg(st.port, sj.port)) is None
        text = wait_for(lambda: terminal.read_device("R1")["content"], ["RP/0/RP0/CPU0:R1#"])
        assert "RP/0/RP0/CPU0:R1#" in text and "Accepted" in log(rt, "t")
        assert Path(rt.product_known_hosts).read_text().count("\n") == 2  # jump + target registered
    finally:
        try:
            terminal.close_device_terminal("R1")
        except Exception:
            pass
        for s in servers:
            s.stop()
        iso.uninstall_for_tests()
    assert iso.snapshot_real_ssh_area() == before
