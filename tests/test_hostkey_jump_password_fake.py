# SPDX-License-Identifier: Apache-2.0
"""Jump-server password handling in registration/probe connections, verified
with a FAKE ssh that scripts OpenSSH's prompts on a real pty. Sentinels are
fictional. ssh-keygen (real) runs only through the isolation wrapper."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from network_lab_mcp import hostkey_flow as hf
from network_lab_mcp import hostkey_store as hs
from network_lab_mcp import ssh_isolation as iso
from hostkey_support import make_host_key, make_real_runtime, real_binaries_available

pytestmark = pytest.mark.skipif(not real_binaries_available(), reason="OpenSSH binaries unavailable")

JUMP_PW, TARGET_PW = "SENTINEL_JUMP_PW", "SENTINEL_TARGET_PW"
FAKE = f"""#!{sys.executable}
import json, os, sys
sc = json.load(open(os.environ["FAKE_SSH_SCENARIO"]))
rec = {{"argv": sys.argv[1:], "reads": []}}
def save():
    json.dump(rec, open(sc["record"], "w"))
for step in sc["steps"]:
    k = step[0]
    if k == "print":
        sys.stdout.write(step[1]); sys.stdout.flush()
    elif k == "read":
        rec["reads"].append([step[1], sys.stdin.readline().rstrip("\\r\\n")])
    elif k == "append":
        open(step[1], "a").write(step[2] + "\\n")
    elif k == "exit":
        save(); sys.exit(step[1])
save()
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    before = iso.snapshot_real_ssh_area()
    rt = make_real_runtime(tmp_path)
    fake = tmp_path / "fake_ssh"
    fake.write_text(FAKE)
    fake.chmod(0o700)
    monkeypatch.setattr(hf, "ssh_prefix", lambda kh=None: [str(fake)])
    key = make_host_key(rt, "T")

    def scenario(steps):
        record = tmp_path / "record.json"
        if record.exists():
            record.unlink()
        (tmp_path / "scenario.json").write_text(json.dumps({"steps": steps, "record": str(record)}))
        monkeypatch.setenv("FAKE_SSH_SCENARIO", str(tmp_path / "scenario.json"))
        return record

    yield rt, key, scenario, tmp_path
    iso.uninstall_for_tests()
    assert iso.snapshot_real_ssh_area() == before


CFG = {"transport": "ssh", "address": "192.0.2.20", "port": 22, "username": "targetu", "password": TARGET_PW,
       "jump_host_config": {"address": "192.0.2.1", "port": 22, "username": "jumpu", "password": JUMP_PW}}


def target_prompt(key):
    return ("The authenticity of host '192.0.2.20 (<no hostip for proxy command>)' can't be established.\r\n"
            f"ED25519 key fingerprint is: {key['fingerprint']}\r\nThis key is not known by any other names.\r\n"
            "Are you sure you want to continue connecting (yes/no/[fingerprint])? ")


def run(rt, key, counters=None, expected=None):
    return hf.run_registration_connection("Router1", CFG, "target", rt.known_hosts,
                                          expected or ("ED25519", key["fingerprint"]), counters=counters, timeout=10)


def test_jump_password_sent_once_to_jump_then_target_key_registered_without_target_credential(env):
    rt, key, scenario, tmp = env
    record = scenario([
        ["print", "jumpu@192.0.2.1's password: "], ["read", "jump"],
        ["print", target_prompt(key)], ["read", "fp"],
        ["append", rt.known_hosts, f"192.0.2.20 {key['type']} {key['blob']}"],
        ["print", "targetu@192.0.2.20: Permission denied (publickey).\r\n"], ["exit", 255],
    ])
    counters = {}
    res = run(rt, key, counters)
    assert res.ok, res.reason
    rec = json.loads(record.read_text())
    assert rec["reads"] == [["jump", JUMP_PW], ["fp", key["fingerprint"]]]
    assert counters["jump_sends"] == 1
    argv = " ".join(rec["argv"])
    assert "SENTINEL" not in argv and "PreferredAuthentications=none" in argv and "NumberOfPasswordPrompts=0" in argv
    assert TARGET_PW not in json.dumps(rec)  # the target credential was never typed
    assert hs.classify("192.0.2.20", 22, rt.known_hosts).state == hs.REGISTERED_UNVERIFIED


def test_reprompt_sends_nothing_more_and_blocks_later_connections(env):
    rt, key, scenario, tmp = env
    record = scenario([
        ["print", "jumpu@192.0.2.1's password: "], ["read", "jump"],
        ["print", "Permission denied, please try again.\r\njumpu@192.0.2.1's password: "], ["read", "second"],
        ["exit", 255],
    ])
    counters = {}
    res = run(rt, key, counters)
    assert not res.ok and "jump host authentication failed" in res.reason
    # the fake only saves its record when it exits normally; it was killed at the re-prompt, so nothing more was read
    assert not record.exists()
    assert counters["jump_failed"] == {("192.0.2.1", 22)}
    # a later registration through the same jump host is refused before ssh even starts
    record2 = scenario([["print", "x"], ["exit", 0]])
    res2 = run(rt, key, counters)
    assert not res2.ok and "already failed" in res2.reason
    assert not record2.exists()


