# SPDX-License-Identifier: Apache-2.0
"""Building blocks for telnet over an SSH local port forward (macOS).

These are pure/inspectable helpers (used by terminal_open for telnet via jump)
and are used only by terminal.py's telnet-via-jump path. Everything fails closed:
any doubt raises TunnelSafetyError and no credential may be sent.

`lsof -F` output is parsed (never the human table). Fields requested:
p (PID), c (command), f (FD), n (socket name / 4-tuple), T (TCP state, as
`TST=...`)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from dataclasses import dataclass


class TunnelSafetyError(Exception):
    """Ownership or environment could not be proven safe."""


# ---- telnet client ------------------------------------------------------


def resolve_telnet_client(search_path: str | None = None) -> str:
    """Absolute path of an executable telnet client, resolved once by the
    MCP process so the pane's own PATH cannot substitute another binary."""
    found = shutil.which("telnet", path=search_path)
    if not found:
        raise TunnelSafetyError("telnet client not found")
    resolved = os.path.realpath(found)
    if not (os.path.isfile(resolved) and os.access(resolved, os.X_OK)):
        raise TunnelSafetyError("telnet client not found")
    return resolved


def telnet_argv(client: str, port: int) -> list[str]:
    """`-K` disables automatic login (no local user name is offered);
    `-E` disables the escape character. The loopback host is fixed."""
    if not os.path.isabs(client):
        raise TunnelSafetyError("telnet client path must be absolute")
    return [client, "-K", "-E", "127.0.0.1", str(int(port))]


# ---- local port ---------------------------------------------------------


def allocate_loopback_port() -> int:
    """A currently free 127.0.0.1 port. Inherently racy (the port is
    released before ssh binds it); callers must retry only on an explicit
    bind conflict and must still verify ownership afterwards."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---- lsof -F parsing ----------------------------------------------------


@dataclass(frozen=True)
class SocketRecord:
    pid: int
    command: str
    fd: str
    name: str  # "local:port" or "local:port->remote:port"
    state: str  # "LISTEN", "ESTABLISHED", ...


def parse_lsof_f(output: str) -> list[SocketRecord]:
    """Parse `lsof -nP -F pcfnT` output. Raises on anything malformed."""
    records: list[SocketRecord] = []
    pid: int | None = None
    command = ""
    fd = name = state = None
    saw_any = False

    def flush() -> None:
        nonlocal fd, name, state
        if fd is not None:
            if pid is None or name is None or state is None:
                raise TunnelSafetyError("lsof output could not be parsed")
            records.append(SocketRecord(pid, command, fd, name, state))
        fd = name = state = None

    for line in output.splitlines():
        if not line:
            continue
        saw_any = True
        tag, value = line[0], line[1:]
        if tag == "p":
            flush()
            try:
                pid = int(value)
            except ValueError:
                raise TunnelSafetyError("lsof output could not be parsed") from None
            command = ""
        elif tag == "c":
            command = value
        elif tag == "f":
            flush()
            fd = value
        elif tag == "n":
            name = value
        elif tag == "T":
            if value.startswith("ST="):
                state = value[3:]
        # other fields (t, TQR, TQS, ...) are ignored
    flush()
    if not saw_any:
        raise TunnelSafetyError("lsof output could not be parsed")
    return records


def run_lsof() -> str:
    """Machine-readable snapshot of all TCP sockets; fail closed if lsof is
    missing, fails or lacks permission."""
    lsof = shutil.which("lsof")
    if not lsof:
        raise TunnelSafetyError("lsof not available")
    try:
        result = subprocess.run(
            [lsof, "-nP", "-iTCP", "-F", "pcfnT"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        raise TunnelSafetyError("lsof failed") from None
    if result.returncode not in (0, 1) or (result.returncode == 1 and result.stdout.strip() == ""):
        raise TunnelSafetyError("lsof failed")
    return result.stdout


# ---- connection ownership ----------------------------------------------


@dataclass(frozen=True)
class ConnectionIdentity:
    client_port: int
    tunnel_port: int
    server_pid: int


def _split(name: str) -> tuple[str, str] | None:
    return tuple(name.split("->", 1)) if "->" in name else None  # type: ignore[return-value]


def verify_tcp_ownership(
    lsof_output: str, *, tunnel_port: int, managed_ssh_pids: frozenset[int], client_pids: frozenset[int]
) -> ConnectionIdentity:
    """Prove that exactly one ESTABLISHED loopback connection from a client
    process (the telnet client) reaches `tunnel_port`, that BOTH ends are
    visible as ESTABLISHED (client side owned by `client_pids`, server side
    owned by a managed ssh pid), and that the listener is owned by a managed
    ssh pid. Any absence, ambiguity or foreign owner raises."""
    records = parse_lsof_f(lsof_output)
    listen = f"127.0.0.1:{tunnel_port}"
    listeners = [r for r in records if r.state == "LISTEN" and r.name == listen]
    if any(r.pid not in managed_ssh_pids for r in listeners) or not listeners:
        raise TunnelSafetyError("tunnel ownership could not be verified")

    client_side = []
    for r in records:
        parts = _split(r.name)
        if r.state == "ESTABLISHED" and parts and parts[1] == listen and r.pid in client_pids:
            client_side.append((r, parts[0]))
    if len(client_side) != 1:
        raise TunnelSafetyError("tunnel ownership could not be verified")
    client_rec, client_endpoint = client_side[0]

    server_side = [
        r for r in records
        if r.state == "ESTABLISHED" and _split(r.name) == (listen, client_endpoint)
    ]
    if len(server_side) != 1 or server_side[0].pid not in managed_ssh_pids:
        raise TunnelSafetyError("tunnel ownership could not be verified")
    try:
        client_port = int(client_endpoint.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        raise TunnelSafetyError("tunnel ownership could not be verified") from None
    return ConnectionIdentity(client_port, tunnel_port, server_side[0].pid)


def verify_twice(snapshot_a: str, snapshot_b: str, **kw) -> ConnectionIdentity:
    """Both snapshots must independently verify and yield the same 4-tuple
    and owner. This narrows, but does not eliminate, a TOCTOU window."""
    first = verify_tcp_ownership(snapshot_a, **kw)
    second = verify_tcp_ownership(snapshot_b, **kw)
    if first != second:
        raise TunnelSafetyError("tunnel ownership could not be verified")
    return first


def verify_listener(lsof_output: str, *, tunnel_port: int, managed_ssh_pids: frozenset[int]) -> None:
    """Exactly the managed ssh process listens on 127.0.0.1:<port>."""
    listen = f"127.0.0.1:{tunnel_port}"
    listeners = [r for r in parse_lsof_f(lsof_output) if r.state == "LISTEN" and r.name == listen]
    if len(listeners) < 1 or any(r.pid not in managed_ssh_pids for r in listeners):
        raise TunnelSafetyError("tunnel ownership could not be verified")
