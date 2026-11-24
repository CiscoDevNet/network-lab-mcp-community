# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Deterministic regression tests for tmux buffer delivery/cleanup.

A FakeTmux models tmux 3.2a's behavior: `delete-buffer -b MISSING` deletes the
NEWEST buffer instead of failing; `save-buffer -b MISSING` fails with
"no buffer MISSING"; `paste-buffer -d` deletes what it pastes; a failing
command aborts the rest of a `;` list. This is the Fake's model of tmux, not
evidence about real tmux -- tests/test_tmux_buffer_real.py checks the real thing.
All data is dummy."""

from __future__ import annotations

import logging
import re
import subprocess
import uuid
from pathlib import Path

import pytest

from network_lab_mcp import terminal

SECRET = "DUMMY_SECRET_VALUE_9137"


class FakeTmux:
    def __init__(self):
        self.buffers: dict[str, bytes] = {}  # insertion order = oldest first
        self.log: list[list[str]] = []
        self.pasted: list[bytes] = []
        self.fail: dict[str, tuple[str, int, str]] = {}  # verb -> (kind, rc, stderr)
        self.hooks: dict[str, object] = {}  # verb -> callable(args) run before the command
        self.paste_targets_missing = False

    def run(self, argv, **kwargs):
        args = list(argv[argv.index("-L") + 2 :]) if "-L" in argv else list(argv[1:])
        self.log.append(args)
        stdin = kwargs.get("input")
        cmds, cur = [], []
        for a in args:
            if a == ";":
                cmds.append(cur)
                cur = []
            else:
                cur.append(a)
        cmds.append(cur)
        rc, err = 0, ""
        for cmd in cmds:
            hook = self.hooks.get(cmd[0])
            if hook:
                hook(cmd)
            if cmd[0] in self.fail:
                kind, frc, ferr = self.fail[cmd[0]]
                if kind == "timeout":
                    raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"), output=SECRET.encode(), stderr=SECRET.encode())
                if kind == "oserror":
                    raise FileNotFoundError(SECRET)
                if kind == "calledprocess":
                    raise subprocess.CalledProcessError(1, argv, output=SECRET.encode(), stderr=SECRET.encode())
                rc, err = frc, ferr.replace("{NAME}", cmd[cmd.index("-b") + 1] if "-b" in cmd else "")
                break
            rc, err = self._exec(cmd, stdin)
            if rc:
                break
        return subprocess.CompletedProcess(argv, rc, b"", err.encode())

    def _exec(self, cmd, stdin):
        verb, name = cmd[0], (cmd[cmd.index("-b") + 1] if "-b" in cmd else None)
        if verb == "load-buffer":
            assert isinstance(stdin, bytes)
            self.buffers[name] = stdin
            return 0, ""
        if verb == "paste-buffer":
            if name not in self.buffers:
                return 1, f"no buffer {name}"
            if self.paste_targets_missing:
                return 1, "can't find pane: %9"
            self.pasted.append(self.buffers[name])
            if "-d" in cmd:
                del self.buffers[name]
            return 0, ""
        if verb == "delete-buffer":
            if name in self.buffers:  # 3.2a: missing name -> newest buffer
                del self.buffers[name]
            elif self.buffers:
                del self.buffers[list(self.buffers)[-1]]
            return 0, ""
        if verb == "save-buffer":
            return (0, "") if name in self.buffers else (1, f"no buffer {name}")
        raise AssertionError(f"unexpected tmux command {cmd}")

    def verbs(self):
        return [c[0] for c in self.log]


@pytest.fixture
def tmux(monkeypatch):
    fake = FakeTmux()
    monkeypatch.setattr(terminal, "_tmux_base", lambda: ["tmux", "-L", "fake"])
    monkeypatch.setattr(terminal, "_send_target", lambda name: "%1")
    monkeypatch.setattr(terminal.subprocess, "run", fake.run)
    return fake


def _assert_safe_error(exc: terminal.TerminalError):
    for text in (str(exc), repr(exc)):
        assert SECRET not in text
    assert exc.__cause__ is None and exc.__context__ is None


# ---- R01/R02/R03: the legacy double delete vs. the fixed sender ----


def test_r01_legacy_double_delete_destroys_another_workers_buffer(tmux):
    tmux.buffers["A"], tmux.buffers["B"] = b"a", b"b"
    tmux._exec(["paste-buffer", "-r", "-d", "-b", "A"], None)
    tmux._exec(["delete-buffer", "-b", "A"], None)  # legacy finally-clause
    assert "B" not in tmux.buffers


def test_r02_r03_fixed_sender_keeps_other_buffers_and_never_deletes_on_success(tmux):
    tmux.buffers["nlm-other-worker"] = b"b"
    terminal._send_text_via_stdin("s", "hello")
    assert tmux.pasted == [b"hello"]
    assert set(tmux.buffers) == {"nlm-other-worker"}
    assert "delete-buffer" not in tmux.verbs() and "save-buffer" not in tmux.verbs()


def test_r02_other_worker_loads_between_our_load_and_paste(tmux):
    tmux.hooks["paste-buffer"] = lambda cmd: tmux.buffers.setdefault("nlm-b", b"b")
    terminal._send_text_via_stdin("s", "x")
    assert "nlm-b" in tmux.buffers


# ---- failure matrix ----


def test_r04_r08_paste_failure_cleans_only_own_buffer(tmux):
    tmux.buffers["nlm-other"] = b"o"
    tmux.paste_targets_missing = True
    with pytest.raises(terminal.TerminalError, match="target pane is unavailable"):
        terminal._send_text_via_stdin("s", SECRET)
    assert set(tmux.buffers) == {"nlm-other"}  # own secret buffer removed, other kept
    cleanup = tmux.log[-1]
    assert cleanup[0] == "save-buffer" and cleanup[4] == ";" and cleanup[5] == "delete-buffer"
    assert tmux.verbs().count("paste-buffer") == 1  # never retried


def test_r05_r19_exact_no_buffer_error_runs_no_cleanup(tmux):
    tmux.buffers["nlm-other"] = b"o"
    tmux.fail["paste-buffer"] = ("rc", 1, "no buffer {NAME}")
    with pytest.raises(terminal.TerminalError, match="unexpectedly disappeared"):
        terminal._send_text_via_stdin("s", "x")
    assert "save-buffer" not in tmux.verbs() and "delete-buffer" not in tmux.verbs()
    assert "nlm-other" in tmux.buffers


def test_r07_load_failure_never_deletes_other_buffers(tmux):
    tmux.buffers["nlm-other"] = b"o"
    tmux.fail["load-buffer"] = ("rc", 1, f"boom {SECRET}")
    with pytest.raises(terminal.TerminalError, match="load-buffer failed") as ei:
        terminal._send_text_via_stdin("s", "x")
    _assert_safe_error(ei.value)
    assert "nlm-other" in tmux.buffers
    assert "delete-buffer" not in [c[0] for c in tmux.log if ";" not in c]


def test_r09_r23_paste_timeout_is_unknown_not_success_and_not_retried(tmux):
    tmux.fail["paste-buffer"] = ("timeout", 0, "")
    with pytest.raises(terminal.TerminalError, match="delivery result is unknown") as ei:
        terminal._send_text_via_stdin("s", SECRET)
    _assert_safe_error(ei.value)
    assert tmux.verbs().count("paste-buffer") == 1 and tmux.verbs().count("load-buffer") == 1


def test_load_timeout_not_retried_and_finite_timeouts_passed(tmux, monkeypatch):
    seen = {}
    real = tmux.run

    def spy(argv, **kw):
        seen.setdefault(next(a for a in argv if a.endswith("-buffer")), kw.get("timeout"))
        return real(argv, **kw)

    monkeypatch.setattr(terminal.subprocess, "run", spy)
    tmux.fail["load-buffer"] = ("timeout", 0, "")
    with pytest.raises(terminal.TerminalError, match="load-buffer timed out"):
        terminal._send_text_via_stdin("s", "x")
    assert seen["load-buffer"] == terminal._BUFFER_LOAD_TIMEOUT_SECONDS
    assert seen["save-buffer"] == terminal._BUFFER_CLEANUP_TIMEOUT_SECONDS
    assert tmux.verbs().count("load-buffer") == 1


def test_timeouts_are_finite_and_bounded():
    for v in (
        terminal._BUFFER_LOAD_TIMEOUT_SECONDS,
        terminal._BUFFER_PASTE_TIMEOUT_SECONDS,
        terminal._BUFFER_CLEANUP_TIMEOUT_SECONDS,
    ):
        assert 0 < v <= 15


def test_r06_r21_cleanup_failure_keeps_original_error(tmux, caplog):
    tmux.paste_targets_missing = True
    tmux.fail["save-buffer"] = ("timeout", 0, "")
    with caplog.at_level(logging.WARNING), pytest.raises(terminal.TerminalError, match="target pane is unavailable") as ei:
        terminal._send_text_via_stdin("s", SECRET)
    _assert_safe_error(ei.value)
    assert "residual buffer may remain" in caplog.text and SECRET not in caplog.text
    assert tmux.verbs().count("paste-buffer") == 1


def test_r20_unclassifiable_paste_error_uses_only_guarded_cleanup(tmux):
    tmux.buffers["nlm-other"] = b"o"
    tmux.fail["paste-buffer"] = ("rc", 7, f"weird {SECRET}")
    with pytest.raises(terminal.TerminalError, match="paste-buffer failed") as ei:
        terminal._send_text_via_stdin("s", "x")
    _assert_safe_error(ei.value)
    assert "nlm-other" in tmux.buffers
    cleanup = tmux.log[-1]
    assert cleanup[0] == "save-buffer" and cleanup[3] == "/dev/null" and cleanup[4] == ";" and cleanup[5] == "delete-buffer"


def test_r04_cleanup_of_missing_buffer_does_not_touch_others(tmux):
    tmux.buffers["nlm-other"] = b"o"
    assert terminal._cleanup_buffer("nlm-missing") == terminal.BUFFER_ALREADY_ABSENT
    assert tmux.buffers == {"nlm-other": b"o"}


def test_r05_buffer_vanishing_before_cleanup_is_safe(tmux):
    tmux.buffers["nlm-other"] = b"o"
    tmux.paste_targets_missing = True

    def vanish(cmd):
        tmux.buffers.pop(cmd[cmd.index("-b") + 1], None)

    tmux.hooks["save-buffer"] = vanish
    with pytest.raises(terminal.TerminalError):
        terminal._send_text_via_stdin("s", "x")
    assert "nlm-other" in tmux.buffers


def test_r24_r25_cleanup_classification(tmux):
    tmux.buffers["nlm-x"] = b"1"
    assert terminal._cleanup_buffer("nlm-x") == terminal.BUFFER_CLEANED
    assert terminal._cleanup_buffer("nlm-x") == terminal.BUFFER_ALREADY_ABSENT
    tmux.buffers["nlm-keep"] = b"k"
    tmux.fail["save-buffer"] = ("rc", 1, "something else entirely")
    assert terminal._cleanup_buffer("nlm-y") == terminal.BUFFER_CLEANUP_FAILED
    tmux.fail["save-buffer"] = ("rc", 1, "prefix no buffer nlm-y")  # no fuzzy substring match
    assert terminal._cleanup_buffer("nlm-y") == terminal.BUFFER_CLEANUP_FAILED
    tmux.fail["save-buffer"] = ("oserror", 0, "")
    assert terminal._cleanup_buffer("nlm-y") == terminal.BUFFER_CLEANUP_FAILED
    assert "nlm-keep" in tmux.buffers
    assert "delete-buffer" not in [c[0] for c in tmux.log if c[0] != "save-buffer"]


# ---- names ----


def test_r10_r11_buffer_names_are_unique_and_opaque(monkeypatch):
    names = {terminal._new_buffer_name() for _ in range(5000)}
    assert len(names) == 5000
    assert all(re.fullmatch(r"nlm-\d+-[0-9a-f]{32}", n) for n in names)
    # a different process id still yields distinct names (pid is part of the name)
    monkeypatch.setattr(terminal.os, "getpid", lambda: 1)
    assert terminal._new_buffer_name().startswith("nlm-1-")


def test_each_send_uses_one_consistent_own_name(tmux):
    terminal._send_text_via_stdin("sess-name", SECRET)
    terminal._send_text_via_stdin("sess-name", SECRET)
    names = [c[c.index("-b") + 1] for c in tmux.log if c[0] == "load-buffer"]
    pasted = [c[c.index("-b") + 1] for c in tmux.log if c[0] == "paste-buffer"]
    assert names == pasted and len(set(names)) == 2
    assert all("sess-name" not in n and SECRET not in n for n in names)


def test_r18_no_fixed_name_or_bare_delete_buffer_in_source():
    src = Path(terminal.__file__).read_text()
    assert 'f"text-{' not in src and 'f"secret-{' not in src
    code = [ln for ln in src.splitlines() if not ln.strip().startswith(("#",))]
    uses = [i for i, ln in enumerate(code) if '"delete-buffer"' in ln]
    assert len(uses) == 1 and '"save-buffer"' in code[uses[0]] and '";"' in code[uses[0]]
    assert "-d" in src and "shell=True" not in src


# ---- UTF-8 and exception hygiene ----


def test_r12_r13_unencodable_input_fails_before_any_tmux_call(tmux, caplog):
    bad = "DUMMY_SECRET_PREFIX" + "\ud800" + "DUMMY_SECRET_SUFFIX"
    with caplog.at_level(logging.DEBUG), pytest.raises(terminal.TerminalError) as ei:
        terminal._send_text_via_stdin("s", bad)
    assert tmux.log == []
    assert str(ei.value) == "Input cannot be encoded as UTF-8"
    assert ei.value.__cause__ is None and ei.value.__context__ is None
    import traceback

    rendered = "".join(traceback.format_exception(ei.value)) + repr(ei.value) + caplog.text
    for frag in ("DUMMY_SECRET", "ud800", "\\ud800", "19", "20", "21"):
        if frag.startswith(("DUMMY", "ud800", "\\u")):
            assert frag not in rendered


@pytest.mark.parametrize("kind", ["timeout", "oserror", "calledprocess"])
@pytest.mark.parametrize("verb", ["load-buffer", "paste-buffer"])
def test_r14_r15_r17_external_exceptions_do_not_leak_or_chain(tmux, kind, verb):
    tmux.fail[verb] = (kind, 0, "")
    with pytest.raises(terminal.TerminalError) as ei:
        terminal._send_text_via_stdin("s", SECRET)
    _assert_safe_error(ei.value)


def test_r16_stderr_with_secret_is_not_reflected(tmux):
    for verb in ("load-buffer", "paste-buffer"):
        tmux.fail.clear()
        tmux.fail[verb] = ("rc", 1, f"echo {SECRET}")
        with pytest.raises(terminal.TerminalError) as ei:
            terminal._send_text_via_stdin("s", SECRET)
        _assert_safe_error(ei.value)


def test_argv_has_no_payload_and_stdin_is_exact_utf8(tmux):
    payload = "日本語\nline2\n🚀"
    terminal._send_text_via_stdin("s", payload)
    assert tmux.pasted == [payload.encode("utf-8")]
    assert all(payload not in " ".join(c) for c in tmux.log)
    assert "-r" in next(c for c in tmux.log if c[0] == "paste-buffer")


# ---- R22: session independence, no global lock ----


def test_r22_timeout_on_one_session_does_not_block_another(tmux):
    def maybe_timeout(cmd):
        if cmd[cmd.index("-t") + 1] == "%bad":
            raise subprocess.TimeoutExpired(cmd, 1)

    tmux.hooks["paste-buffer"] = maybe_timeout
    import unittest.mock as m

    with m.patch.object(terminal, "_send_target", lambda name: "%bad" if name == "a" else "%1"):
        with pytest.raises(terminal.TerminalError):
            terminal._send_text_via_stdin("a", "x")
        terminal._send_text_via_stdin("b", "y")
    assert tmux.pasted == [b"y"]
    assert terminal._session_lock("a") is not terminal._session_lock("b")


# ---- R26: model of the known old-process-mixing limitation (NOT real-tmux evidence) ----


def test_r26_model_legacy_process_can_break_guarded_cleanup_ownership(tmux):
    """If save-buffer ever suspended the command queue, a legacy process's
    unconditional delete-buffer could remove our buffer in between, making
    our delete-buffer fall back to another worker's newest buffer. The Fake
    injects that order; real tmux has NOT been shown to do this. Hence the
    deployment rule: never run legacy and fixed processes on one tmux server."""
    tmux.buffers["nlm-mine"], tmux.buffers["nlm-victim"] = b"m", b"v"

    def legacy_delete_between(cmd):  # runs before our delete-buffer
        if cmd[0] == "delete-buffer":
            tmux._exec(["delete-buffer", "-b", "legacy-missing"], None)  # falls back to newest

    tmux.hooks["delete-buffer"] = legacy_delete_between
    tmux.hooks["save-buffer"] = lambda cmd: tmux.buffers.pop("nlm-victim", None) and tmux.buffers.update({"nlm-victim": b"v"})
    tmux.run(["tmux", "-L", "x", "save-buffer", "-b", "nlm-mine", "/dev/null", ";", "delete-buffer", "-b", "nlm-mine"])
    assert "nlm-victim" not in tmux.buffers  # the modeled hazard
