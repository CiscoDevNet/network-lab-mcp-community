# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Test-only isolation for every OpenSSH-family process (ssh, ssh-keygen, sshd).

Purpose
-------
Development tests of the SSH host-key feature must never touch the real
user's ``~/.ssh`` (known_hosts, private keys, ssh_config, agent). This module
provides:

* ``SshRuntime``: a typed dependency object that names the absolute paths of
  the binaries and of every file OpenSSH may read or write. It is injected
  through ``terminal._install_ssh_runtime`` (a private, import-only hook). It
  is never read from environment variables, YAML, the CLI, or MCP.
* A *forced wrapper*: ``python -I -m network_lab_mcp.ssh_isolation <policy>
  <kind> args...``. It validates the argument vector and the generated ssh
  config against an allow-list, rebuilds the environment from scratch, and
  only then ``execve``s the pinned absolute binary. A violation exits with
  status 97 *before* any OpenSSH binary is started.
* Generators for the sandbox (0700 directory, ``bin/ssh`` wrapper scripts so
  that ProxyJump child processes are wrapped too, policy file, ssh config).

This module deliberately reads **no** environment variable for policy.
Production code never imports it for decisions; it only runs when a test
installs a runtime.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shlex
import stat
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

EXIT_VIOLATION = 97


class IsolationViolation(Exception):
    """Raised when an argument vector, path, config or runtime is unsafe."""


# --------------------------------------------------------------------------
# Real user's SSH area (never read; only stat'ed for non-interference checks)
# --------------------------------------------------------------------------


def real_user_ssh_dir() -> str:
    """The real user's ``~/.ssh`` from the password database (not $HOME)."""
    return os.path.realpath(os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".ssh"))


def _stat_record(path: str) -> tuple | None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def snapshot_real_ssh_area() -> dict:
    """Non-secret metadata only (existence, size, mtime, inode)."""
    base = real_user_ssh_dir()
    return {"dir": _stat_record(base), "known_hosts": _stat_record(os.path.join(base, "known_hosts"))}


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


# --------------------------------------------------------------------------
# Runtime object
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SshRuntime:
    sandbox: str
    ssh: str
    ssh_keygen: str
    sshd: str
    tmux: str
    known_hosts: str
    global_known_hosts: str
    ssh_config: str
    identity: str
    # Product-owned known_hosts (the only file TOFU registration writes);
    # `known_hosts` above stands in for the user's own file and is read-only
    # for the product.
    product_known_hosts: str = ""

    @property
    def bin_dir(self) -> str:
        return os.path.join(self.sandbox, "bin")

    @property
    def home_dir(self) -> str:
        return os.path.join(self.sandbox, "home")

    @property
    def tmp_dir(self) -> str:
        return os.path.join(self.sandbox, "tmp")

    @property
    def policy_file(self) -> str:
        return os.path.join(self.sandbox, "policy.json")

    def wrapper(self, kind: str) -> str:
        return os.path.join(self.bin_dir, kind)


_BAD_PATH_CHARS = re.compile(r"[\x00-\x1f\x7f~%$`\\]")


def check_sandbox_path(path: str, sandbox: str, *, what: str = "path") -> str:
    """Absolute, no expansion characters, realpath inside the sandbox and not
    inside the real user's ~/.ssh. Returns the realpath."""
    if not isinstance(path, str) or not path:
        raise IsolationViolation(f"{what}: empty path")
    if _BAD_PATH_CHARS.search(path):
        raise IsolationViolation(f"{what}: path contains an expansion or control character")
    if not os.path.isabs(path):
        raise IsolationViolation(f"{what}: path must be absolute")
    real = os.path.realpath(path)
    if _under(real, real_user_ssh_dir()):
        raise IsolationViolation(f"{what}: path is inside the real user's SSH area")
    if not _under(real, os.path.realpath(sandbox)):
        raise IsolationViolation(f"{what}: path resolves outside the isolated sandbox")
    return real


def check_executable(path: str, *, what: str) -> None:
    if not isinstance(path, str) or not os.path.isabs(path) or _BAD_PATH_CHARS.search(path):
        raise IsolationViolation(f"{what}: executable must be an absolute path")
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise IsolationViolation(f"{what}: executable is missing or not executable")


