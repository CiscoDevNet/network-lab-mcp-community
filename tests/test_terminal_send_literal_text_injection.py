# SPDX-License-Identifier: Apache-2.0
"""terminal_send(text=...) / `_send_literal_text()`: byte-fidelity
characterization, argv/error-message safety, and ordering.

Investigation found the ordinary (non-credential) text path,
`_send_literal_text()`, still used `tmux send-keys -t <session> -l --
<text>` -- placing operator-supplied text directly in that `tmux`
subprocess's own argv for as long as it runs, and (via `_run()`'s generic
`f"tmux command failed: {' '.join(args)}: ..."` error formatting)
potentially into a raised TerminalError's message if that specific
command ever failed. Since `terminal_send(text=...)` text can legitimately
be a username, password, enable secret, or other private configuration
value being typed into a device, this is the same class of exposure the
credential-only `_send_secret_text()` fix already closed for private
authentication -- this file closes it for the public path too, via the
same underlying stdin-based `tmux load-buffer` / `paste-buffer` transport
(`_send_text_via_stdin()`, shared by both).

Unlike the credential fix, this path's existing behavioral CONTRACT must
be preserved exactly: `send-keys -l --` is a raw literal-byte pass-through
(confirmed empirically against a real tmux session before any code
change: an isolated validation session running a raw-tty-mode stdin
capture script, so what's asserted here is the literal bytes tmux
delivers to the child process, never `capture-pane`'s VT100-rendered
text, which cannot distinguish an embedded LF from an embedded CR).
`tmux paste-buffer`'s own default behavior (replacing every LF with a CR
separator) would have silently changed that contract, so the fix
specifically adds `-r` ("no replacement") to preserve it -- this file's
byte-fidelity matrix is what proves `-r` was the right choice and that no
other paste-buffer default (bracketed paste, etc.) altered behavior."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from network_lab_mcp import terminal

_SENTINEL = "SENTINEL_LITERAL_TEXT_ARGV_SAFETY_XYZ"
_CAPTURE_SCRIPT = Path(__file__).parent / "fixtures" / "raw_stdin_capture.py"


@pytest.fixture(autouse=True)
def _cleanup_validation_sessions():
    yield
    for session_name in list(terminal.list_validation_sessions()):
        validation_id = session_name[len(terminal.VALIDATION_PREFIX) :]
        terminal.close_validation_session(validation_id)


def _send_and_capture_raw_bytes(validation_id: str, payload: str, tmp_path: Path) -> bytes:
    """Open a validation session whose pane process puts its own stdin into
    raw tty mode and records every byte it receives, send `payload` via the
    real `_send_literal_text()`, then read back exactly what arrived."""
    out_path = tmp_path / f"{validation_id}.bin"
    terminal.open_validation_session(
        validation_id, ["python3", str(_CAPTURE_SCRIPT), str(out_path), "1.0"]
    )
    session_name = terminal.derive_validation_session_name(validation_id)

    terminal._send_literal_text(session_name, payload)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if out_path.exists() and out_path.stat().st_size >= len(payload.encode("utf-8")):
            break
        time.sleep(0.1)
    else:
        time.sleep(1.3)  # let the capture script's own idle timeout flush what it has
    return out_path.read_bytes() if out_path.exists() else b""


# ---- byte-fidelity characterization: same contract before and after the
# stdin-based rewrite ----


@pytest.mark.parametrize(
    "case_id,payload",
    [
        ("backslash", r"a\b"),
        ("dollar", "a$b"),
        ("semicolon", "a;b"),
        ("single_quote", "a'b"),
        ("double_quote", 'a"b'),
        ("exclamation", "a!b"),
        ("pipe", "a|b"),
        ("ampersand", "a&b"),
        ("parens", "a(b)c"),
        ("spaces", "a  b   c"),
        ("leading_trailing_space", " a b "),
        ("mixed_special", "a\\$b;c'd\"e!f|g&h(i)j k"),
        ("utf8_accents", "héllo wörld"),
        ("utf8_cjk", "日本語"),
        ("utf8_emoji", "rocket:🚀"),
        ("embedded_lf", "line1\nline2"),
        ("embedded_cr", "line1\rline2"),
        ("embedded_tab", "col1\tcol2"),
        ("mixed_lf_cr_tab", "a\nb\rc\td"),
    ],
)
def test_send_literal_text_delivers_exact_bytes(case_id, payload, tmp_path):
    captured = _send_and_capture_raw_bytes(f"charmatrix-{case_id}", payload, tmp_path)
    assert captured == payload.encode("utf-8")


# ---- argv safety (mirrors the existing credential test) ----


def _spy_on_subprocess_run(monkeypatch):
    real_run = subprocess.run
    calls: list[tuple[list[str], object]] = []

    def spy(args, **kwargs):
        calls.append((list(args), kwargs.get("input")))
        return real_run(args, **kwargs)

    monkeypatch.setattr(terminal.subprocess, "run", spy)
    return calls


def test_send_literal_text_never_places_text_in_subprocess_argv(monkeypatch):
    calls = _spy_on_subprocess_run(monkeypatch)
    terminal.open_validation_session("argvtest", ["bash", "-c", "cat"])
    session_name = terminal.derive_validation_session_name("argvtest")

    terminal._send_literal_text(session_name, _SENTINEL)

    assert calls, "expected at least one subprocess.run call"
    for args, _stdin_input in calls:
        assert _SENTINEL not in " ".join(args), f"text leaked into subprocess argv: {args}"
    # Confirm the text *did* travel, just via stdin -- a no-op fake must not
    # trivially satisfy the check above.
    assert any(stdin_input == _SENTINEL for _, stdin_input in calls)


def test_send_literal_text_still_reaches_the_pane(monkeypatch):
    terminal.open_validation_session("argvtest2", ["bash", "-c", "cat"])
    session_name = terminal.derive_validation_session_name("argvtest2")

    terminal._send_literal_text(session_name, _SENTINEL)
    terminal._send_enter(session_name)
    time.sleep(0.3)

    pane = terminal._capture_pane(session_name, terminal.HISTORY_LIMIT)
    assert _SENTINEL in pane


def test_send_literal_text_cleans_up_its_named_buffer():
    terminal.open_validation_session("argvtest3", ["bash", "-c", "cat"])
    session_name = terminal.derive_validation_session_name("argvtest3")

    terminal._send_literal_text(session_name, _SENTINEL)

    result = subprocess.run(terminal._tmux_base() + ["list-buffers"], capture_output=True, text=True)
    assert session_name not in result.stdout


# ---- error-message safety ----


def test_send_literal_text_error_message_excludes_text_on_tmux_failure(monkeypatch):
    """If the underlying tmux command fails, the raised TerminalError must
    not include the text being sent -- confirming the fix isn't merely
    "safe on the happy path" (the old send-keys implementation's failure
    message, via _run()'s generic `' '.join(args)` formatting, would have
    included it, since `text` was itself one of that command's args)."""
    terminal.open_validation_session("errtest", ["bash", "-c", "cat"])
    session_name = terminal.derive_validation_session_name("errtest")
    real_run = subprocess.run

    def failing_run(args, **kwargs):
        if "paste-buffer" in args:
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="synthetic failure for test")
        return real_run(args, **kwargs)

    monkeypatch.setattr(terminal.subprocess, "run", failing_run)

    with pytest.raises(terminal.TerminalError) as exc_info:
        terminal._send_literal_text(session_name, _SENTINEL)

    assert _SENTINEL not in str(exc_info.value)


