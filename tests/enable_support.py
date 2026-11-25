# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Synthetic IOS / IOS XE device for enable-password tests.

A small Python program run inside a real (isolated) tmux pane. It never
touches a network. Its behaviour comes from a JSON scenario file (the expected
enable secret lives there, never in argv) and it records *events* -- counts of
`enable` commands, enable-secret attempts (ok/bad, never the value), and any
other command -- to a file the tests read back. All credentials are synthetic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEVICE_SOURCE = r'''
import json, sys, termios, time
cfg = json.load(open(sys.argv[1]))
host = cfg["host"]; mode = cfg.get("mode", "ok"); secret = cfg.get("secret", "")
def ev(text):
    with open(cfg["events"], "a") as f:
        f.write(text + "\n")
def out(text):
    sys.stdout.write(text); sys.stdout.flush()
def read_secret():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    if not cfg.get("local_echo"):
        new[3] = new[3] & ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSADRAIN, new)
    try:
        line = sys.stdin.readline()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return line.rstrip("\n")
if cfg.get("raw_prompt"):
    out(cfg["raw_prompt"]); time.sleep(600)
for line in cfg.get("banner", []):
    out(line + "\n")
level = "priv" if cfg.get("start_priv") else "user"
cfg_mode = False
def prompt():
    if cfg.get("start_prompt_override") and not hasattr(prompt, "n"):
        prompt.n = 1; return out(cfg["start_prompt_override"])
    if cfg_mode: return out(host + "(config)#")
    out(host + (">" if level == "user" else "#"))
while True:
    prompt()
    line = sys.stdin.readline()
    if not line:
        break
    cmd = line.strip()
    if cmd == "enable" and level == "user":
        ev("enable")
        if mode == "silent":
            time.sleep(600)
        if mode == "disconnect":
            sys.exit(0)
        if mode == "nopass":
            level = "priv"; continue
        if mode == "diffhost":
            out("\nOTHER#"); time.sleep(600)
        if mode == "config_prompt":
            out("\n"); cfg_mode = True; level = "priv"; continue
        if mode == "no_password_set":
            out("% No password set\n"); continue
        attempts = cfg.get("attempts", 1)
        for i in range(attempts):
            out("\nPassword: ")
            got = read_secret()
            ev("secret_ok" if got == secret and mode != "reject" else "secret_bad")
            if got == secret and mode != "reject":
                if mode in ("echo_full", "echo_split", "echo_then_exit", "echo_with_prompt"):
                    if mode == "echo_split":
                        out("\n" + got[:6]); time.sleep(0.4); out(got[6:] + "\n")
                    else:
                        out("\n" + got + "\n")
                    if mode == "echo_then_exit":
                        sys.exit(0)
                else:
                    out("\n")
                level = "priv"
                break
            out("\n" + cfg.get("fail_text", "% Access denied") + "\n")
            if cfg.get("reprompt") and i + 1 < attempts:
                continue
            break
    elif cmd:
        ev("other:" + cmd)
        out(line.replace(cmd, "") + "output for " + cmd + "\n")
'''


def write_device(tmp_path: Path, name: str, **scenario) -> list[str]:
    """Return the transport argv for a synthetic device. `scenario` keys:
    host, mode, secret, start_priv, banner, attempts, reprompt, fail_text,
    local_echo."""
    base = tmp_path / name
    script = base.with_suffix(".py")
    script.write_text(DEVICE_SOURCE)
    events = base.with_suffix(".events")
    events.write_text("")
    scenario = {"host": "L3SW1", "events": str(events), **scenario}
    scenario_path = base.with_suffix(".json")
    scenario_path.write_text(json.dumps(scenario))
    scenario_path.chmod(0o600)
    return [sys.executable, str(script), str(scenario_path)]


def events_of(argv: list[str]) -> list[str]:
    path = Path(argv[2]).with_suffix(".events")
    return [line for line in path.read_text().splitlines() if line]


def count(argv: list[str], prefix: str) -> int:
    return sum(1 for e in events_of(argv) if e == prefix or e.startswith(prefix))
