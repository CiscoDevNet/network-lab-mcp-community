# SPDX-License-Identifier: Apache-2.0
"""Minimal VT100/xterm screen decoder for PTY tests (stdlib only).

Handles exactly the controls prompt_toolkit's xterm output emits for this
CLI: printable text, CR/LF/BS/BEL, CSI A/B/C/D/G/H/J/K, DEC autowrap
(?7h/l), and answers CPR (CSI 6n) from the emulated cursor. It tracks
scrollback, so tests can inspect everything a user could scroll back to
(`history()`), not just the final 80x24 screen. Unknown CSI sequences that
would change layout (L, M, S, T, P, @, X) raise, so a limitation shows up
as a failure instead of a silent mis-render.
"""

from __future__ import annotations

import re

_CSI = re.compile(r"\x1b\[([0-9;?<>=]*)([@-~])")
_LAYOUT_UNSUPPORTED = set("LMSTP@X")


class Screen:
    def __init__(self, rows: int = 24, cols: int = 80) -> None:
        self.rows, self.cols = rows, cols
        self.screen = [[" "] * cols for _ in range(rows)]
        self.scrollback: list[str] = []
        self.r = self.c = 0
        self.autowrap = True
        self._pending = ""

    # -- public ---------------------------------------------------------
    def feed(self, text: str) -> str:
        """Feed decoded output; returns bytes to answer back (CPR replies)."""
        text = self._pending + text
        self._pending = ""
        reply = ""
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\x1b":
                if i + 1 >= len(text):
                    self._pending = text[i:]
                    break
                if text[i + 1] == "[":
                    m = _CSI.match(text, i)
                    if not m:
                        self._pending = text[i:]
                        break
                    reply += self._csi(m.group(1), m.group(2))
                    i = m.end()
                    continue
                i += 2  # other two-byte escapes: ignore
                continue
            self._char(ch)
            i += 1
        return reply

    def lines(self) -> list[str]:
        return [("".join(row)).rstrip() for row in self.screen]

    def history(self) -> list[str]:
        """Scrollback plus current screen, trailing blank rows trimmed."""
        rows = self.scrollback + self.lines()
        while rows and not rows[-1]:
            rows.pop()
        return rows

    # -- internals ------------------------------------------------------
    def _char(self, ch: str) -> None:
        if ch == "\r":
            self.c = 0
        elif ch == "\n":
            self._down()
        elif ch == "\x08":
            self.c = max(0, self.c - 1)
        elif ch < " " or ch == "\x7f":
            return
        else:
            if self.c >= self.cols:
                if self.autowrap:
                    self.c = 0
                    self._down()
                else:
                    self.c = self.cols - 1
            self.screen[self.r][self.c] = ch
            self.c += 1

    def _down(self) -> None:
        if self.r == self.rows - 1:
            self.scrollback.append("".join(self.screen[0]).rstrip())
            self.screen = self.screen[1:] + [[" "] * self.cols]
        else:
            self.r += 1

    def _csi(self, params: str, final: str) -> str:
        if params.startswith("?"):
            if params == "?7":
                self.autowrap = final == "h"
            return ""
        nums = [int(p) if p else 0 for p in params.split(";")] if params else []
        n = nums[0] if nums and nums[0] else 1
        if final == "A":
            self.r = max(0, self.r - n)
        elif final == "B":
            self.r = min(self.rows - 1, self.r + n)
        elif final == "C":
            self.c = min(self.cols - 1, self.c + n)
        elif final == "D":
            self.c = max(0, self.c - n)
        elif final == "G":
            self.c = min(self.cols - 1, n - 1)
        elif final == "H":
            self.r = min(self.rows - 1, (nums[0] or 1) - 1) if nums else 0
            self.c = min(self.cols - 1, ((nums[1] if len(nums) > 1 else 1) or 1) - 1)
        elif final == "J":
            mode = nums[0] if nums else 0
            if mode == 0:
                self.screen[self.r][self.c :] = [" "] * (self.cols - self.c)
                for rr in range(self.r + 1, self.rows):
                    self.screen[rr] = [" "] * self.cols
            elif mode == 2:
                self.screen = [[" "] * self.cols for _ in range(self.rows)]
        elif final == "K":
            mode = nums[0] if nums else 0
            if mode == 0:
                self.screen[self.r][self.c :] = [" "] * (self.cols - self.c)
            elif mode == 2:
                self.screen[self.r] = [" "] * self.cols
        elif final == "n" and nums and nums[0] == 6:
            return f"\x1b[{self.r + 1};{self.c + 1}R"
        elif final in _LAYOUT_UNSUPPORTED:
            raise NotImplementedError(f"unsupported layout CSI {params!r}{final}")
        return ""