def test_target_attributed_password_prompt_is_never_answered(env):
    rt, key, scenario, tmp = env
    record = scenario([["print", "targetu@192.0.2.20's password: "], ["read", "x"], ["exit", 255]])
    counters = {}
    res = run(rt, key, counters)
    assert not res.ok and "nothing was sent" in res.reason
    assert counters.get("jump_sends", 0) == 0
    assert not record.exists() or json.loads(record.read_text())["reads"] == []


@pytest.mark.parametrize("prompt", [
    "eve@192.0.2.1's password: ",             # wrong user for the jump hop
    "jumpu@198.51.100.9's password: ",        # wrong host
    "Password: ",                              # unattributable
    "(jumpu@192.0.2.20) Password: ",           # jump user at the target's address
])
def test_unattributable_or_wrong_prompts_send_nothing(env, prompt):
    rt, key, scenario, tmp = env
    record = scenario([["print", prompt], ["read", "x"], ["exit", 255]])
    counters = {}
    res = run(rt, key, counters)
    assert not res.ok
    assert counters.get("jump_sends", 0) == 0
    assert not record.exists() or json.loads(record.read_text())["reads"] == []


def test_kbdint_style_jump_prompt_is_attributed(env):
    rt, key, scenario, tmp = env
    record = scenario([
        ["print", "(jumpu@192.0.2.1) Password: "], ["read", "jump"],
        ["print", target_prompt(key)], ["read", "fp"],
        ["append", rt.known_hosts, f"192.0.2.20 {key['type']} {key['blob']}"], ["exit", 255],
    ])
    assert run(rt, key, {}).ok
    assert json.loads(record.read_text())["reads"][0] == ["jump", JUMP_PW]


def test_wrong_presented_fingerprint_is_not_answered_and_registers_nothing(env):
    rt, key, scenario, tmp = env
    other = make_host_key(rt, "Other")
    record = scenario([["print", target_prompt(other)], ["read", "fp"], ["exit", 255]])
    res = run(rt, key, {})
    assert not res.ok and "does not match" in res.reason
    assert not record.exists() or json.loads(record.read_text())["reads"] == []
    assert not Path(rt.known_hosts).exists()


def test_prompt_for_other_host_is_not_answered(env):
    rt, key, scenario, tmp = env
    txt = target_prompt(key).replace("192.0.2.20", "198.51.100.77")
    record = scenario([["print", txt], ["read", "fp"], ["exit", 255]])
    res = run(rt, key, {})
    assert not res.ok and "unexpected host" in res.reason
    assert not record.exists() or json.loads(record.read_text())["reads"] == []


def test_fingerprint_prompt_repeated_is_answered_only_once(env):
    rt, key, scenario, tmp = env
    record = scenario([["print", target_prompt(key)], ["read", "fp1"], ["print", target_prompt(key)], ["read", "fp2"],
                       ["exit", 255]])
    res = run(rt, key, {})
    assert not res.ok
    reads = json.loads(record.read_text())["reads"] if record.exists() else []
    assert len(reads) <= 1


def test_budget_exhausted_sends_nothing(env):
    rt, key, scenario, tmp = env
    record = scenario([["print", "jumpu@192.0.2.1's password: "], ["read", "jump"], ["exit", 255]])
    counters = {"jump_sends": 3, "limit": 3}
    res = run(rt, key, counters)
    assert not res.ok and "budget" in res.reason
    assert counters["jump_sends"] == 3
    assert not record.exists() or json.loads(record.read_text())["reads"] == []


def test_deterministic_budget_formula_in_prepare(env, tmp_path):
    rt, key, scenario, tmp = env

    class Quiet:
        def flush_input(self): pass
        def write(self, t): pass
        def readline(self, timeout): return None

    co = hf.Coordinator(Quiet(), known_hosts=rt.known_hosts, global_known_hosts=rt.global_known_hosts,
                        backup_dir=str(tmp_path / "bk"), lock_dir=str(tmp_path / "lk"))
    # every hop already registered -> prepare asks nothing, but still computes the budget
    with open(rt.known_hosts, "w") as fh:
        for h in ("192.0.2.1", "192.0.2.20", "192.0.2.21", "192.0.2.22"):
            fh.write(f"{h} {key['type']} {key['blob']}\n")
    os.chmod(rt.known_hosts, 0o600)
    targets = {f"R{i}": dict(CFG, address=f"192.0.2.2{i}") for i in range(3)}
    targets["R0"] = dict(CFG, address="192.0.2.20")
    co.prepare({k: v for k, v in targets.items()})
    assert co.counters["limit"] == 2 * 3 + 6
    big = {f"R{i}": dict(CFG, address=f"192.0.2.{20 + i}") for i in range(30)}
    co2 = hf.Coordinator(Quiet(), known_hosts=rt.known_hosts, global_known_hosts=rt.global_known_hosts,
                         backup_dir=str(tmp_path / "bk"), lock_dir=str(tmp_path / "lk"))
    try:
        co2.prepare(big)
    except hf.HostKeyError:
        pass  # unknown hosts cancel; only the budget matters here
    assert co2.counters["limit"] == hf.MAX_JUMP_PASSWORD_SENDS


def test_credentials_never_reach_any_argv(env):
    rt, key, scenario, tmp = env
    for kwargs in ({}, {"no_auth_hop": "target"}, {"no_auth_hop": "jump"}):
        assert "SENTINEL" not in " ".join(hf.build_ssh_command(CFG, kh=rt.known_hosts, **kwargs))