def validate_runtime(rt: SshRuntime) -> None:
    sandbox = rt.sandbox
    if not os.path.isabs(sandbox) or _BAD_PATH_CHARS.search(sandbox):
        raise IsolationViolation("sandbox must be an absolute plain path")
    real = os.path.realpath(sandbox)
    if real != sandbox:
        raise IsolationViolation("sandbox must be given as its realpath")
    st = os.stat(sandbox)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise IsolationViolation("sandbox must be a 0700 directory owned by the current user")
    if _under(real, real_user_ssh_dir()):
        raise IsolationViolation("sandbox is inside the real user's SSH area")
    for name in ("ssh", "ssh_keygen", "sshd", "tmux"):
        check_executable(getattr(rt, name), what=name)
    for name in ("known_hosts", "global_known_hosts", "ssh_config", "identity", "product_known_hosts"):
        check_sandbox_path(getattr(rt, name), sandbox, what=name)


# --------------------------------------------------------------------------
# Environment (rebuilt from an allow-list; nothing is inherited)
# --------------------------------------------------------------------------


def build_clean_env(rt: SshRuntime) -> dict[str, str]:
    return {
        "PATH": f"{rt.bin_dir}:/usr/bin:/bin",
        "HOME": rt.home_dir,
        "TMPDIR": rt.tmp_dir,
        "LC_ALL": "C",
        "LANG": "C",
    }


# --------------------------------------------------------------------------
# ssh_config validation (harness-generated structure only)
# --------------------------------------------------------------------------

_SSH_CONFIG_ALLOWED = {
    "host", "hostname", "port", "user", "hostkeyalias", "identityfile", "identitiesonly",
    "identityagent", "userknownhostsfile", "globalknownhostsfile", "stricthostkeychecking",
    "hostkeyalgorithms", "updatehostkeys", "checkhostip", "verifyhostkeydns", "batchmode",
    "preferredauthentications", "pubkeyauthentication", "passwordauthentication",
    "kbdinteractiveauthentication", "gssapiauthentication", "numberofpasswordprompts",
    "proxyjump", "connecttimeout", "loglevel", "serveraliveinterval", "serveralivecountmax",
    "forwardagent", "forwardx11", "permitlocalcommand", "controlmaster", "controlpath",
    "compression", "hashknownhosts", "addkeystoagent", "requesttty", "sessiontype", "controlpersist",
}
_SSH_CONFIG_PATH_KEYS = {"identityfile", "userknownhostsfile", "globalknownhostsfile"}
_SSH_CONFIG_FIXED = {
    "forwardagent": {"no"}, "forwardx11": {"no"}, "permitlocalcommand": {"no"},
    "controlmaster": {"no"}, "identityagent": {"none"}, "identitiesonly": {"yes"},
    "controlpath": {"none"}, "updatehostkeys": {"no", "yes"}, "controlpersist": {"no"},
    "sessiontype": {"none"},
}
# Values accepted for a security-relevant `-o` on the command line. The probe
# and the strict connection state these explicitly; nothing else is accepted.
_CLI_FIXED = {
    "controlmaster": {"no"}, "controlpath": {"none"}, "controlpersist": {"no"}, "permitlocalcommand": {"no"},
    "sessiontype": {"none"}, "stricthostkeychecking": {"yes", "accept-new"}, "hashknownhosts": {"no"},
    "checkhostip": {"no"}, "verifyhostkeydns": {"no"}, "updatehostkeys": {"no"}, "batchmode": {"yes", "no"},
    "preferredauthentications": {"none"}, "pubkeyauthentication": {"no"}, "passwordauthentication": {"no"},
    "kbdinteractiveauthentication": {"no"}, "gssapiauthentication": {"no"}, "numberofpasswordprompts": {"0"},
    "hostbasedauthentication": {"no"},
}


def _known_hosts_values(value: str) -> list[str]:
    """`UserKnownHostsFile` takes one or more whitespace-separated, optionally
    double-quoted paths."""
    try:
        parts = shlex.split(value)
    except ValueError:
        raise IsolationViolation("ssh: UserKnownHostsFile is not parseable") from None
    if not parts:
        raise IsolationViolation("ssh: UserKnownHostsFile is empty")
    return parts
_JUMP_RE = re.compile(r"^[A-Za-z0-9_.@:\[\]-]+(,[A-Za-z0-9_.@:\[\]-]+)*$")


def _split_directive(line: str) -> tuple[str, str]:
    m = re.match(r"^\s*([A-Za-z0-9]+)\s*(?:=\s*|\s+)(.*?)\s*$", line)
    if not m:
        raise IsolationViolation("ssh_config: unparsable line")
    return m.group(1).lower(), m.group(2).strip().strip('"')


