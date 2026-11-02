# SPDX-License-Identifier: Apache-2.0
"""Managed Terminal (the MCP-facing path) with REAL OpenSSH over loopback:
every hop is verified strictly; unknown/changed keys are refused with no
prompt that terminal_send could answer, and nothing is registered."""

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


# state of (jump, target): "T"=trusted, "U"=unknown, "C"=changed
CASES = [
    ("E", "T", "T", True),
    ("A_F", "U", "T", False),
    ("B_H", "C", "T", False),
    ("C_G", "T", "U", False),
    ("D", "T", "C", False),
    ("UU", "U", "U", False),
]


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
    before = Path(rt.known_hosts).read_bytes() if Path(rt.known_hosts).exists() else None
    err = open_terminal(cfg(st.port, sj.port))
    assert (err is None) == should_open, err
    text = wait_for(lambda: terminal.read_device("R1")["content"], ["RP/0/RP0/CPU0:R1#", "verification failed", "Host key"])
    after = Path(rt.known_hosts).read_bytes() if Path(rt.known_hosts).exists() else None
    assert after == before, "known_hosts must never change in a managed terminal"
    if should_open:
        assert "RP/0/RP0/CPU0:R1#" in text
        assert "Accepted" in log(rt, "t")
        return
    assert "RP/0/RP0/CPU0:R1#" not in text
    assert "authenticity of host" not in text and "yes/no" not in text  # nothing for terminal_send to answer
    assert "Accepted" not in log(rt, "t")  # the target never authenticated
    if jstate != "T":
        assert "Accepted" not in log(rt, "j")  # an untrusted jump never authenticated either
    # the pane is dead: a `yes` cannot reach any shell (disconnect-safe launch)
    wait_for(lambda: terminal.read_device("R1")["content"], ["verification failed"], 10)
    with pytest.raises(terminal.TerminalError):
        terminal.send_to_device("R1", "yes", None, True)
    assert (Path(rt.known_hosts).read_bytes() if Path(rt.known_hosts).exists() else None) == before


@pytest.mark.parametrize("tstate", ["T", "U", "C"])
def test_direct_ssh_is_strict(lab, tstate):
    rt, server = lab
    T, WRONG = make_host_key(rt, "T"), make_host_key(rt, "W")
    st = server("t", T, forced=True)
    if tstate == "T":
        trust(rt, T, st.port)
    elif tstate == "C":
        trust(rt, T, st.port, wrong=WRONG)
    before = Path(rt.known_hosts).read_bytes() if Path(rt.known_hosts).exists() else None
    err = open_terminal(cfg(st.port))
    assert (err is None) == (tstate == "T"), err
    text = wait_for(lambda: terminal.read_device("R1")["content"], ["RP/0/RP0/CPU0:R1#", "verification failed"])
    assert (Path(rt.known_hosts).read_bytes() if Path(rt.known_hosts).exists() else None) == before
    if tstate == "T":
        assert "RP/0/RP0/CPU0:R1#" in text
    else:
        assert "authenticity of host" not in text and "RP/0/RP0/CPU0:R1#" not in text
        assert "Accepted" not in log(rt, "t")
