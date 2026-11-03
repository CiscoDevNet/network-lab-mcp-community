# SPDX-License-Identifier: Apache-2.0
"""Forced-wrapper isolation, verified with Fake executables only.
No real ssh / ssh-keygen / sshd is started by this module."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from network_lab_mcp import ssh_isolation as iso

FAKE = """#!/bin/sh
{
  echo "ARGV: $*"
  env | sort | sed 's/^/ENV: /'
} > "$FAKE_OUT"
"""


@pytest.fixture
def box(tmp_path):
    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir()
    exes = {}
    for name in ("ssh", "ssh-keygen", "sshd", "tmux"):
        p = fake_dir / f"fake-{name}"
        p.write_text(FAKE.replace("$FAKE_OUT", str(tmp_path / f"out-{name}.txt")))
        p.chmod(0o700)
        exes[name.replace("-", "_")] = str(p)
    rt = iso.create_sandbox(str(tmp_path), **exes)
    Path(rt.ssh_config).write_text(iso.default_ssh_config(rt))
    Path(rt.identity).write_text("not a key")
    os.chmod(rt.identity, 0o600)
    return rt, tmp_path


def run_wrapper(rt, kind, args, env=None):
    base = dict(os.environ)
    base.update(env or {})
    return subprocess.run(
        [rt.wrapper(kind), *args], capture_output=True, text=True, env=base
    )


def good_ssh(rt, *extra):
    return [*iso.required_ssh_args(rt), *extra, "-p", "22", "user@192.0.2.1"]


def test_good_ssh_runs_fake(box):
    rt, tmp = box
    r = run_wrapper(rt, "ssh", good_ssh(rt))
    assert r.returncode == 0, r.stderr
    out = (tmp / "out-ssh.txt").read_text()
    assert "ARGV: -F" in out


@pytest.mark.parametrize("drop", ["-F", "UserKnownHostsFile", "GlobalKnownHostsFile", "-i", "IdentityAgent", "IdentitiesOnly"])
def test_missing_required_option_rejected_without_exec(box, drop):
    rt, tmp = box
    args = iso.required_ssh_args(rt)
    out = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-F", "-i") and a == drop:
            i += 2
            continue
        if a == "-o" and args[i + 1].startswith(drop):
            i += 2
            continue
        out.append(a)
        i += 1
    r = run_wrapper(rt, "ssh", [*out, "user@192.0.2.1"])
    assert r.returncode == iso.EXIT_VIOLATION
    assert not (tmp / "out-ssh.txt").exists()


def _args_with(rt, **repl):
    args = iso.required_ssh_args(rt)
    for k, v in repl.items():
        if k == "F":
            args[1] = v
        elif k == "UKH":
            args[3] = f"UserKnownHostsFile={v}"
        elif k == "I":
            args[-1] = v
    return [*args, "user@192.0.2.1"]


def test_real_user_ssh_dir_rejected(box):
    rt, tmp = box
    real = os.path.join(iso.real_user_ssh_dir(), "known_hosts")
    for args in (_args_with(rt, UKH=real), _args_with(rt, I=os.path.join(iso.real_user_ssh_dir(), "id_ed25519")),
                 _args_with(rt, F=os.path.join(iso.real_user_ssh_dir(), "config"))):
        r = run_wrapper(rt, "ssh", args)
        assert r.returncode == iso.EXIT_VIOLATION
    assert not (tmp / "out-ssh.txt").exists()


def test_relative_and_tilde_paths_rejected(box):
    rt, tmp = box
    for bad in ("known_hosts", "~/.ssh/known_hosts", "$HOME/.ssh/known_hosts"):
        r = run_wrapper(rt, "ssh", _args_with(rt, UKH=bad))
        assert r.returncode == iso.EXIT_VIOLATION
    assert not (tmp / "out-ssh.txt").exists()


def test_symlink_escape_rejected(box):
    rt, tmp = box
    outside = tmp / "outside"
    outside.mkdir()
    link = Path(rt.sandbox) / "evil"
    link.symlink_to(outside)
    r = run_wrapper(rt, "ssh", _args_with(rt, UKH=str(link / "known_hosts")))
    assert r.returncode == iso.EXIT_VIOLATION
    assert not (tmp / "out-ssh.txt").exists()


@pytest.mark.parametrize("line", [
    "Include /etc/ssh/ssh_config",
    "Match exec true",
    "ProxyCommand nc %h %p",
    "LocalCommand echo hi",
    "KnownHostsCommand /bin/true",
    "RemoteCommand ls",
    "LocalForward 1 127.0.0.1:2",
    "RemoteForward 1 127.0.0.1:2",
    "DynamicForward 1080",
    "CertificateFile /tmp/x",
    "PermitLocalCommand yes",
    "IdentityFile ~/.ssh/id_rsa",
    "UserKnownHostsFile ~/.ssh/known_hosts",
])
def test_unsafe_ssh_config_rejected(box, line):
    rt, tmp = box
    Path(rt.ssh_config).write_text(iso.default_ssh_config(rt, extra=line + "\n"))
    r = run_wrapper(rt, "ssh", good_ssh(rt))
    assert r.returncode == iso.EXIT_VIOLATION
    assert not (tmp / "out-ssh.txt").exists()


def test_config_missing_mandatory_directive_rejected(box):
    rt, tmp = box
    Path(rt.ssh_config).write_text("Host *\n  StrictHostKeyChecking yes\n")
    r = run_wrapper(rt, "ssh", good_ssh(rt))
    assert r.returncode == iso.EXIT_VIOLATION


@pytest.mark.parametrize("opt", [
    "ProxyCommand=nc x 1", "Include=/etc/ssh/ssh_config", "LocalCommand=id", "KnownHostsCommand=/bin/true",
    "IdentityFile=/tmp/k", "CertificateFile=/tmp/c", "ControlPath=/tmp/c", "LocalForward=1 h:2",
    # security-relevant options are accepted only with their safe value
    "ControlMaster=auto", "ControlPath=%d/x", "ControlPersist=10m", "PermitLocalCommand=yes", "SessionType=default",
    "StrictHostKeyChecking=no", "StrictHostKeyChecking=off", "StrictHostKeyChecking=ask", "HashKnownHosts=yes",
    "CheckHostIP=yes", "VerifyHostKeyDNS=yes", "VerifyHostKeyDNS=ask", "UpdateHostKeys=yes",
    "PasswordAuthentication=yes", "PubkeyAuthentication=yes", "KbdInteractiveAuthentication=yes",
    "PreferredAuthentications=password", "NumberOfPasswordPrompts=3", "HostbasedAuthentication=yes",
    "GSSAPIAuthentication=yes",
])
def test_unsafe_dash_o_rejected(box, opt):
    rt, tmp = box
    r = run_wrapper(rt, "ssh", good_ssh(rt, "-o", opt))
    assert r.returncode == iso.EXIT_VIOLATION
    assert not (tmp / "out-ssh.txt").exists()


def test_probe_and_strict_option_sets_pass_the_wrapper_and_unsafe_known_hosts_values_do_not(box):
    from network_lab_mcp import hostkey_tofu as tofu

    rt, tmp = box
    probe = tofu.probe_opts(str(Path(rt.tmp_dir) / "probe_kh"))
    r = run_wrapper(rt, "ssh", [*iso.required_ssh_args(rt, str(Path(rt.tmp_dir) / "probe_kh")), "-N", "-T", *probe,
                                "-p", "22", "nlm-hostkey-probe@192.0.2.1"])
    assert r.returncode == 0, r.stderr
    r = run_wrapper(rt, "ssh", [*iso.required_ssh_args(rt), *tofu.strict_opts([rt.product_known_hosts, rt.known_hosts]),
                                "-p", "22", "user@192.0.2.1"])
    assert r.returncode == 0, r.stderr
    for bad in ('UserKnownHostsFile="/etc/hosts"', f'UserKnownHostsFile="{rt.product_known_hosts}" "/etc/hosts"',
                f'UserKnownHostsFile="{rt.product_known_hosts}" "{Path.home()}/.ssh/known_hosts"',
                'UserKnownHostsFile="unterminated'):
        r = run_wrapper(rt, "ssh", good_ssh(rt, "-o", bad))
        assert r.returncode == iso.EXIT_VIOLATION, bad


def test_probe_proxycommand_child_is_validated_with_the_same_policy(box):
    from network_lab_mcp import hostkey_tofu as tofu

    rt, tmp = box
    cfg = {"transport": "ssh", "address": "192.0.2.20", "port": 22, "username": "u",
           "jump_host_config": {"address": "192.0.2.1", "port": 22, "username": "j"}}
    iso.install_for_tests(rt)
    try:
        argv = tofu.build_probe_command(cfg, "target", str(Path(rt.tmp_dir) / "probe_kh"))
    finally:
        iso.uninstall_for_tests()
    assert argv[0] == rt.wrapper("ssh")
    assert run_wrapper(rt, "ssh", argv[1:]).returncode == 0
    evil = [a.replace("StrictHostKeyChecking=yes", "StrictHostKeyChecking=no") for a in argv]
    assert evil != argv and run_wrapper(rt, "ssh", evil[1:]).returncode == iso.EXIT_VIOLATION


def test_runtime_known_hosts_files_must_be_inside_the_sandbox(box, tmp_path):
    rt, tmp = box
    import dataclasses

    outside = dataclasses.replace(rt, product_known_hosts=str(tmp_path / "outside_known_hosts"))
    with pytest.raises(iso.IsolationViolation):
        iso.validate_runtime(outside)


@pytest.mark.parametrize("flag", [["-L", "1:h:2"], ["-R", "1:h:2"], ["-D", "1080"], ["-S", "/tmp/s"], ["-E", "/tmp/log"]])
def test_forward_and_socket_flags_rejected(box, flag):
    rt, tmp = box
    assert run_wrapper(rt, "ssh", good_ssh(rt, *flag)).returncode == iso.EXIT_VIOLATION


def test_second_identity_rejected(box):
    rt, tmp = box
    r = run_wrapper(rt, "ssh", good_ssh(rt, "-i", os.path.join(iso.real_user_ssh_dir(), "id_rsa")))
    assert r.returncode == iso.EXIT_VIOLATION


def test_environment_is_rebuilt_from_allowlist(box):
    rt, tmp = box
    poison = {
        "SSH_AUTH_SOCK": "/tmp/agent", "SSH_ASKPASS": "/tmp/askpass", "SSH_ASKPASS_REQUIRE": "force",
        "DISPLAY": ":0", "TMUX": "/tmp/tmux,1,0", "TMUX_PANE": "%1", "DYLD_INSERT_LIBRARIES": "/tmp/x.dylib",
        "LD_PRELOAD": "/tmp/x.so", "SECRET_TOKEN": "sentinel", "SSH_CONNECTION": "x",
    }
    r = run_wrapper(rt, "ssh", good_ssh(rt), env=poison)
    assert r.returncode == 0, r.stderr
    envlines = [l[5:] for l in (tmp / "out-ssh.txt").read_text().splitlines() if l.startswith("ENV: ")]
    names = {l.split("=", 1)[0] for l in envlines}
    for k in poison:
        assert k not in names
    assert {"PATH", "HOME", "TMPDIR", "LC_ALL"} <= names
    assert f"HOME={rt.home_dir}" in envlines
    assert f"PATH={rt.bin_dir}:/usr/bin:/bin" in envlines
    assert names <= {"PATH", "HOME", "TMPDIR", "LC_ALL", "LANG", "PWD", "SHLVL", "_", "OLDPWD"}


def test_keygen_requires_sandbox_f_and_allowlist(box):
    rt, tmp = box
    ok = run_wrapper(rt, "ssh-keygen", ["-F", "192.0.2.1", "-f", rt.known_hosts])
    assert ok.returncode == 0, ok.stderr
    (tmp / "out-ssh-keygen.txt").unlink()
    bad = [
        ["-F", "192.0.2.1"],  # no -f -> default ~/.ssh/known_hosts
        ["-R", "192.0.2.1"],
        ["-F", "h", "-f", os.path.join(iso.real_user_ssh_dir(), "known_hosts")],
        ["-F", "h", "-f", "known_hosts"],
        ["-A"],
        ["-s", "x", "-f", rt.known_hosts],
    ]
    for args in bad:
        assert run_wrapper(rt, "ssh-keygen", args).returncode == iso.EXIT_VIOLATION, args
    assert not (tmp / "out-ssh-keygen.txt").exists()


def test_sshd_requires_loopback_pubkey_only_sandbox_config(box):
    rt, tmp = box
    cfg = Path(rt.sandbox) / "etc" / "sshd_config"
    good = (f"Port 22222\nListenAddress 127.0.0.1\nHostKey {rt.sandbox}/hk\n"
            f"AuthorizedKeysFile {rt.sandbox}/ak\nPasswordAuthentication no\nUsePAM no\n")
    cfg.write_text(good)
    assert run_wrapper(rt, "sshd", ["-D", "-e", "-f", str(cfg)]).returncode == 0
    (tmp / "out-sshd.txt").unlink()
    for bad in ("ListenAddress 0.0.0.0\n", "PasswordAuthentication yes\n", "AuthorizedKeysCommand /bin/x\n",
                "HostKey /etc/ssh/ssh_host_rsa_key\n"):
        cfg.write_text(good + bad)
        assert run_wrapper(rt, "sshd", ["-D", "-f", str(cfg)]).returncode == iso.EXIT_VIOLATION
    assert run_wrapper(rt, "sshd", ["-D"]).returncode == iso.EXIT_VIOLATION
    assert not (tmp / "out-sshd.txt").exists()


def test_sandbox_must_be_0700_and_executables_absolute(box):
    rt, tmp = box
    os.chmod(rt.sandbox, 0o755)
    try:
        assert run_wrapper(rt, "ssh", good_ssh(rt)).returncode == iso.EXIT_VIOLATION
    finally:
        os.chmod(rt.sandbox, 0o700)
    assert not (tmp / "out-ssh.txt").exists()


def test_policy_file_permissions_enforced(box):
    rt, tmp = box
    os.chmod(rt.policy_file, 0o644)
    try:
        assert run_wrapper(rt, "ssh", good_ssh(rt)).returncode == iso.EXIT_VIOLATION
    finally:
        os.chmod(rt.policy_file, 0o600)


def test_proxyjump_child_form_is_validated_by_config(box):
    rt, tmp = box
    r = run_wrapper(rt, "ssh", ["-F", rt.ssh_config, "-W", "192.0.2.2:22", "jump@192.0.2.1"])
    assert r.returncode == 0, r.stderr
    Path(rt.ssh_config).write_text("Host *\n  ProxyCommand x\n")
    assert run_wrapper(rt, "ssh", ["-F", rt.ssh_config, "-W", "h:22", "j"]).returncode == iso.EXIT_VIOLATION


def test_proxyjump_value_must_be_plain(box):
    rt, tmp = box
    Path(rt.ssh_config).write_text(iso.default_ssh_config(rt, extra="ProxyJump a b;c\n"))
    assert run_wrapper(rt, "ssh", good_ssh(rt)).returncode == iso.EXIT_VIOLATION


def test_no_environment_variable_selects_policy():
    src = Path(iso.__file__).read_text()
    assert "os.environ" not in re.sub(r"#.*", "", src.replace('"""', "")) .replace("os.environ is", "")