# ---- ordering: text -> keys -> Enter, unchanged ----


def test_send_to_device_preserves_text_then_keys_then_enter_order(monkeypatch):
    calls: list[str] = []
    real_literal = terminal._send_literal_text
    real_keys = terminal._send_special_keys
    real_enter = terminal._send_enter

    def tracked_literal(session_name, text):
        calls.append("text")
        return real_literal(session_name, text)

    def tracked_keys(session_name, keys):
        calls.append("keys")
        return real_keys(session_name, keys)

    def tracked_enter(session_name):
        calls.append("enter")
        return real_enter(session_name)

    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["bash", "-c", "cat"]))
    monkeypatch.setattr(terminal, "_authenticate_managed_session", lambda *a, **k: None)

    # Session bootstrap (_create_logged_session) itself types the launch
    # command into a pre-logging shell via _send_literal_text()/_send_enter()
    # -- unrelated to the text/keys/Enter ordering under test, so only start
    # tracking once the session already exists.
    terminal.open_device_terminal("ORDERTEST", {"transport": "ssh", "address": "192.0.2.1"})
    monkeypatch.setattr(terminal, "_send_literal_text", tracked_literal)
    monkeypatch.setattr(terminal, "_send_special_keys", tracked_keys)
    monkeypatch.setattr(terminal, "_send_enter", tracked_enter)
    try:
        terminal.send_to_device("ORDERTEST", "show version", ["Tab"], True)
    finally:
        terminal.close_device_terminal("ORDERTEST")

    assert calls == ["text", "keys", "enter"]