def validate_ssh_config_text(text: str, rt: SshRuntime) -> None:
    """Allow-list validation. Rejects Include, Match, ProxyCommand,
    LocalCommand, KnownHostsCommand, RemoteCommand, forwards, and any path
    outside the sandbox. Also requires the mandatory isolation directives."""
    seen: dict[str, set[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, value = _split_directive(line)
        if key not in _SSH_CONFIG_ALLOWED:
            raise IsolationViolation(f"ssh_config: directive '{key}' is not allowed")
        if _BAD_PATH_CHARS.search(value.replace('"', "")) and key in _SSH_CONFIG_PATH_KEYS:
            raise IsolationViolation(f"ssh_config: '{key}' value contains an expansion character")
        if key in _SSH_CONFIG_PATH_KEYS:
            if key == "identityfile" and value == "none":
                pass
            elif key == "userknownhostsfile":
                for path in _known_hosts_values(value):
                    check_sandbox_path(path, rt.sandbox, what=key)
            else:
                check_sandbox_path(value, rt.sandbox, what=key)
        if key == "identityfile" and value not in ("none", rt.identity):
            raise IsolationViolation("ssh_config: IdentityFile must be the injected temporary identity")
        if key == "globalknownhostsfile" and value != rt.global_known_hosts:
            raise IsolationViolation("ssh_config: GlobalKnownHostsFile must be the injected file")
        if key in _SSH_CONFIG_FIXED and value.lower() not in _SSH_CONFIG_FIXED[key]:
            raise IsolationViolation(f"ssh_config: '{key}' has a forbidden value")
        if key == "proxyjump":
            if value.lower() != "none" and (not _JUMP_RE.match(value) or "/" in value):
                raise IsolationViolation("ssh_config: ProxyJump must be a plain host list")
        seen.setdefault(key, set()).add(value)
    required = {
        "identityagent": {"none"}, "identitiesonly": {"yes"},
        "identityfile": {rt.identity}, "userknownhostsfile": None, "globalknownhostsfile": {rt.global_known_hosts},
    }
    for key, expected in required.items():
        if key not in seen:
            raise IsolationViolation(f"ssh_config: missing mandatory '{key}'")
        if expected is not None and not seen[key] <= expected:
            raise IsolationViolation(f"ssh_config: '{key}' has an unexpected value")


# --------------------------------------------------------------------------
# argv validation
# --------------------------------------------------------------------------

_FORBIDDEN_O_KEYS = {
    "include", "match", "localcommand", "knownhostscommand", "remotecommand",
    "localforward", "remoteforward", "dynamicforward", "identityfile", "certificatefile",
    "securitykeyprovider", "pkcs11provider", "xauthlocation", "sendenv", "setenv",
    "tunnel", "forwardagent",
}


def _require_o(opts: list[str], key: str, value: str) -> None:
    want = f"{key}={value}".lower()
    if not any(o.replace(" ", "").lower() == want for o in opts):
        raise IsolationViolation(f"ssh: required option '-o {key}={value}' is missing")


def _validate_proxycommand(value: str, rt: SshRuntime, depth: int) -> None:
    """Only a structured child invocation of the forced ssh wrapper itself:
    `<wrapper ssh> <args> -W host:port`, validated with the same policy."""
    if depth > 1:
        raise IsolationViolation("ssh: nested ProxyCommand is not allowed")
    try:
        tokens = shlex.split(value)
    except ValueError:
        raise IsolationViolation("ssh: ProxyCommand is not parseable") from None
    if not tokens or tokens[0] != rt.wrapper("ssh"):
        raise IsolationViolation("ssh: ProxyCommand must invoke the isolated ssh wrapper")
    if "-W" not in tokens[1:]:
        raise IsolationViolation("ssh: ProxyCommand child must use -W")
    validate_ssh_argv(tokens[1:], rt, depth + 1)


def validate_ssh_argv(args: list[str], rt: SshRuntime, depth: int = 0) -> None:
    opts: list[str] = []
    f_values: list[str] = []
    i_values: list[str] = []
    is_w = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if not a.startswith("-") or a == "-":
            i += 1
            continue
        flag = a[1:2]
        rest = a[2:]
        if flag == "o":
            val = rest if rest else (args[i + 1] if i + 1 < len(args) else "")
            if not rest:
                i += 1
            opts.append(val)
        elif flag == "F":
            f_values.append(rest if rest else (args[i + 1] if i + 1 < len(args) else ""))
            if not rest:
                i += 1
        elif flag == "i":
            i_values.append(rest if rest else (args[i + 1] if i + 1 < len(args) else ""))
            if not rest:
                i += 1
        elif flag == "W":
            is_w = True
            if not rest:
                i += 1
        elif flag == "J":
            spec = rest if rest else (args[i + 1] if i + 1 < len(args) else "")
            if not rest:
                i += 1
            if not _JUMP_RE.match(spec.replace(" ", "")):
                raise IsolationViolation("ssh: -J must be a plain host list")
        elif flag in "DLRSEwB":
            raise IsolationViolation(f"ssh: option -{flag} is not allowed")
        elif flag in "bcemOpQl" and flag != "":
            if not rest:
                i += 1
        i += 1
    if len(f_values) != 1 or f_values[0] != rt.ssh_config:
        raise IsolationViolation("ssh: exactly one '-F <injected ssh_config>' is required")
    try:
        cfg_text = Path(rt.ssh_config).read_text(encoding="utf-8")
    except OSError:
        raise IsolationViolation("ssh: the injected ssh_config cannot be read") from None
    validate_ssh_config_text(cfg_text, rt)
    for o in opts:
        key, _, value = o.partition("=")
        k = key.strip().lower()
        if k == "proxycommand":
            _validate_proxycommand(value, rt, depth)
            continue
        if k in _FORBIDDEN_O_KEYS or (k not in _SSH_CONFIG_ALLOWED and k not in _CLI_FIXED):
            raise IsolationViolation(f"ssh: '-o {k}' is not allowed")
        if k in _CLI_FIXED and value.strip().strip('"').lower() not in _CLI_FIXED[k]:
            raise IsolationViolation(f"ssh: '-o {k}' has a forbidden value")
        if k == "userknownhostsfile":
            for path in _known_hosts_values(value):
                check_sandbox_path(path, rt.sandbox, what="ssh -o UserKnownHostsFile")
        if k == "globalknownhostsfile" and value.strip() != rt.global_known_hosts:
            raise IsolationViolation("ssh: GlobalKnownHostsFile must be the injected file")
        if k in ("identityagent", ) and value.strip().lower() != "none":
            raise IsolationViolation("ssh: IdentityAgent must be none")
        if k == "identitiesonly" and value.strip().lower() != "yes":
            raise IsolationViolation("ssh: IdentitiesOnly must be yes")
    if is_w:
        return  # ProxyJump child: isolation comes from the validated -F file
    if not any(o.partition("=")[0].strip().lower() == "userknownhostsfile" for o in opts):
        raise IsolationViolation("ssh: -o UserKnownHostsFile is required")
    _require_o(opts, "GlobalKnownHostsFile", rt.global_known_hosts)
    _require_o(opts, "IdentityAgent", "none")
    _require_o(opts, "IdentitiesOnly", "yes")
    if i_values != [rt.identity]:
        raise IsolationViolation("ssh: exactly one '-i <injected identity>' is required")


_KEYGEN_FLAGS_NO_ARG = set("lqHrLy")
_KEYGEN_FLAGS_ARG = set("tFRfNCb")


def validate_ssh_keygen_argv(args: list[str], rt: SshRuntime) -> None:
    f_path = None
    flags: set[str] = set()
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("-") or len(a) < 2:
            raise IsolationViolation("ssh-keygen: unexpected positional argument")
        flag = a[1]
        if flag in _KEYGEN_FLAGS_ARG:
            val = a[2:] if len(a) > 2 else (args[i + 1] if i + 1 < len(args) else None)
            if val is None:
                raise IsolationViolation("ssh-keygen: missing option value")
            if len(a) == 2:
                i += 1
            if flag == "f":
                f_path = val
            if flag == "N" and val != "":
                raise IsolationViolation("ssh-keygen: only an empty passphrase is allowed")
        elif flag in _KEYGEN_FLAGS_NO_ARG and len(a) == 2:
            pass
        else:
            raise IsolationViolation(f"ssh-keygen: option '{a}' is not allowed")
        flags.add(flag)
        i += 1
    if f_path is None:
        raise IsolationViolation("ssh-keygen: '-f <file>' is required (no default ~/.ssh paths)")
    check_sandbox_path(f_path, rt.sandbox, what="ssh-keygen -f")
    if flags - set("tFRfNCbqlH"):
        raise IsolationViolation("ssh-keygen: unsupported option set")


_SSHD_ALLOWED = {
    "port", "listenaddress", "hostkey", "authorizedkeysfile", "pidfile", "passwordauthentication",
    "pubkeyauthentication", "usepam", "strictmodes", "hostkeyalgorithms", "loglevel",
    "kbdinteractiveauthentication", "permitrootlogin", "authenticationmethods", "allowusers",
    "permittty", "maxauthtries", "logingracetime", "useprivilegeseparation", "permitemptypasswords",
}
_SSHD_PATH_KEYS = {"hostkey", "authorizedkeysfile", "pidfile"}


def validate_sshd_config_text(text: str, rt: SshRuntime) -> None:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, value = _split_directive(line)
        if key not in _SSHD_ALLOWED:
            raise IsolationViolation(f"sshd_config: directive '{key}' is not allowed")
        if key in _SSHD_PATH_KEYS:
            check_sandbox_path(value, rt.sandbox, what=key)
        if key == "listenaddress" and value != "127.0.0.1":
            raise IsolationViolation("sshd_config: ListenAddress must be 127.0.0.1")
        if key == "passwordauthentication" and value.lower() != "no":
            raise IsolationViolation("sshd_config: PasswordAuthentication must be no")


def validate_sshd_argv(args: list[str], rt: SshRuntime) -> None:
    cfg = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-D", "-e", "-d", "-q"):
            pass
        elif a == "-f" and i + 1 < len(args):
            cfg = args[i + 1]
            i += 1
        else:
            raise IsolationViolation(f"sshd: argument '{a}' is not allowed")
        i += 1
    if cfg is None:
        raise IsolationViolation("sshd: '-f <sandbox config>' is required")
    real = check_sandbox_path(cfg, rt.sandbox, what="sshd -f")
    try:
        validate_sshd_config_text(Path(real).read_text(encoding="utf-8"), rt)
    except OSError:
        raise IsolationViolation("sshd: config cannot be read") from None


def validate_argv(kind: str, args: list[str], rt: SshRuntime) -> None:
    validate_runtime(rt)
    for a in args:
        if "\x00" in a:
            raise IsolationViolation("argument contains NUL")
    if kind == "ssh":
        validate_ssh_argv(args, rt)
    elif kind == "ssh-keygen":
        validate_ssh_keygen_argv(args, rt)
    elif kind == "sshd":
        validate_sshd_argv(args, rt)
    else:
        raise IsolationViolation("unknown wrapper kind")


# --------------------------------------------------------------------------
# Sandbox / wrapper generation
# --------------------------------------------------------------------------

_WRAPPER_NAMES = ("ssh", "ssh-keygen", "sshd")


def create_sandbox(
    base_dir: str, *, ssh: str, ssh_keygen: str, sshd: str, tmux: str,
) -> SshRuntime:
    """Create a 0700 sandbox under `base_dir` with wrapper scripts and a
    policy file. Does not execute any OpenSSH binary."""
    base = os.path.realpath(base_dir)
    sandbox = os.path.join(base, "sshiso")
    os.makedirs(sandbox, mode=0o700, exist_ok=True)
    os.chmod(sandbox, 0o700)
    for sub in ("bin", "home", "tmp", "etc", "run"):
        os.makedirs(os.path.join(sandbox, sub), mode=0o700, exist_ok=True)
    rt = SshRuntime(
        sandbox=sandbox, ssh=ssh, ssh_keygen=ssh_keygen, sshd=sshd, tmux=tmux,
        known_hosts=os.path.join(sandbox, "known_hosts"),
        global_known_hosts=os.path.join(sandbox, "global_known_hosts"),
        ssh_config=os.path.join(sandbox, "ssh_config"),
        identity=os.path.join(sandbox, "identity"),
        product_known_hosts=os.path.join(sandbox, "product_known_hosts"),
    )
    with open(rt.policy_file, "w", encoding="utf-8") as fh:
        json.dump(asdict(rt), fh)
    os.chmod(rt.policy_file, 0o600)
    for name in _WRAPPER_NAMES:
        path = rt.wrapper(name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/bin/sh\n"
                f"exec {shlex.quote(sys.executable)} -I -m network_lab_mcp.ssh_isolation "
                f"{shlex.quote(rt.policy_file)} {name} \"$@\"\n"
            )
        os.chmod(path, 0o700)
    return rt


def default_ssh_config(rt: SshRuntime, extra: str = "") -> str:
    """The only ssh_config shape the harness generates."""
    return (
        "Host *\n"
        f'  UserKnownHostsFile "{rt.product_known_hosts}" "{rt.known_hosts}"\n'
        f"  GlobalKnownHostsFile {rt.global_known_hosts}\n"
        f"  IdentityFile {rt.identity}\n"
        "  IdentitiesOnly yes\n"
        "  IdentityAgent none\n"
        "  ForwardAgent no\n"
        "  ForwardX11 no\n"
        "  PermitLocalCommand no\n"
        "  ControlMaster no\n"
        "  ControlPath none\n"
        "  UpdateHostKeys no\n"
        + extra
    )


def required_ssh_args(rt: SshRuntime, known_hosts: str | None = None) -> list[str]:
    """`known_hosts` may name another sandbox file (e.g. a probe's temporary
    file); the first -o occurrence wins in OpenSSH, so it must be the one
    emitted here. By default both the product file and the (stand-in) user
    file are consulted."""
    files = f'"{known_hosts}"' if known_hosts else f'"{rt.product_known_hosts}" "{rt.known_hosts}"'
    return [
        "-F", rt.ssh_config,
        "-o", f"UserKnownHostsFile={files}",
        "-o", f"GlobalKnownHostsFile={rt.global_known_hosts}",
        "-o", "IdentityAgent=none",
        "-o", "IdentitiesOnly=yes",
        "-i", rt.identity,
    ]


# --------------------------------------------------------------------------
# Test-only injection point and Python-side access guard
# --------------------------------------------------------------------------

_active_runtime: SshRuntime | None = None


def install_for_tests(rt: SshRuntime) -> None:
    """Private, import-only hook (never reachable from env/YAML/CLI/MCP)."""
    global _active_runtime
    validate_runtime(rt)
    _active_runtime = rt


def uninstall_for_tests() -> None:
    global _active_runtime
    _active_runtime = None


def active_runtime() -> SshRuntime | None:
    return _active_runtime


class forbid_real_ssh_area:
    """Context manager: Python-level open()/os.open() of the real user's
    ~/.ssh raises. It does NOT protect subprocesses (the forced wrapper and
    the isolated config do), and stat-before/after is only a supplement."""

    def __enter__(self):
        import builtins

        self._real_dir = real_user_ssh_dir()
        self._open, self._os_open = builtins.open, os.open
        guard = self

        def _check(path):
            try:
                real = os.path.realpath(os.fspath(path))
            except TypeError:
                return
            if _under(real, guard._real_dir):
                raise PermissionError("access to the real user's SSH area is forbidden in tests")

        def g_open(file, *a, **kw):
            if not isinstance(file, int):
                _check(file)
            return guard._open(file, *a, **kw)

        def g_os_open(path, *a, **kw):
            _check(path)
            return guard._os_open(path, *a, **kw)

        builtins.open, os.open = g_open, g_os_open
        return self

    def __exit__(self, *exc):
        import builtins

        builtins.open, os.open = self._open, self._os_open
        return False


# --------------------------------------------------------------------------
# Wrapper entry point
# --------------------------------------------------------------------------


def load_policy(path: str) -> SshRuntime:
    st = os.stat(path)
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise IsolationViolation("policy file must be owned by the user and mode 0600")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    rt = SshRuntime(**data)
    if os.path.realpath(path) != os.path.realpath(rt.policy_file):
        raise IsolationViolation("policy file is not the sandbox policy file")
    return rt


def _audit(policy_path: str, kind: str, decision: str, reason: str = "", argc: int = 0) -> None:
    """One JSON line per wrapper invocation inside the sandbox (never argv
    content: only kind, decision, reason, argument count)."""
    try:
        sandbox = os.path.dirname(os.path.realpath(policy_path))
        fd = os.open(os.path.join(sandbox, "audit.log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps({"ts": time.time(), "pid": os.getpid(), "kind": kind, "decision": decision,
                                 "reason": reason, "argc": argc}) + "\n")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("ssh_isolation: usage: <policy> <kind> args...\n")
        return EXIT_VIOLATION
    try:
        rt = load_policy(argv[0])
        kind, args = argv[1], argv[2:]
        validate_argv(kind, args, rt)
        exe = {"ssh": rt.ssh, "ssh-keygen": rt.ssh_keygen, "sshd": rt.sshd}[kind]
        env = build_clean_env(rt)
    except (IsolationViolation, OSError, ValueError, TypeError, KeyError) as exc:
        sys.stderr.write(f"ssh_isolation: refused: {exc}\n")
        _audit(argv[0], argv[1], "refused", str(exc)[:200], len(argv) - 2)
        return EXIT_VIOLATION
    _audit(argv[0], kind, "allowed", "", len(args))
    os.execve(exe, [exe, *args], env)
    return EXIT_VIOLATION  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
