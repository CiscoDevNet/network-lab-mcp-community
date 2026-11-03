# SPDX-License-Identifier: Apache-2.0
"""Jump-server password handling in the authentication-free probe, verified
with a FAKE ssh that scripts OpenSSH's prompts on a real pty. Sentinels are
fictional. ssh-keygen (real) runs only through the isolation wrapper."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from network_lab_mcp import hostkey_flow as hf
from network_lab_mcp import hostkey_tofu as tofu
from network_lab_mcp import hostkey_store as hs
from network_lab_mcp import ssh_isolation as iso
from hostkey_support import make_host_key, make_real_runtime, real_binaries_available

pytestmark = pytest.mark.skipif(not real_binaries_available(), reason="OpenSSH binaries unavailable")

JUMP_PW, TARGET_PW = "SENTINEL_JUMP_PW", "SENTINEL_TARGET_PW"
FAKE = f"""#!{sys.executable}
import json, os, re, sys
sc = json.load(open(os.environ["FAKE_SSH_SCENARIO"]))
rec = {{"argv": sys.argv[1:], "reads": []}}
def save():
    json.dump(rec, open(sc["record"], "w"))
def probe_file():
    for a in sys.argv:
        m = re.match(r'UserKnownHostsFile="([^"]+)"$', a)
        if m:
            return m.group(1)
for step in sc["steps"]:
    k = step[0]
    if k == "print":
        sys.stdout.write(step[1]); sys.stdout.flush()
    elif k == "read":
        rec["reads"].append([step[1], sys.stdin.readline().rstrip("\\r\\n")])
    elif k == "record_kh":  # what OpenSSH's accept-new would write into the temporary known_hosts
        open(probe_file(), "a").write(step[1] + "\\n")
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
    monkeypatch.setattr(tofu, "ssh_prefix", lambda kh=None: [str(fake)] + (["-o", f'UserKnownHostsFile="{kh}"'] if kh else []))
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


def kh_line(key, host="192.0.2.20"):
    return f"{host} {key['type']} {key['blob']}"


def run(rt, key, counters=None, role="target"):
    return tofu.probe_host_key("Router1", CFG, role, counters=counters, timeout=10)


def no_reads(record):
    return not record.exists() or json.loads(record.read_text())["reads"] == []


def test_jump_password_sent_once_to_jump_then_target_key_recorded_without_target_credential(env):
    rt, key, scenario, tmp = env
    record = scenario([
        ["print", "jumpu@192.0.2.1's password: "], ["read", "jump"],
        ["record_kh", kh_line(key)],
        ["print", "nlm-hostkey-probe@192.0.2.20: Permission denied (publickey).\r\n"], ["exit", 255],
    ])
    counters = {}
    got = run(rt, key, counters)
    assert (got.key_type, got.fingerprint) == (key["type"], key["fingerprint"])
    rec = json.loads(record.read_text())
    assert rec["reads"] == [["jump", JUMP_PW]]
    assert counters["jump_sends"] == 1
    argv = " ".join(rec["argv"])
    assert "SENTINEL" not in argv and "PreferredAuthentications=none" in argv and "NumberOfPasswordPrompts=0" in argv
    assert "nlm-hostkey-probe@192.0.2.20" in argv and "targetu" not in argv.split("ProxyCommand=")[0]
    assert TARGET_PW not in json.dumps(rec)  # the target credential was never typed
    # nothing was registered by the probe itself: persistence is a separate, later step
    assert not Path(rt.product_known_hosts).exists()


def test_reprompt_sends_nothing_more_and_blocks_later_probes(env):
    rt, key, scenario, tmp = env
    record = scenario([
        ["print", "jumpu@192.0.2.1's password: "], ["read", "jump"],
        ["print", "Permission denied, please try again.\r\njumpu@192.0.2.1's password: "], ["read", "second"],
        ["exit", 255],
    ])
    counters = {}
    with pytest.raises(hf.HostKeyError, match="JUMP_AUTH_ERROR.*jump host authentication failed"):
        run(rt, key, counters)
    assert not record.exists()  # killed at the re-prompt: nothing more was read
    assert counters["jump_failed"] == {("192.0.2.1", 22)}
    record2 = scenario([["print", "x"], ["exit", 0]])
    with pytest.raises(hf.HostKeyError, match="already failed"):
        run(rt, key, counters)
    assert not record2.exists()  # refused before ssh even started


def test_target_attributed_password_prompt_is_never_answered(env):
    rt, key, scenario, tmp = env
    record = scenario([["print", "targetu@192.0.2.20's password: "], ["read", "x"], ["exit", 255]])
    counters = {}
    with pytest.raises(hf.HostKeyError, match="nothing was sent"):
        run(rt, key, counters)
    assert counters.get("jump_sends", 0) == 0 and no_reads(record)


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
    with pytest.raises(hf.HostKeyError):
        run(rt, key, counters)
    assert counters.get("jump_sends", 0) == 0 and no_reads(record)


def test_kbdint_style_jump_prompt_is_attributed(env):
    rt, key, scenario, tmp = env
    record = scenario([["print", "(jumpu@192.0.2.1) Password: "], ["read", "jump"], ["record_kh", kh_line(key)], ["exit", 255]])
    assert run(rt, key, {}).fingerprint == key["fingerprint"]
    assert json.loads(record.read_text())["reads"][0] == ["jump", JUMP_PW]


