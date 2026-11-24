#!/usr/bin/env python3
# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Test fixture: put the pane tty in raw mode, create <ready> (atomically),
then append every received byte to <out> until <total> bytes arrived or
<timeout> seconds pass without progress. Used by the tmux buffer tests to
observe exactly what a pane received (no cooked-mode translation)."""
import os
import select
import sys
import termios
import tty


def main() -> None:
    out, ready, total, timeout = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    got = 0
    with open(out, "ab", buffering=0) as handle:
        tmp = f"{ready}.tmp"
        open(tmp, "wb").close()
        os.replace(tmp, ready)
        try:
            while got < total:
                r, _, _ = select.select([fd], [], [], timeout)
                if not r:
                    break
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                handle.write(chunk)
                got += len(chunk)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


main()
