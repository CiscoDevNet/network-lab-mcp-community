#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Test fixture helper: puts stdin into raw tty mode (disabling ICRNL/
ICANON/etc. line-discipline translation) and writes every byte it reads
to a binary file, so a test can observe the exact bytes a tmux pane's
child process actually received -- never what a cooked pty or tmux's own
capture-pane VT100 rendering would show, either of which can obscure a
CR/LF distinction that matters for characterizing terminal_send()'s byte-
level contract.

Usage: raw_stdin_capture.py <output-path> [idle-timeout-seconds]

Reads until `idle-timeout-seconds` (default 2.0) elapses with no new
bytes, then writes whatever was collected and exits."""
import select
import sys
import termios
import tty

out_path = sys.argv[1]
idle_timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0

fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setraw(fd)

data = bytearray()
try:
    while True:
        ready, _, _ = select.select([fd], [], [], idle_timeout)
        if not ready:
            break
        chunk = sys.stdin.buffer.read1(4096)
        if not chunk:
            break
        data.extend(chunk)
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)

with open(out_path, "wb") as f:
    f.write(bytes(data))