def test_jump_hop_probe_never_gets_a_password_even_if_asked(env):
    rt, key, scenario, tmp = env
    record = scenario([["print", "jumpu@192.0.2.1's password: "], ["read", "x"], ["exit", 255]])
    counters = {}
    with pytest.raises(hf.HostKeyError, match="nothing was sent"):
        tofu.probe_host_key("Router1", CFG, "jump", counters=counters, timeout=10)
    assert counters.get("jump_sends", 0) == 0 and no_reads(record)


def test_unexpected_confirmation_prompt_is_not_answered(env):
    rt, key, scenario, tmp = env
    record = scenario([["print", "Are you sure you want to continue connecting (yes/no/[fingerprint])? "], ["read", "x"], ["exit", 255]])
    with pytest.raises(hf.HostKeyError, match="unexpected confirmation prompt"):
        run(rt, key, {})
    assert no_reads(record)


def test_unexpected_authentication_success_is_stopped_and_nothing_registered(env):
    rt, key, scenario, tmp = env
    scenario([["record_kh", kh_line(key)],
              ["print", 'Authenticated to 192.0.2.20 ([192.0.2.20]:22) using "none".\r\n'], ["read", "never"], ["exit", 0]])
    with pytest.raises(hf.HostKeyError, match="authentication unexpectedly succeeded"):
        run(rt, key, {})
    assert not Path(rt.product_known_hosts).exists()


@pytest.mark.parametrize("recorded, match", [
    ("192.0.2.99 {t} {b}", "different host identifier"),
    ("192.0.2.20 ssh-dss AAAA", "not supported"),
    ("192.0.2.20 {t} AAAA", "not supported"),
    ("|1|aaaa|bbbb {t} {b}", "different host identifier"),
])
def test_unexpected_probe_record_is_refused(env, recorded, match):
    rt, key, scenario, tmp = env
    scenario([["record_kh", recorded.format(t=key["type"], b=key["blob"])], ["exit", 255]])
    with pytest.raises(hf.HostKeyError, match=match):
        run(rt, key, {})


def test_two_recorded_keys_are_refused_single_key_only(env):
    rt, key, scenario, tmp = env
    other = make_host_key(rt, "Other")
    scenario([["record_kh", kh_line(key)], ["record_kh", kh_line(other)], ["exit", 255]])
    with pytest.raises(hf.HostKeyError, match="unexpected number"):
        run(rt, key, {})


def test_budget_exhausted_sends_nothing(env):
    rt, key, scenario, tmp = env
    record = scenario([["print", "jumpu@192.0.2.1's password: "], ["read", "jump"], ["exit", 255]])
    counters = {"jump_sends": 3, "limit": 3}
    with pytest.raises(hf.HostKeyError, match="budget"):
        run(rt, key, counters)
    assert counters["jump_sends"] == 3 and no_reads(record)


def test_probe_temp_dir_is_always_removed(env):
    rt, key, scenario, tmp = env
    scenario([["record_kh", kh_line(key)], ["exit", 255]])
    run(rt, key, {})
    scenario([["print", "x"], ["exit", 255]])
    with pytest.raises(hf.HostKeyError):
        run(rt, key, {})
    assert [p for p in Path(rt.tmp_dir).iterdir() if p.name.startswith("nlm-probe-")] == []


def test_deterministic_budget_formula_in_prepare(env, tmp_path):
    rt, key, scenario, tmp = env
    co = hf.Coordinator(None, known_hosts=rt.product_known_hosts, user_known_hosts=rt.known_hosts,
                        global_known_hosts=rt.global_known_hosts, backup_dir=str(tmp_path / "bk"), lock_dir=str(tmp_path / "lk"))
    # every hop already registered -> prepare probes nothing, but still computes the budget
    with open(rt.known_hosts, "w") as fh:
        for h in ("192.0.2.1", "192.0.2.20", "192.0.2.21", "192.0.2.22"):
            fh.write(f"{h} {key['type']} {key['blob']}\n")
    os.chmod(rt.known_hosts, 0o600)
    targets = {f"R{i}": dict(CFG, address=f"192.0.2.2{i}") for i in range(3)}
    co.prepare(targets)
    assert co.counters["limit"] == 2 * 3 + 6
    big = {f"R{i}": dict(CFG, address=f"192.0.2.{20 + i}") for i in range(30)}
    co2 = hf.Coordinator(None, known_hosts=rt.product_known_hosts, user_known_hosts=rt.known_hosts,
                         global_known_hosts=rt.global_known_hosts, backup_dir=str(tmp_path / "bk"), lock_dir=str(tmp_path / "lk"))
    scenario([["print", "x"], ["exit", 255]])
    with pytest.raises(hf.HostKeyError):
        co2.prepare(big)  # unknown hosts fail the (fake) probe; only the budget matters here
    assert co2.counters["limit"] == hf.MAX_JUMP_PASSWORD_SENDS


def test_credentials_never_reach_any_argv(env):
    rt, key, scenario, tmp = env
    assert "SENTINEL" not in " ".join(hf.build_ssh_command(CFG, files=[rt.product_known_hosts, rt.known_hosts]))
    for role in ("target", "jump"):
        assert "SENTINEL" not in " ".join(tofu.build_probe_command(CFG, role, "/tmp/x"))