def test_user_ssh_area_untouched_by_stage0(box):
    rt, tmp = box
    before = iso.snapshot_real_ssh_area()
    run_wrapper(rt, "ssh", good_ssh(rt))
    assert iso.snapshot_real_ssh_area() == before


# ---- injection point and guards -------------------------------------------

from network_lab_mcp import terminal  # noqa: E402


@pytest.fixture
def installed(box):
    rt, tmp = box
    iso.install_for_tests(rt)
    yield rt, tmp
    iso.uninstall_for_tests()


def test_no_runtime_means_unchanged_production_command():
    assert iso.active_runtime() is None
    _, cmd = terminal._build_transport_command(
        {"transport": "ssh", "address": "192.0.2.10", "username": "u", "port": 22}
    )
    assert cmd[0] == "ssh" and "-F" not in cmd


def test_installed_runtime_routes_product_ssh_through_wrapper(installed):
    rt, tmp = installed
    _, cmd = terminal._build_transport_command(
        {"transport": "ssh", "address": "192.0.2.10", "username": "u", "port": 22,
         "jump_host_config": {"address": "192.0.2.1", "username": "j", "port": 22, "password": "x"}}
    )
    assert cmd[0] == rt.wrapper("ssh")
    iso.validate_argv("ssh", cmd[1:], rt)  # product argv (incl. the ProxyCommand child) passes the wrapper policy
    proxy = next(a for a in cmd if a.startswith("ProxyCommand="))
    assert proxy.split("=", 1)[1].startswith(rt.wrapper("ssh"))  # the jump child is wrapped too
    assert "-J" not in cmd and "StrictHostKeyChecking=yes" in cmd


