#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Test fixture helper: puts stdin into raw tty mode (disabling ICRNL/
ICANON/etc. line-discipline translation) and records every byte it reads,
so a test can observe the exact bytes a tmux pane's child process actually
received -- never what a cooked pty or tmux's own capture-pane VT100
rendering would show, either of which can obscure a CR/LF distinction that
matters for characterizing terminal_send()'s byte-level contract.

Usage:
    raw_stdin_capture.py <output-path> --ready <ready-path> --expect <N>
        [--quiet SECONDS] [--timeout SECONDS]
        [--events <events-path>] [--pre-raw-delay S] [--save-delay S]

Synchronization contract (neither channel is the pane's own stdin/stdout,
so nothing diagnostic ever mixes into the captured bytes):

* Readiness: `<ready-path>` is created (atomically, via rename) only after
  tty.setraw() has completed. tty.setraw() flushes pending input
  (TCSAFLUSH), so anything typed into the pane before this point is
  discarded; a test must wait for this file before sending.
* Completion: `<output-path>` is created atomically (temp file + rename)
  and only once capturing is finished -- so its mere existence means the
  content is final and complete. Capturing finishes when at least N bytes
  have arrived and the input has then been quiet for --quiet seconds
  (default 0.3; the quiet window still catches *extra* bytes, keeping the
  comparison exact), or when --timeout seconds (default 10) pass without
  reaching N bytes.

Test-only knobs: --pre-raw-delay delays tty.setraw() (simulates a slow
interpreter start); --save-delay delays writing the result (simulates a
slow save); --events appends JSON lines with CLOCK_MONOTONIC timestamps
(raw mode set, each receive, saved) to a separate side file."""
import argparse
import json
import os
import select
import sys
import termios
import time
import tty


def _atomic_write(path: str, payload: bytes) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "wb") as handle:
        handle.write(payload)
    os.replace(tmp, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--ready", required=True)
    parser.add_argument("--expect", type=int, required=True)
    parser.add_argument("--quiet", type=float, default=0.3)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--events")
    parser.add_argument("--pre-raw-delay", type=float, default=0.0)
    parser.add_argument("--save-delay", type=float, default=0.0)
    args = parser.parse_args()

    def event(kind: str, **fields) -> None:
        if args.events:
            with open(args.events, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"t": time.monotonic_ns(), "who": "capture", "ev": kind, **fields}) + "\n")

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    time.sleep(args.pre_raw_delay)
    tty.setraw(fd)
    event("raw_set")
    _atomic_write(args.ready, b"")

    data = bytearray()
    try:
        while True:
            wait = args.quiet if len(data) >= args.expect else args.timeout
            ready, _, _ = select.select([fd], [], [], wait)
            if not ready:
                break
            chunk = sys.stdin.buffer.read1(4096)
            if not chunk:
                break
            data.extend(chunk)
            event("rx", hex=chunk.hex())
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    time.sleep(args.save_delay)
    _atomic_write(args.output, bytes(data))
    event("saved", n=len(data))


main()
