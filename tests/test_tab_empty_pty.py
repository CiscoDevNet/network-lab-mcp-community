# SPDX-License-Identifier: Apache-2.0
"""Real-PTY check of the IOS XR-style empty-input Tab no-op.

Runs the actual CLI (prompt_toolkit) in a pseudo-terminal against a
throwaway lab in tmp_path (never the repository lab/). Synchronization is
by sentinel: after the keys under test a unique marker character is typed,
and since keys are processed in order, everything read before the marker
echoes back is exactly the output the keys under test produced.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import struct
import sys
import termios
import time
from pathlib import Path

import pytest
import yaml

from vt_screen import Screen

SRC = str(Path(__file__).resolve().parent.parent / "src")
DRIVER = (
    "import sys, pathlib\n"
    "from network_lab_mcp import lab\n"
    "root = pathlib.Path(sys.argv[1])\n"
    "lab.find_lab_root = lambda: root\n"
    "from network_lab_mcp.cli import main\n"
    "main.main()\n"
)
CSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x08")
DEADLINE = 20.0


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _make_lab(tmp: Path, extra_refs: tuple[str, ...] = ()) -> Path:
    root = tmp / "lab"
    _write(root / "settings.yaml", {"active_access_info": "sample_lab", "active_topology": "sample_lab",
                                    "active_scenario": "ref_beta", "active_references": ["ref_beta"]})
    _write(root / "principles.yaml", {"general_operating_principles": ["x"]})
    _write(root / "topologies" / "sample_lab.yaml",
           {"name": "sample_lab", "description": "d", "devices": {"R1": {"type": "iosxr"}}, "links": []})
    _write(root / "access-info" / "sample_lab.yaml",
           {"name": "sample_lab", "devices": {"R1": {"type": "iosxr", "address": "192.0.2.11",
                                                     "transport": "ssh", "port": 22}}})
    for name in ("ref_alpha_one", "ref_alpha_two", "ref_beta", "ref_gamma", *extra_refs):
        _write(root / "references" / f"{name}.yaml", {"name": name, "description": "d"})
        _write(root / "scenarios" / f"{name}.yaml", {"name": name, "description": "d"})
    return root


class _Cli:
    def __init__(self, tmp: Path, rows: int = 24, cols: int = 80, extra_refs: tuple[str, ...] = ()) -> None:
        root = _make_lab(tmp, extra_refs)
        self.screen = Screen(rows, cols)
        env = {
            "PATH": os.environ.get("PATH", ""), "TERM": "xterm", "HOME": str(tmp),
            "INPUTRC": "/dev/null", "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # pragma: no cover - child
            os.execve(sys.executable, [sys.executable, "-c", DRIVER, str(root)], env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.log = ""
        self.deadline = time.monotonic() + DEADLINE

    def _pump(self, timeout: float = 0.1) -> None:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if ready:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            text = data.decode("utf-8", "replace")
            # Answer cursor-position requests from the emulated cursor, like
            # a real terminal does.
            reply = self.screen.feed(text)
            if reply:
                os.write(self.fd, reply.encode())
            self.log += text

    def wait_for(self, needle: str, start: int = 0) -> int:
        while time.monotonic() < self.deadline:
            idx = CSI.sub("", self.log).find(needle, start)
            if idx >= 0:
                return idx + len(needle)
            self._pump()
        raise AssertionError(f"timeout waiting for {needle!r}; output so far: {CSI.sub('', self.log)!r}")

    def settle(self, quiet: float = 0.5) -> None:
        """Wait until the terminal has produced no output for `quiet` seconds."""
        quiet_since = time.monotonic()
        size = len(self.log)
        while time.monotonic() < self.deadline and time.monotonic() - quiet_since < quiet:
            self._pump(0.05)
            if len(self.log) != size:
                size = len(self.log)
                quiet_since = time.monotonic()

    def segment(self, keys: str, marker: str = "Q") -> str:
        """Send `keys` plus a marker; return the visible output they produced."""
        self.settle()
        start = len(CSI.sub("", self.log))
        os.write(self.fd, keys.encode())
        os.write(self.fd, marker.encode())
        self.wait_for(marker, start)
        # run_in_terminal() prints asynchronously, so the marker echo is not
        # ordered against a candidate list: after the marker, remove it from
        # the buffer again and wait for the terminal to go quiet, then
        # inspect everything produced since `start`.
        os.write(self.fd, b"\x7f")
        self.settle()
        return CSI.sub("", self.log)[start:].replace(marker, "", 1)

    def line(self, text: str) -> str:
        start = len(CSI.sub("", self.log))
        os.write(self.fd, (text + "\r").encode())
        self.wait_for("#", start + len(text) + 1)
        return CSI.sub("", self.log)[start:]

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.kill(self.pid, 9)
        except OSError:
            pass
        os.waitpid(self.pid, 0)


def _open(tmp_path, **kwargs):
    if not hasattr(os, "fork"):
        pytest.skip("PTY not available on this platform")
    c = _Cli(tmp_path, **kwargs)
    c.wait_for("network-lab#")
    return c


@pytest.fixture()
def cli(tmp_path):
    c = _open(tmp_path)
    try:
        yield c
    finally:
        c.close()


def _enter_config(cli: _Cli) -> None:
    cli.line("config")
    cli.wait_for("network-lab(config)#")


def test_empty_and_whitespace_tab_prints_nothing_in_exec_mode(cli):
    out = cli.segment("\t\t\t")
    assert "\n" not in out and "\r" not in out
    assert "configure" not in out and "show" not in out
    out = cli.segment("\x15" + "  \t\t")  # Ctrl-U, then whitespace + Tabs
    assert "\n" not in out and "configure" not in out and "show" not in out


def test_empty_tab_prints_nothing_in_config_mode(cli):
    _enter_config(cli)
    out = cli.segment("\t\t\t\t")
    assert "\n" not in out and "\r" not in out
    for name in ("running-config", "access-info", "commit", "reference"):
        assert name not in out


def test_completion_still_works_after_noop_tabs(cli):
    _enter_config(cli)
    cli.segment("\t\t")
    out = cli.segment("ref\t")
    assert "erence" in out


def test_accepted_command_after_completion_is_reference_ref_beta(cli):
    _enter_config(cli)
    cli.segment("\t")
    text = cli.line("reference ref_b\t")  # Tab is part of the typed line
    visible = text.replace("\r", "")
    assert "reference ref_beta" in visible
    assert "Unknown command" not in visible and "Invalid" not in visible


def test_ambiguous_tab_lists_candidates_once_per_press(cli):
    _enter_config(cli)
    cli.segment("reference ref_alpha_")
    for _ in range(2):
        out = cli.segment("\t")
        assert out.count("ref_alpha_one") == 1 and out.count("ref_alpha_two") == 1


# ==========================================================================
# Visible-screen checks (scrollback-aware, via vt_screen.Screen)
# ==========================================================================

PROMPT = "network-lab(config)# "


def _tab_once(c: _Cli, keys: str) -> tuple[list[str], list[str]]:
    """Type `keys` (no Tab), settle, then send exactly one Tab and settle.
    Returns the full scrollback+screen history before and after the Tab."""
    c.settle()
    os.write(c.fd, keys.encode())
    c.settle()
    before = c.screen.history()
    os.write(c.fd, b"\t")
    c.settle()
    return before, c.screen.history()


def test_empty_argument_token_tab_changes_nothing_on_screen(cli):
    _enter_config(cli)
    for command in ("reference ", "topology ", "scenario "):
        cli.settle()
        os.write(cli.fd, b"\x15")  # Ctrl-U
        before, after = _tab_once(cli, command)
        assert after == before
        assert after[-1] == f"{PROMPT}{command}".rstrip()
        assert (cli.screen.r, cli.screen.c) == (len(after) - 1, len(PROMPT) + len(command))


def test_command_word_without_space_tab_changes_nothing_on_screen(cli):
    _enter_config(cli)
    before, after = _tab_once(cli, "reference")
    assert after == before


def test_ambiguous_tab_keeps_line_then_aligned_candidates_then_prompt(cli):
    _enter_config(cli)
    before, after = _tab_once(cli, "reference ref_alpha_")
    # The edited line is kept, candidates follow on one aligned row with no
    # blank lines around them, and the prompt + buffer come back below.
    assert after[len(before) - 1 :] == [
        f"{PROMPT}reference ref_alpha_",
        "  ref_alpha_one  ref_alpha_two",
        f"{PROMPT}reference ref_alpha_",
    ]
    assert cli.screen.c == len(PROMPT) + len("reference ref_alpha_")
    # The history above the Tab line is untouched (no re-emitted old prompt).
    assert after[: len(before) - 1] == before[:-1]


def test_repeated_tab_lists_once_per_press(cli):
    _enter_config(cli)
    _tab_once(cli, "reference ref_alpha_")
    os.write(cli.fd, b"\t")
    cli.settle()
    history = cli.screen.history()
    assert sum(1 for line in history if line == "  ref_alpha_one  ref_alpha_two") == 2
    assert sum(1 for line in history if line.startswith("network-lab# config")) == 1
    assert history[-1] == f"{PROMPT}reference ref_alpha_"
    assert "" not in history[history.index("  ref_alpha_one  ref_alpha_two") :]


def test_accept_completed_command_and_question_mark_unchanged(cli):
    _enter_config(cli)
    cli.settle()
    os.write(cli.fd, b"reference ref_b\t")
    cli.settle()
    assert cli.screen.history()[-1] == f"{PROMPT}reference ref_beta"
    os.write(cli.fd, b"\r")
    cli.settle()
    assert cli.screen.history()[-1].startswith("network-lab(config-reference-ref_beta)#")
    os.write(cli.fd, b"\x15")  # nothing typed yet; back out via exit
    os.write(cli.fd, b"exit\r")
    cli.settle()
    os.write(cli.fd, b"reference ?")
    cli.settle()
    history = cli.screen.history()
    assert any(line.startswith("  ref_beta") and "Existing reference definition" in line for line in history)
    assert any(line.startswith("  <name>") for line in history)


@pytest.mark.parametrize("cols, expected_rows", [(40, 4), (80, 2), (120, 1)])
def test_candidate_columns_adapt_to_terminal_width(tmp_path, cols, expected_rows):
    names = tuple(f"wide_item_{ch}x" for ch in "abcdefgh")
    c = _open(tmp_path, cols=cols, extra_refs=names)
    try:
        _enter_config(c)
        before, after = _tab_once(c, "reference wide_item_")
        # At 40 columns the 41-character prompt line itself wraps, so pick
        # the candidate rows by content instead of by position.
        rows = [line for line in after[len(before) - 1 :] if line.startswith("  wide_item_")]
        assert len(rows) == expected_rows
        assert all(len(r) < cols for r in rows)
        assert " ".join(" ".join(rows).split()).split(" ") == list(names)
        assert f"{PROMPT}reference wide_item_".endswith(after[-1])
        assert after[: len(before) - 1] == before[:-1]
    finally:
        c.close()