def test_tunnel_refused_under_runtime(installed):
    with pytest.raises(terminal.TerminalError):
        terminal._build_tunnel_command(
            {"jump_host_config": {"address": "192.0.2.1", "username": "j"}, "address": "192.0.2.5", "port": 23}, 40000
        )


def test_tmux_path_comes_from_runtime(installed):
    rt, _ = installed
    assert terminal._tmux_base()[0] == rt.tmux


def test_install_requires_valid_runtime(box):
    rt, _ = box
    os.chmod(rt.sandbox, 0o755)
    try:
        with pytest.raises(iso.IsolationViolation):
            iso.install_for_tests(rt)
    finally:
        os.chmod(rt.sandbox, 0o700)
    assert iso.active_runtime() is None


def test_injection_not_reachable_from_env_yaml_cli_mcp():
    root = Path(iso.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        if path.name in ("ssh_isolation.py",):
            continue
        text = path.read_text()
        if "install_for_tests" in text:
            offenders.append(path.name)
    assert offenders == []
    for name in ("mcp_server.py", "cli/main.py", "cli/config.py", "lab.py", "discovery.py"):
        assert "ssh_isolation" not in (root / name).read_text() or name == "discovery.py"
    # the module itself never reads the environment
    src = Path(iso.__file__).read_text()
    assert "os.environ" not in src and "getenv" not in src


def test_python_open_guard_blocks_real_ssh_area_and_allows_tmp(tmp_path):
    target = os.path.join(iso.real_user_ssh_dir(), "known_hosts")
    before = iso.snapshot_real_ssh_area()
    with iso.forbid_real_ssh_area():
        with pytest.raises(PermissionError):
            open(target)
        with pytest.raises(PermissionError):
            os.open(target, os.O_RDONLY)
        (tmp_path / "ok.txt").write_text("ok")
    assert iso.snapshot_real_ssh_area() == before
    assert (tmp_path / "ok.txt").read_text() == "ok"


def test_audit_log_records_decisions_without_argv_content(box):
    rt, tmp = box
    run_wrapper(rt, "ssh", good_ssh(rt))
    run_wrapper(rt, "ssh", ["-L", "1:h:2", *good_ssh(rt)])
    lines = [json.loads(l) for l in Path(rt.sandbox, "audit.log").read_text().splitlines()]
    assert [l["decision"] for l in lines] == ["allowed", "refused"]
    assert "not allowed" in lines[1]["reason"]
    assert "user@192.0.2.1" not in Path(rt.sandbox, "audit.log").read_text()
    assert oct(os.stat(Path(rt.sandbox, "audit.log")).st_mode & 0o777) == "0o600"
