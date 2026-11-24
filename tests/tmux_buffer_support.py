# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Helpers for tests/test_tmux_buffer_*.py: framed payloads, raw receiver
panes, and spawn-safe worker entry points that call the real production
`terminal._send_text_via_stdin()`. Only dummy data is ever used."""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "raw_stream_receiver.py"
# Printable markers: tmux (3.8) rewrites C0 control characters when pasting.
STX, ETX, US = b"[[NLM|", b"|NLM]]", b"|"

_BODIES = [
    "plain-ascii",
    "line1\nline2\nline3",
    "trailing-lf\n",
    "no-trailing-lf",
    "ws \t tab  spaces ",
    "semi;colon $dollar 'q' \"dq\" back\\slash !bang |pipe &amp (p)",
    "日本語のテキスト",
    "emoji 🚀🔥 mix é",
    "X" * 4000 + "\nend",
]


def body_for(seq: int) -> str:
    return _BODIES[seq % len(_BODIES)]


def frame(worker: str, seq: int) -> bytes:
    body = body_for(seq).encode("utf-8")
    return STX + f"{worker}:{seq}:{len(body)}".encode() + US + body + ETX


def parse_frames(data: bytes) -> tuple[list[tuple[str, int]], int]:
    """Return (well-formed (worker, seq) frames, corrupted-frame count)."""
    seen, bad, pos = [], 0, 0
    while pos < len(data):
        start = data.find(STX, pos)
        if start < 0:
            bad += 1 if data[pos:] else 0
            break
        if start > pos:
            bad += 1
        sep = data.find(US, start + len(STX))
        if sep < 0:
            bad += 1
            break
        try:
            worker, seq_s, len_s = data[start + len(STX) : sep].decode().split(":")
            seq, length = int(seq_s), int(len_s)
        except ValueError:
            bad += 1
            pos = start + 1
            continue
        end = sep + len(US) + length
        if data[end : end + len(ETX)] != ETX or data[sep + len(US) : end] != body_for(seq).encode("utf-8"):
            bad += 1
            pos = start + 1
            continue
        seen.append((worker, seq))
        pos = end + len(ETX)
    return seen, bad


def wait_for(predicate, what: str, timeout: float = 60.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")


def open_receiver(terminal, name: str, workdir: Path, total_bytes: int):
    """Open a validation session running the raw receiver; returns
    (session_name, out_path). Waits for raw mode before returning."""
    out, ready = workdir / f"{name}.out", workdir / f"{name}.ready"
    terminal.open_validation_session(
        name, [sys.executable, str(FIXTURE), str(out), str(ready), str(total_bytes), "120"]
    )
    wait_for(ready.exists, f"receiver {name} ready")
    return terminal.derive_validation_session_name(name), out


def send_loop(session: str, worker: str, count: int, result: dict) -> None:
    """Send `count` framed payloads; record failures into `result`."""
    from network_lab_mcp import terminal

    failures = 0
    for seq in range(count):
        try:
            terminal._send_text_via_stdin(session, frame(worker, seq).decode("utf-8"))
        except terminal.TerminalError as exc:
            failures += 1
            result.setdefault("errors", set()).add(str(exc))
    result["failures"] = result.get("failures", 0) + failures


def process_worker(socket_name: str, jobs: list, queue) -> None:
    """Spawn entry point: jobs = [(session, worker_id, count), ...], each run
    in its own thread (mixed Thread+Process load)."""
    from network_lab_mcp import terminal

    terminal.TMUX_SOCKET_NAME = socket_name
    results = [dict() for _ in jobs]
    threads = [
        threading.Thread(target=send_loop, args=(s, w, c, r)) for (s, w, c), r in zip(jobs, results, strict=True)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    queue.put(sum(r.get("failures", 0) for r in results))


def load_worker(terminal, stop: threading.Event, counters: dict, load_session: str, idx: int) -> None:
    """Mixed non-buffer tmux traffic against a dedicated load session."""
    ops = [
        lambda: terminal._run(["capture-pane", "-p", "-t", load_session], check=False),
        lambda: terminal._run(["display-message", "-p", "-t", load_session, "#{pane_id}"], check=False),
        lambda: terminal._run(["list-panes", "-a"], check=False),
        lambda: terminal._run(["list-sessions"], check=False),
        lambda: terminal._run(["send-keys", "-t", load_session, "-l", "x"], check=False),
        lambda: terminal._run(["pipe-pane", "-t", load_session, f"cat > /dev/null"], check=False),
    ]
    n = 0
    while not stop.is_set():
        ops[n % len(ops)]()
        if n % 7 == 0:
            name = f"nlm-load-{idx}-{uuid.uuid4().hex[:8]}"
            terminal._run(["new-session", "-d", "-s", name, "cat"], check=False)
            terminal._run(["kill-session", "-t", name], check=False)
        n += 1
    counters[idx] = n


def residual_nlm_buffers(terminal) -> list[str]:
    out = terminal._run(["list-buffers", "-F", "#{buffer_name}"], check=False).stdout
    return [b for b in out.split() if b.startswith("nlm-")]


def stress_enabled() -> bool:
    return os.environ.get("NETWORK_LAB_STRESS") == "1"


def scale() -> int:
    return max(1, int(os.environ.get("NLM_STRESS_SCALE", "1")))
