# SPDX-License-Identifier: Apache-2.0
"""`delete logging all` / `delete logging <device> all` / `delete logging
<device> <log-file>` / `delete logging <device> directory` / `delete
logging all directory`: EXEC-only, destructive terminal-log deletion.
Eligibility is derived by reusing the exact same
stored-log enumeration `show logging` uses
(terminal.list_logged_device_ids() / terminal.list_device_logs()), so
what is deletable never drifts from what is displayed.

Every destructive form requires mandatory [y/N] confirmation, a
confirm-then-re-preflight-then-apply flow (terminal.DeletionPlan /
build_*_deletion_plan() / apply_deletion_plan()) that aborts safely if
the target state changed while the user was deciding, and two
directory-cleanup commands that remove a now-empty device logging
directory (never recursively) after its eligible logs are deleted.

The central safety invariant is device-level
active-writer protection: a production session and a Discovery bootstrap
session both attach persistent pipe-pane logging to the same
logs/terminal/<device-id>/ directory, keyed only by device name -- so an
active writer for a device blocks deletion of ALL of that device's logs
(and its directory), not a guessed "active" one. Real tmux sessions
(mocked to a safe local command, never ssh/telnet) are used for the
active-writer tests so the actual detection mechanism is genuinely
exercised.

`terminal.LOGS_ROOT` is monkeypatched to an isolated tmp_path directory in
every test here -- the `isolated_logs` fixture itself asserts this
isolation before returning, so no destructive operation in this file can
reach the real repository's logs/terminal/. Confirmation is simulated by
monkeypatching `cli.main._read_confirmation_line()` (never a real
interactive stdin), so no test here can ever accept a real destructive
prompt."""

from __future__ import annotations

import time

import pytest

from network_lab_mcp import terminal
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain

_REAL_REPO_LOGS_ROOT = terminal.LOGS_ROOT  # captured once, before any monkeypatch in this file


@pytest.fixture()
def isolated_logs(tmp_path, monkeypatch):
    logs_root = tmp_path / "logs" / "terminal"
    monkeypatch.setattr(terminal, "LOGS_ROOT", logs_root)
    # A prior real-lab incident happened because an assumed redirection
    # did not actually take effect. Prove it here, explicitly, before any
    # destructive operation below can run.
    assert terminal.LOGS_ROOT == logs_root
    assert terminal.LOGS_ROOT != _REAL_REPO_LOGS_ROOT
    assert tmp_path in terminal.LOGS_ROOT.parents
    assert _REAL_REPO_LOGS_ROOT not in terminal.LOGS_ROOT.parents
    return logs_root


def _write_log(logs_root, device_id, timestamp, content=""):
    device_dir = logs_root / device_id
    device_dir.mkdir(parents=True, exist_ok=True)
    (device_dir / f"{timestamp}.log").write_text(content)


def _wait_until(predicate, timeout=3.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# Fixed device names reserved for real-tmux active-writer tests in this
# file only (never R1/R2/... used by the synthetic-file tests, so a
# leaked session from a failed test can never be mistaken for synthetic
# fixture data).
_WRITER_TEST_DEVICE_IDS = ("DW1", "DW2", "DW3")


@pytest.fixture(autouse=True)
def _cleanup_sessions():
    yield
    for session_name in list(terminal.list_validation_sessions()):
        terminal.close_validation_session(session_name[len(terminal.VALIDATION_PREFIX) :])


@pytest.fixture(autouse=True)
def _no_managed_auth_wait(monkeypatch):
    """This file exercises logging/active-writer safety, never SSH
    authentication -- `open_device_terminal(..., {"transport": "ssh", ...})`
    below is only ever a convenient stand-in for "some active managed
    session", against a fake, non-routable address (the real
    authentication wait would otherwise poll for the bounded
    _MANAGED_LOGIN_TIMEOUT_SECONDS on every such call for nothing)."""
    monkeypatch.setattr(terminal, "_authenticate_managed_session", lambda *a, **k: None)
    for device_id in _WRITER_TEST_DEVICE_IDS:
        terminal.close_device_terminal(device_id)
        terminal.close_bootstrap_terminal(device_id)


def _fake_transport(monkeypatch):
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["cat"]))


def _first_log_filename(logs_root, device_id):
    device_dir = logs_root / device_id
    assert _wait_until(lambda: device_dir.is_dir() and any(device_dir.iterdir()))
    return next(iter(device_dir.iterdir())).name


def _answer(monkeypatch, *answers):
    """Feed a fixed sequence of confirmation answers to
    climain._read_confirmation_line(), one per call -- simulates typing
    each answer at the [y/N] prompt without a real interactive stdin.
    Exhausting the sequence raises StopIteration, so a test that
    under-specifies answers fails loudly instead of hanging on real
    stdin. The prompt itself is printed separately by _confirm_delete()
    (via plain print()), so it is still observable in captured stdout
    even though this mock never touches real input()."""
    it = iter(answers)
    monkeypatch.setattr(climain, "_read_confirmation_line", lambda: next(it))


# ==========================================================================
# Grammar: `delete` (EXEC only), `delete logging`, `all`/`directory`
# literals combined with dynamic providers, `?`, Tab, executable
# endpoints
# ==========================================================================


def test_delete_parses_only_in_exec():
    # Grammar reachability is independent of filesystem state -- parsing
    # succeeds even with nothing eligible to delete yet; the handler is
    # what actually enumerates/rejects at execution time (see the
    # empty-state tests below).
    result = grammar.parse("exec", "delete logging all")
    assert result.ok
    assert result.action == "exec.delete_logging_all"
    for mode in ("global", "running", "topology", "device", "access_info", "access_device", "scenario", "reference"):
        result = grammar.parse(mode, "delete")
        assert not result.ok, mode
        assert result.error.kind == "unknown"


def test_delete_bare_help_lists_logging_no_cr():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "delete ", ctx)
    assert [line.token for line in result.lines] == ["logging"]
    assert result.show_cr is False


def test_delete_inline_help_no_cr():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "delete", ctx)
    assert [(line.token, line.description) for line in result.lines] == [("delete", "Delete stored information")]
    assert result.show_cr is False


def test_delete_logging_inline_help_no_cr():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "delete logging", ctx)
    assert [(line.token, line.description) for line in result.lines] == [("logging", "Delete terminal logs")]
    assert result.show_cr is False


def test_delete_logging_bare_not_executable():
    result = grammar.parse("exec", "delete logging")
    assert not result.ok
    assert result.error.kind == "incomplete"


def test_delete_logging_device_bare_not_executable():
    result = grammar.parse("exec", "delete logging R1")
    assert not result.ok
    assert result.error.kind == "incomplete"


def test_delete_logging_all_parses():
    result = grammar.parse("exec", "delete logging all")
    assert result.ok
    assert result.action == "exec.delete_logging_all"
    assert result.args == {}


def test_delete_logging_all_directory_parses():
    result = grammar.parse("exec", "delete logging all directory")
    assert result.ok
    assert result.action == "exec.delete_logging_all_directory"
    assert result.args == {}


def test_delete_logging_device_all_parses():
    result = grammar.parse("exec", "delete logging R1 all")
    assert result.ok
    assert result.action == "exec.delete_logging_device_all"
    assert result.args == {"device_id": "R1"}


def test_delete_logging_device_directory_parses():
    result = grammar.parse("exec", "delete logging R1 directory")
    assert result.ok
    assert result.action == "exec.delete_logging_device_directory"
    assert result.args == {"device_id": "R1"}


def test_delete_logging_device_file_parses():
    result = grammar.parse("exec", "delete logging R1 20260921T091500.log")
    assert result.ok
    assert result.action == "exec.delete_logging_device_file"
    assert result.args == {"device_id": "R1", "log_file": "20260921T091500.log"}


def test_delete_logging_bare_help_lists_all_and_known_devices():
    ctx = grammar.CliContext(log_device_ids=("R1", "R2"))
    result = grammar.help("exec", "delete logging ", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("all", "Delete all terminal logs"),
        ("R1", "Device logging directory"),
        ("R2", "Device logging directory"),
    ]
    assert result.show_cr is False


def test_delete_logging_all_inline_help_shows_cr():
    ctx = grammar.CliContext(log_device_ids=("R1",))
    result = grammar.help("exec", "delete logging all", ctx)
    assert [(line.token, line.description) for line in result.lines] == [("all", "Delete all terminal logs")]
    assert result.show_cr is True


def test_delete_logging_all_spaced_help_shows_directory_and_cr():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "delete logging all ", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("directory", "Delete all terminal logs and device log directories")
    ]
    assert result.show_cr is True


def test_delete_logging_all_directory_inline_help_shows_cr():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "delete logging all directory", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("directory", "Delete all terminal logs and device log directories")
    ]
    assert result.show_cr is True


def test_delete_logging_all_directory_spaced_help_is_cr_only():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "delete logging all directory ", ctx)
    assert result.lines == []
    assert result.show_cr is True


def test_delete_logging_device_exact_inline_help_no_cr():
    ctx = grammar.CliContext(log_device_ids=("R1", "R2"))
    result = grammar.help("exec", "delete logging R1", ctx)
    assert [(line.token, line.description) for line in result.lines] == [("R1", "Device logging directory")]
    assert result.show_cr is False  # bare "delete logging R1" is intentionally incomplete


def test_delete_logging_device_partial_help_has_no_cr():
    ctx = grammar.CliContext(log_device_ids=("R10",))
    result = grammar.help("exec", "delete logging R1", ctx)
    assert [line.token for line in result.lines] == ["R10"]
    assert result.show_cr is False


def test_delete_logging_unknown_device_inline_help_empty():
    ctx = grammar.CliContext(log_device_ids=("R1",))
    result = grammar.help("exec", "delete logging R9", ctx)
    assert result.lines == []
    assert result.show_cr is False


def test_delete_logging_device_spaced_help_lists_all_directory_and_files():
    ctx = grammar.CliContext(log_files_by_device={"R1": ("20260921T091500.log", "20260921T103210.log")})
    result = grammar.help("exec", "delete logging R1 ", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("all", "Delete all terminal logs for this device"),
        ("directory", "Delete terminal logs and device log directory"),
        ("20260921T091500.log", "Terminal log"),
        ("20260921T103210.log", "Terminal log"),
    ]
    assert result.show_cr is False


def test_delete_logging_device_spaced_help_lists_all_and_directory_when_empty():
    ctx = grammar.CliContext(log_device_ids=("SW2",), log_files_by_device={})
    result = grammar.help("exec", "delete logging SW2 ", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("all", "Delete all terminal logs for this device"),
        ("directory", "Delete terminal logs and device log directory"),
    ]


def test_delete_logging_device_all_inline_help_shows_cr():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "delete logging R1 all", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("all", "Delete all terminal logs for this device")
    ]
    assert result.show_cr is True


def test_delete_logging_device_directory_inline_help_shows_cr():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "delete logging R1 directory", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("directory", "Delete terminal logs and device log directory")
    ]
    assert result.show_cr is True


def test_delete_logging_device_directory_partial_resolves():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "delete logging R1 dir", ctx)
    assert [line.token for line in result.lines] == ["directory"]
    assert result.show_cr is False


def test_delete_logging_device_file_exact_inline_help_shows_cr():
    ctx = grammar.CliContext(log_files_by_device={"R1": ("20260921T091500.log",)})
    result = grammar.help("exec", "delete logging R1 20260921T091500.log", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("20260921T091500.log", "Terminal log")
    ]
    assert result.show_cr is True


def test_delete_logging_device_file_spaced_help_is_cr_only():
    ctx = grammar.CliContext(log_files_by_device={"R1": ("20260921T091500.log",)})
    result = grammar.help("exec", "delete logging R1 20260921T091500.log ", ctx)
    assert result.lines == []
    assert result.show_cr is True


def test_delete_logging_tab_completion_devices_and_all():
    ctx = grammar.CliContext(log_device_ids=("R1", "R2"))
    candidates = grammar.complete("exec", "delete logging ", ctx).candidates
    assert set(candidates) == {"all", "R1", "R2"}


def test_delete_logging_device_tab_completion_files_all_and_directory():
    ctx = grammar.CliContext(log_files_by_device={"R1": ("20260921T091500.log",)})
    candidates = grammar.complete("exec", "delete logging R1 ", ctx).candidates
    assert set(candidates) == {"all", "directory", "20260921T091500.log"}


def test_delete_logging_all_tab_completion_includes_directory():
    ctx = grammar.CliContext()
    candidates = grammar.complete("exec", "delete logging all ", ctx).candidates
    assert candidates == ["directory"]


def test_delete_unknown_device_or_file_does_not_wildcard_match():
    ctx = grammar.CliContext(log_device_ids=("R1",), log_files_by_device={"R1": ("20260921T091500.log",)})
    result = grammar.help("exec", "delete logging R9", ctx)
    assert result.lines == []
    result2 = grammar.help("exec", "delete logging R1 does-not-exist.log", ctx)
    assert result2.lines == []


def test_delete_keyword_no_new_abbreviation_ambiguity():
    result = grammar.parse("exec", "d logging all")
    assert result.ok
    assert result.action == "exec.delete_logging_all"
    ctx = grammar.CliContext()
    tokens = [line.token for line in grammar.help("exec", "", ctx).lines]
    assert tokens == ["configure", "show", "delete", "terminal", "help", "exit", "quit"]


def test_directory_keyword_abbreviation_resolves():
    assert grammar.parse("exec", "delete logging all dir").action == "exec.delete_logging_all_directory"
    assert grammar.parse("exec", "delete logging R1 dir").action == "exec.delete_logging_device_directory"


def test_directory_keyword_cannot_collide_with_a_real_log_filename():
    """Eligible log filenames always match `<timestamp>[_<n>].log`
    (terminal._LOG_FILENAME_RE); "directory" cannot possibly match that
    pattern, so it can never be returned by provide_log_files() and can
    never collide with the fixed "directory" keyword."""
    assert terminal._LOG_FILENAME_RE.match("directory") is None


# ==========================================================================
# Single-file deletion, with confirmation
# ==========================================================================


def test_delete_single_log_file_confirmed(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _write_log(isolated_logs, "R1", "20260921T100000", "b")
    _write_log(isolated_logs, "R2", "20260921T110000", "c")
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 20260921T090000.log")
    out = capsys.readouterr().out
    assert "Delete terminal log R1/20260921T090000.log? [y/N]:" in out
    assert out.strip().endswith("Deleted terminal log R1/20260921T090000.log.")
    assert [name for _, name in terminal.list_device_logs("R1")] == ["20260921T100000.log"]
    assert [name for _, name in terminal.list_device_logs("R2")] == ["20260921T110000.log"]


def test_delete_single_log_file_declined_with_n(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _answer(monkeypatch, "n")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 20260921T090000.log")
    assert "Delete cancelled." in capsys.readouterr().out
    assert (isolated_logs / "R1" / "20260921T090000.log").exists()


def test_delete_single_log_file_declined_with_empty_enter(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _answer(monkeypatch, "")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 20260921T090000.log")
    assert "Delete cancelled." in capsys.readouterr().out
    assert (isolated_logs / "R1" / "20260921T090000.log").exists()


def test_delete_nonexistent_exact_filename_deletes_nothing_no_prompt(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 does-not-exist.log")
    out = capsys.readouterr().out
    assert out.startswith("%")
    assert "[y/N]" not in out  # no meaningless confirmation for an already-invalid request
    assert [name for _, name in terminal.list_device_logs("R1")] == ["20260921T090000.log"]


# ==========================================================================
# Confirmation semantics -- mandatory, exhaustive
# ==========================================================================


@pytest.mark.parametrize("answer", ["y", "Y"])
def test_confirmation_yes_variants_proceed(isolated_logs, lab_root, monkeypatch, capsys, answer):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _answer(monkeypatch, answer)
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 20260921T090000.log")
    assert not (isolated_logs / "R1" / "20260921T090000.log").exists()


@pytest.mark.parametrize("answer", ["n", "N", ""])
def test_confirmation_no_variants_cancel(isolated_logs, lab_root, monkeypatch, capsys, answer):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _answer(monkeypatch, answer)
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 20260921T090000.log")
    assert "Delete cancelled." in capsys.readouterr().out
    assert (isolated_logs / "R1" / "20260921T090000.log").exists()


def test_confirmation_invalid_input_reprompts_then_no_mutation_on_eventual_no(
    isolated_logs, lab_root, monkeypatch, capsys
):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _answer(monkeypatch, "yes", "maybe", "n")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 20260921T090000.log")
    out = capsys.readouterr().out
    assert out.count("Please enter y or n.") == 2
    assert "Delete cancelled." in out
    # Proves "yes"/"maybe" were never silently treated as confirmation:
    # the file still exists even though the eventual answer chain ended
    # in an explicit "n".
    assert (isolated_logs / "R1" / "20260921T090000.log").exists()


def test_confirmation_invalid_input_reprompts_then_confirms_on_eventual_yes(
    isolated_logs, lab_root, monkeypatch, capsys
):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _answer(monkeypatch, "yes", "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 20260921T090000.log")
    out = capsys.readouterr().out
    assert "Please enter y or n." in out
    assert not (isolated_logs / "R1" / "20260921T090000.log").exists()


def test_confirmation_ctrl_c_cancels_safely(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")

    def _raise_keyboard_interrupt():
        raise KeyboardInterrupt

    monkeypatch.setattr(climain, "_read_confirmation_line", _raise_keyboard_interrupt)
    session = cfgmod.CliSession(lab_root)
    ok = climain.execute_command_line(session, "delete logging R1 20260921T090000.log")
    assert ok is True  # Ctrl-C during confirmation is a cancel, not a command failure
    assert "Delete cancelled." in capsys.readouterr().out
    assert (isolated_logs / "R1" / "20260921T090000.log").exists()


def test_confirmation_eof_cancels_safely(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")

    def _raise_eof():
        raise EOFError

    monkeypatch.setattr(climain, "_read_confirmation_line", _raise_eof)
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 20260921T090000.log")
    assert "Delete cancelled." in capsys.readouterr().out
    assert (isolated_logs / "R1" / "20260921T090000.log").exists()


def test_confirmation_answer_never_enters_command_history(isolated_logs, lab_root, monkeypatch):
    """_read_confirmation_line() uses input(), never PromptSession's own
    history -- a confirmation answer is structurally incapable of being
    recorded by MaskingHistory, since MaskingHistory.append_string() is
    never called for it at all."""
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    history = climain.MaskingHistory()
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    history.append_string("delete logging R1 20260921T090000.log")
    climain.execute_command_line(session, "delete logging R1 20260921T090000.log")
    stored = history.get_strings()
    assert stored == ["delete logging R1 20260921T090000.log"]
    assert "y" not in stored


# ==========================================================================
# Multi-line paste: confirmation-requiring commands fail closed
# ==========================================================================


def test_delete_logging_inside_paste_fails_closed(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    session = cfgmod.CliSession(lab_root)
    climain.execute_input_block(session, "delete logging R1 20260921T090000.log\n")
    out = capsys.readouterr().out
    assert out.strip().startswith("%")
    assert "cannot be run from multi-line paste" in out
    assert (isolated_logs / "R1" / "20260921T090000.log").exists()


def test_single_line_delete_logging_remains_interactive(isolated_logs, lab_root, monkeypatch, capsys):
    """Sanity: only the multi-line paste path is affected -- a single
    manually-typed line (even routed through execute_input_block(), which
    has no embedded newline) still allows confirmation."""
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_input_block(session, "delete logging R1 20260921T090000.log")
    assert not (isolated_logs / "R1" / "20260921T090000.log").exists()


# ==========================================================================
# Device-all deletion
# ==========================================================================


def test_delete_all_logs_for_one_device(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _write_log(isolated_logs, "R1", "20260921T100000", "b")
    _write_log(isolated_logs, "R2", "20260921T110000", "c")
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 all")
    out = capsys.readouterr().out
    assert "Delete all 2 terminal logs for R1? [y/N]:" in out
    assert out.strip().endswith("Deleted 2 terminal logs for R1.")
    assert terminal.list_device_logs("R1") == []
    assert [name for _, name in terminal.list_device_logs("R2")] == ["20260921T110000.log"]
    assert (isolated_logs / "R1").is_dir()  # `all` never removes the directory


def test_delete_all_logs_for_one_device_singular_wording(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 all")
    out = capsys.readouterr().out
    assert "Delete all 1 terminal log for R1? [y/N]:" in out
    assert "Deleted 1 terminal log for R1." in out


def test_delete_all_for_empty_or_nonexistent_device_no_prompt(isolated_logs, lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R9 all")
    out = capsys.readouterr().out
    assert out.strip() == "% No terminal logs found for device 'R9'."
    assert "[y/N]" not in out
    assert not (isolated_logs / "R9").exists()


def test_delete_all_for_a_device_already_emptied_by_deletion(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 all")
    capsys.readouterr()
    climain.execute_command_line(session, "delete logging R1 all")  # retry: nothing left, no prompt
    assert capsys.readouterr().out.strip() == "% No terminal logs found for device 'R1'."


# ==========================================================================
# Global-all deletion
# ==========================================================================


def test_delete_all_logs_globally(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _write_log(isolated_logs, "R2", "20260921T100000", "b")
    _write_log(isolated_logs, "R2", "20260921T110000", "c")
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all")
    out = capsys.readouterr().out
    assert "Delete all 3 terminal logs? [y/N]:" in out
    assert out.strip().endswith("Deleted 3 terminal logs.")
    # Directory cleanup is explicitly out of scope for plain `all`.
    assert terminal.list_device_logs("R1") == []
    assert terminal.list_device_logs("R2") == []
    assert (isolated_logs / "R1").is_dir()
    assert (isolated_logs / "R2").is_dir()


def test_delete_all_logs_globally_with_zero_logs_is_safe(isolated_logs, lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all")
    out = capsys.readouterr().out
    assert out.strip() == "% No terminal logs found."
    assert "[y/N]" not in out


def test_delete_all_logs_globally_with_empty_dir_is_safe(isolated_logs, lab_root, capsys):
    isolated_logs.mkdir(parents=True)
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all")
    assert capsys.readouterr().out.strip() == "% No terminal logs found."


# ==========================================================================
# Path traversal / symlink / unknown-file safety
# ==========================================================================


def test_delete_logging_device_path_traversal_rejected(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    outside = isolated_logs.parent / "outside-device-dir.log"
    outside.write_text("do not delete me")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging ../outside all")
    assert capsys.readouterr().out.startswith("%")
    assert outside.exists()
    assert [name for _, name in terminal.list_device_logs("R1")] == ["20260921T090000.log"]


@pytest.mark.parametrize("traversal", ["../outside.log", "../../outside.log", "/etc/passwd"])
def test_delete_logging_filename_path_traversal_rejected(isolated_logs, lab_root, capsys, traversal, tmp_path):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    outside = tmp_path / "outside.log"
    outside.write_text("do not delete me")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, f"delete logging R1 {traversal}")
    assert capsys.readouterr().out.startswith("%")
    assert outside.exists()
    assert [name for _, name in terminal.list_device_logs("R1")] == ["20260921T090000.log"]


def test_delete_logging_rejects_symlink_and_leaves_target_untouched(isolated_logs, lab_root, capsys, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("precious")
    device_dir = isolated_logs / "R1"
    device_dir.mkdir(parents=True)
    symlink_path = device_dir / "20260921T090000.log"
    symlink_path.symlink_to(outside)

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 20260921T090000.log")
    assert capsys.readouterr().out.startswith("%")
    assert outside.read_text() == "precious"
    assert symlink_path.is_symlink()


def test_delete_all_device_logs_ignores_symlinked_entry(isolated_logs, lab_root, monkeypatch, capsys, tmp_path):
    outside = tmp_path / "outside2.txt"
    outside.write_text("precious2")
    device_dir = isolated_logs / "R1"
    device_dir.mkdir(parents=True)
    (device_dir / "20260921T090000.log").write_text("real log")
    (device_dir / "20260921T100000.log").symlink_to(outside)

    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 all")
    assert capsys.readouterr().out.strip().endswith("Deleted 1 terminal log for R1.")
    assert outside.read_text() == "precious2"
    assert (device_dir / "20260921T100000.log").is_symlink()
    assert not (device_dir / "20260921T090000.log").exists()


def test_delete_all_device_logs_preserves_unknown_files(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    device_dir = isolated_logs / "R1"
    (device_dir / "notes.txt").write_text("keep me")
    (device_dir / "subdirectory").mkdir()

    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 all")
    assert (device_dir / "notes.txt").read_text() == "keep me"
    assert (device_dir / "subdirectory").is_dir()
    assert not (device_dir / "20260921T090000.log").exists()


def test_delete_all_logs_globally_preserves_unrelated_files(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    (isolated_logs / "R1" / "unrelated.txt").write_text("keep me too")
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all")
    assert (isolated_logs / "R1" / "unrelated.txt").read_text() == "keep me too"


# ==========================================================================
# `show logging` bare (flat listing) vs. explicit
# `show logging summary` (per-device count table)
# ==========================================================================


def test_bare_show_logging_is_the_flat_per_file_listing(isolated_logs, lab_root, capsys):
    """Bare `show logging` must match the exact
    flat, per-file, newest-first listing --
    not the summary table, which lives at `show logging
    summary` instead."""
    _write_log(isolated_logs, "R1", "20260921T091500")
    _write_log(isolated_logs, "R2", "20260921T091505")
    _write_log(isolated_logs, "R1", "20260921T103210")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging")
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    data_lines = lines[2:]  # skip header + dashes
    assert [line.split()[0] for line in data_lines] == ["R1", "R2", "R1"]
    assert "20260921T103210.log" in data_lines[0]
    assert "20260921T091505.log" in data_lines[1]
    assert "20260921T091500.log" in data_lines[2]
    # Never the summary shape.
    assert "Total" not in out


def test_show_logging_summary_counts_and_total(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _write_log(isolated_logs, "R1", "20260921T100000", "b")
    _write_log(isolated_logs, "R2", "20260921T110000", "c")
    (isolated_logs / "SW2").mkdir(parents=True)  # empty, valid directory

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging summary")
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    # Semantic check, not exact spacing: one row per device with its
    # count, an empty device shown with 0, and a Total row.
    assert any(line.split()[:2] == ["R1", "2"] for line in lines)
    assert any(line.split()[:2] == ["R2", "1"] for line in lines)
    assert any(line.split()[:2] == ["SW2", "0"] for line in lines)
    assert any(line.split()[:2] == ["Total", "3"] for line in lines)


def test_show_logging_summary_excludes_unknown_entries_from_count(isolated_logs, lab_root, capsys):
    device_dir = isolated_logs / "R1"
    device_dir.mkdir(parents=True)
    (device_dir / "20260921T090000.log").write_text("a")
    (device_dir / "notes.txt").write_text("not a log")
    (device_dir / "nested").mkdir()

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging summary")
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert any(line.split()[:2] == ["R1", "1"] for line in lines)
    assert any(line.split()[:2] == ["Total", "1"] for line in lines)
    assert "notes.txt" not in out


def test_show_logging_summary_after_file_only_cleanup_shows_zero(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "SW2", "20260921T090000", "a")
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging SW2 all")
    capsys.readouterr()
    climain.execute_command_line(session, "show logging summary")
    lines = capsys.readouterr().out.strip().splitlines()
    assert any(line.split()[:2] == ["SW2", "0"] for line in lines)


def test_show_logging_summary_after_directory_cleanup_no_longer_lists_device(
    isolated_logs, lab_root, monkeypatch, capsys
):
    _write_log(isolated_logs, "SW2", "20260921T090000", "a")
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging SW2 directory")
    capsys.readouterr()
    climain.execute_command_line(session, "show logging summary")
    out = capsys.readouterr().out
    assert "SW2" not in out
    assert out.strip() == "No terminal logs found."


def test_show_logging_bare_empty_root_is_safe(isolated_logs, lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging")
    assert capsys.readouterr().out.strip() == "No terminal logs found."
    assert not isolated_logs.exists()  # `show logging` never creates directories


def test_show_logging_bare_missing_root_entirely_is_safe(isolated_logs, lab_root, capsys):
    assert not isolated_logs.exists()
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging")
    assert capsys.readouterr().out.strip() == "No terminal logs found."


def test_show_logging_summary_empty_root_is_safe(isolated_logs, lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging summary")
    assert capsys.readouterr().out.strip() == "No terminal logs found."
    assert not isolated_logs.exists()


def test_show_logging_summary_missing_root_entirely_is_safe(isolated_logs, lab_root, capsys):
    assert not isolated_logs.exists()
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging summary")
    assert capsys.readouterr().out.strip() == "No terminal logs found."


def test_dynamic_help_reflects_deletion_immediately(isolated_logs, lab_root, monkeypatch):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _write_log(isolated_logs, "R1", "20260921T100000", "b")
    session = cfgmod.CliSession(lab_root)
    ctx_before = climain.build_context(session)
    assert set(ctx_before.log_files_by_device.get("R1", ())) == {"20260921T090000.log", "20260921T100000.log"}

    _answer(monkeypatch, "y")
    climain.execute_command_line(session, "delete logging R1 20260921T090000.log")

    ctx_after = climain.build_context(session)
    assert set(ctx_after.log_files_by_device.get("R1", ())) == {"20260921T100000.log"}


def test_device_log_files_no_longer_advertised_once_all_are_gone(isolated_logs, lab_root, monkeypatch):
    """Once R1 has zero eligible log files, no
    filename is offered for it, but R1 itself remains discoverable (its
    directory persists) so `delete logging R1 directory` is reachable."""
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _write_log(isolated_logs, "R2", "20260921T100000", "b")
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 all")
    ctx = climain.build_context(session)
    assert ctx.log_files_by_device.get("R1", ()) == ()
    assert "R1" in ctx.log_device_ids
    assert set(ctx.log_files_by_device.get("R2", ())) == {"20260921T100000.log"}


def test_device_disappears_from_discovery_after_directory_cleanup(isolated_logs, lab_root, monkeypatch):
    """Directory cleanup removes the device's log directory entirely."""
    _write_log(isolated_logs, "SW2", "20260921T090000", "a")
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging SW2 directory")
    ctx = climain.build_context(session)
    assert "SW2" not in ctx.log_device_ids


# ==========================================================================
# Device-directory deletion
# ==========================================================================


def test_delete_empty_device_directory(isolated_logs, lab_root, monkeypatch, capsys):
    (isolated_logs / "SW2").mkdir(parents=True)
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging SW2 directory")
    out = capsys.readouterr().out
    assert "Delete empty logging directory for SW2? [y/N]:" in out
    assert out.strip().endswith("Deleted logging directory for SW2.")
    assert not (isolated_logs / "SW2").exists()
    assert isolated_logs.is_dir()  # LOGS_ROOT itself remains


def test_delete_nonempty_device_directory(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "SW2", "20260921T090000", "a")
    _write_log(isolated_logs, "SW2", "20260921T100000", "b")
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging SW2 directory")
    out = capsys.readouterr().out
    assert "Delete all 2 terminal logs and logging directory for SW2? [y/N]:" in out
    assert out.strip().endswith("Deleted 2 terminal logs and logging directory for SW2.")
    assert not (isolated_logs / "SW2").exists()
    assert isolated_logs.is_dir()


def test_delete_device_directory_singular_wording(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "SW2", "20260921T090000", "a")
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging SW2 directory")
    out = capsys.readouterr().out
    assert "Delete all 1 terminal log and logging directory for SW2? [y/N]:" in out
    assert "Deleted 1 terminal log and logging directory for SW2." in out


def test_delete_device_directory_declined_preserves_everything(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "SW2", "20260921T090000", "a")
    _answer(monkeypatch, "n")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging SW2 directory")
    assert "Delete cancelled." in capsys.readouterr().out
    assert (isolated_logs / "SW2" / "20260921T090000.log").exists()


def test_delete_device_directory_nonexistent_device(isolated_logs, lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R9 directory")
    out = capsys.readouterr().out
    assert out.startswith("%")
    assert "[y/N]" not in out


@pytest.mark.parametrize(
    "make_unsafe_entry",
    [
        lambda d: (d / "notes.txt").write_text("not a log"),
        lambda d: (d / "arbitrary.bin").write_bytes(b"\x00\x01"),
        lambda d: (d / "nested").mkdir(),
    ],
    ids=["unknown-file", "unknown-binary", "nested-directory"],
)
def test_device_directory_deletion_blocked_by_unsafe_entry(
    isolated_logs, lab_root, capsys, make_unsafe_entry
):
    device_dir = isolated_logs / "SW2"
    device_dir.mkdir(parents=True)
    (device_dir / "20260921T090000.log").write_text("a")
    make_unsafe_entry(device_dir)

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging SW2 directory")
    out = capsys.readouterr().out
    assert out.startswith("%")
    assert "[y/N]" not in out  # blocked before confirmation
    assert (device_dir / "20260921T090000.log").exists()
    assert device_dir.is_dir()


def test_device_directory_deletion_blocked_by_symlink_log_named_entry(isolated_logs, lab_root, capsys, tmp_path):
    outside = tmp_path / "important.txt"
    outside.write_text("do not touch")
    device_dir = isolated_logs / "SW2"
    device_dir.mkdir(parents=True)
    (device_dir / "20260921T090000.log").write_text("a")
    (device_dir / "fake.log").symlink_to(outside)

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging SW2 directory")
    out = capsys.readouterr().out
    assert out.startswith("%")
    assert "[y/N]" not in out
    assert outside.read_text() == "do not touch"
    assert (device_dir / "20260921T090000.log").exists()
    assert device_dir.is_dir()


def test_symlink_device_directory_is_never_eligible(isolated_logs, lab_root, capsys, tmp_path):
    real_dir = tmp_path / "real_sw2_data"
    real_dir.mkdir()
    (real_dir / "20260921T090000.log").write_text("a")
    isolated_logs.mkdir(parents=True)
    (isolated_logs / "SW2").symlink_to(real_dir)

    session = cfgmod.CliSession(lab_root)
    ctx = climain.build_context(session)
    assert "SW2" not in ctx.log_device_ids

    climain.execute_command_line(session, "delete logging SW2 directory")
    out = capsys.readouterr().out
    assert out.startswith("%")
    assert real_dir.exists()
    assert (real_dir / "20260921T090000.log").exists()
    assert (isolated_logs / "SW2").is_symlink()


# ==========================================================================
# Global directory deletion
# ==========================================================================


def test_delete_all_directories_and_logs(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _write_log(isolated_logs, "R1", "20260921T091000", "b")
    _write_log(isolated_logs, "R2", "20260921T100000", "c")
    (isolated_logs / "R3").mkdir(parents=True)  # empty

    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all directory")
    out = capsys.readouterr().out
    assert "Delete all 3 terminal logs and 3 device log directories? [y/N]:" in out
    assert out.strip().endswith("Deleted 3 terminal logs and 3 device log directories.")
    assert not (isolated_logs / "R1").exists()
    assert not (isolated_logs / "R2").exists()
    assert not (isolated_logs / "R3").exists()
    assert isolated_logs.is_dir()  # logs/terminal/ itself remains


def test_delete_all_directory_singular_wording(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all directory")
    out = capsys.readouterr().out
    assert "Delete all 1 terminal log and 1 device log directory? [y/N]:" in out
    assert "Deleted 1 terminal log and 1 device log directory." in out


def test_delete_all_directory_declined_preserves_everything(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _answer(monkeypatch, "n")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all directory")
    assert "Delete cancelled." in capsys.readouterr().out
    assert (isolated_logs / "R1" / "20260921T090000.log").exists()


def test_delete_all_directory_empty_root_no_prompt(isolated_logs, lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all directory")
    out = capsys.readouterr().out
    assert out.strip() == "% No device logging directories found."
    assert "[y/N]" not in out
    assert not isolated_logs.exists()


@pytest.mark.parametrize(
    "make_unsafe_entry",
    [
        lambda d: (d / "notes.txt").write_text("not a log"),
        lambda d: (d / "nested").mkdir(),
    ],
    ids=["unknown-file", "nested-directory"],
)
def test_global_directory_deletion_blocked_by_one_unsafe_device(
    isolated_logs, lab_root, capsys, make_unsafe_entry
):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _write_log(isolated_logs, "R2", "20260921T100000", "b")
    _write_log(isolated_logs, "SW2", "20260921T110000", "c")
    make_unsafe_entry(isolated_logs / "SW2")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all directory")
    out = capsys.readouterr().out
    assert out.startswith("%")
    assert "[y/N]" not in out
    assert (isolated_logs / "R1" / "20260921T090000.log").exists()
    assert (isolated_logs / "R2" / "20260921T100000.log").exists()
    assert (isolated_logs / "SW2" / "20260921T110000.log").exists()
    assert (isolated_logs / "R1").is_dir()
    assert (isolated_logs / "R2").is_dir()
    assert (isolated_logs / "SW2").is_dir()


def test_global_directory_deletion_unrelated_root_level_file_untouched(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    isolated_logs.mkdir(parents=True, exist_ok=True)
    (isolated_logs / "stray.txt").write_text("unrelated root-level file")
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all directory")
    assert (isolated_logs / "stray.txt").read_text() == "unrelated root-level file"
    assert isolated_logs.is_dir()


# ==========================================================================
# Active-writer safety: normal production terminal session, using a
# real (mocked-command) tmux session
# ==========================================================================


def test_active_normal_terminal_blocks_individual_file_deletion(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_device_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    filename = _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, f"delete logging DW1 {filename}")
    out = capsys.readouterr().out
    assert out.strip() == "% Cannot delete an active terminal log for device 'DW1'."
    assert "[y/N]" not in out
    assert (isolated_logs / "DW1" / filename).exists()


def test_active_normal_terminal_blocks_device_all(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_device_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging DW1 all")
    out = capsys.readouterr().out
    assert out.strip() == "% Cannot delete all logs for 'DW1' while a terminal log is active."
    assert "[y/N]" not in out
    assert any((isolated_logs / "DW1").iterdir())


def test_active_normal_terminal_blocks_device_directory(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_device_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging DW1 directory")
    out = capsys.readouterr().out
    assert out.strip() == (
        "% Cannot delete logging directory for 'DW1' while a managed terminal session is still open."
    )
    assert "[y/N]" not in out
    assert (isolated_logs / "DW1").is_dir()
    assert any((isolated_logs / "DW1").iterdir())
    assert terminal._session_exists(terminal.derive_production_session_name("DW1"))


def test_active_normal_terminal_blocks_device_directory_message_is_dynamic_per_device(
    isolated_logs, lab_root, monkeypatch, capsys
):
    """Same condition as above, a different device ID (DW2 -- one of this
    file's own reserved real-tmux writer-test device names, see
    _WRITER_TEST_DEVICE_IDS's own comment; never an unrelated name like
    'PAGENT' that other test files also use for real tmux sessions on the
    same shared socket) -- proves the message text substitutes the device
    ID dynamically, not hard-coded."""
    _fake_transport(monkeypatch)
    terminal.open_device_terminal("DW2", {"transport": "ssh", "address": "192.0.2.2"})
    _first_log_filename(isolated_logs, "DW2")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging DW2 directory")
    out = capsys.readouterr().out
    assert out.strip() == (
        "% Cannot delete logging directory for 'DW2' while a managed terminal session is still open."
    )


def test_inactive_device_deletion_proceeds_while_another_device_is_active(
    isolated_logs, lab_root, monkeypatch, capsys
):
    _fake_transport(monkeypatch)
    terminal.open_device_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")
    _write_log(isolated_logs, "R2", "20260921T090000", "inactive")

    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R2 all")
    assert capsys.readouterr().out.strip().endswith("Deleted 1 terminal log for R2.")
    assert terminal.list_device_logs("R2") == []
    assert any((isolated_logs / "DW1").iterdir())  # untouched, still active


def test_active_normal_terminal_never_closed_by_rejected_deletion(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_device_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging DW1 all")  # rejected
    assert terminal._session_exists(terminal.derive_production_session_name("DW1"))


# ==========================================================================
# Discovery bootstrap active-writer safety, using the actual
# open_bootstrap_terminal() writer path -- never a renamed production
# session.
# ==========================================================================


def test_discovery_bootstrap_active_log_blocks_individual_deletion(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    filename = _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, f"delete logging DW1 {filename}")
    out = capsys.readouterr().out
    assert out.strip() == "% Cannot delete an active terminal log for device 'DW1'."
    assert "[y/N]" not in out
    assert (isolated_logs / "DW1" / filename).exists()


def test_discovery_bootstrap_active_log_blocks_device_all(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging DW1 all")
    out = capsys.readouterr().out
    assert out.strip() == "% Cannot delete all logs for 'DW1' while a terminal log is active."
    assert "[y/N]" not in out
    assert any((isolated_logs / "DW1").iterdir())


def test_discovery_bootstrap_active_log_blocks_device_directory(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging DW1 directory")
    out = capsys.readouterr().out
    assert out.strip() == (
        "% Cannot delete logging directory for 'DW1' while a managed terminal session is still open."
    )
    assert "[y/N]" not in out
    assert (isolated_logs / "DW1").is_dir()
    assert terminal._session_exists(terminal.derive_discovery_session_name("DW1"))


def test_discovery_bootstrap_active_log_blocks_global_all(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")
    _write_log(isolated_logs, "R2", "20260921T090000", "b")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all")
    out = capsys.readouterr().out
    assert out.strip() == (
        "% Cannot delete all terminal logs while terminal logs are active. "
        "Close or wait for the active sessions first."
    )
    assert "[y/N]" not in out
    assert any((isolated_logs / "DW1").iterdir())
    assert [name for _, name in terminal.list_device_logs("R2")] == ["20260921T090000.log"]


def test_discovery_bootstrap_active_log_blocks_global_all_directory(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")
    _write_log(isolated_logs, "R2", "20260921T090000", "b")
    (isolated_logs / "R3").mkdir(parents=True)

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all directory")
    out = capsys.readouterr().out
    assert out.startswith("%")
    assert "[y/N]" not in out
    assert any((isolated_logs / "DW1").iterdir())
    assert (isolated_logs / "R2" / "20260921T090000.log").exists()
    assert (isolated_logs / "R3").is_dir()


def test_unrelated_device_deletion_proceeds_while_discovery_is_active(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")
    _write_log(isolated_logs, "R2", "20260921T090000", "b")

    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R2 all")
    assert capsys.readouterr().out.strip().endswith("Deleted 1 terminal log for R2.")
    assert any((isolated_logs / "DW1").iterdir())  # Discovery's log untouched


def test_deletion_resumes_after_discovery_session_closes(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")
    terminal.close_bootstrap_terminal("DW1")

    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging DW1 all")
    assert capsys.readouterr().out.strip().endswith("Deleted 1 terminal log for DW1.")
    assert terminal.list_device_logs("DW1") == []


def test_discovery_active_never_closed_by_rejected_deletion(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging DW1 all")  # rejected
    assert terminal._session_exists(terminal.derive_discovery_session_name("DW1"))


# ==========================================================================
# Bulk atomicity: preflight failure deletes zero files
# ==========================================================================


def test_global_all_atomicity_with_active_normal_terminal(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _fake_transport(monkeypatch)
    terminal.open_device_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all")
    assert capsys.readouterr().out.startswith("%")
    assert [name for _, name in terminal.list_device_logs("R1")] == ["20260921T090000.log"]
    assert any((isolated_logs / "DW1").iterdir())


def test_global_all_atomicity_with_active_discovery(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all")
    assert capsys.readouterr().out.startswith("%")
    assert [name for _, name in terminal.list_device_logs("R1")] == ["20260921T090000.log"]
    assert any((isolated_logs / "DW1").iterdir())


def test_global_directory_atomicity_with_active_normal_terminal(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _fake_transport(monkeypatch)
    terminal.open_device_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all directory")
    assert capsys.readouterr().out.startswith("%")
    assert (isolated_logs / "R1" / "20260921T090000.log").exists()
    assert (isolated_logs / "DW1").is_dir()


def test_global_directory_atomicity_with_active_discovery(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all directory")
    assert capsys.readouterr().out.startswith("%")
    assert (isolated_logs / "R1" / "20260921T090000.log").exists()
    assert (isolated_logs / "DW1").is_dir()


# ==========================================================================
# Re-preflight / confirmation-target-manifest stability
# ==========================================================================


def test_new_log_appearing_during_confirmation_aborts_device_all(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "SW2", "20260921T090000", "a")

    def _answer_and_add_log():
        _write_log(isolated_logs, "SW2", "20260921T100000", "b")
        return "y"

    monkeypatch.setattr(climain, "_read_confirmation_line", _answer_and_add_log)
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging SW2 all")
    out = capsys.readouterr().out
    assert "Logging state changed while waiting for confirmation." in out
    assert "No logs were deleted. Retry the command." in out
    assert (isolated_logs / "SW2" / "20260921T090000.log").exists()
    assert (isolated_logs / "SW2" / "20260921T100000.log").exists()


def test_active_writer_appearing_during_confirmation_aborts_device_all(
    isolated_logs, lab_root, monkeypatch, capsys
):
    _fake_transport(monkeypatch)
    _write_log(isolated_logs, "DW1", "20260921T090000", "a")

    def _answer_and_start_writer():
        terminal.open_device_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
        return "y"

    monkeypatch.setattr(climain, "_read_confirmation_line", _answer_and_start_writer)
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging DW1 all")
    out = capsys.readouterr().out
    assert out.strip().endswith("% Cannot delete all logs for 'DW1' while a terminal log is active.")
    assert (isolated_logs / "DW1" / "20260921T090000.log").exists()
    assert terminal._session_exists(terminal.derive_production_session_name("DW1"))


def test_unknown_entry_appearing_during_confirmation_aborts_device_directory(
    isolated_logs, lab_root, monkeypatch, capsys
):
    _write_log(isolated_logs, "SW2", "20260921T090000", "a")

    def _answer_and_add_unknown_file():
        (isolated_logs / "SW2" / "notes.txt").write_text("surprise")
        return "y"

    monkeypatch.setattr(climain, "_read_confirmation_line", _answer_and_add_unknown_file)
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging SW2 directory")
    out = capsys.readouterr().out
    assert "% Cannot delete logging directory for 'SW2'" in out
    assert (isolated_logs / "SW2" / "20260921T090000.log").exists()
    assert (isolated_logs / "SW2" / "notes.txt").exists()
    assert (isolated_logs / "SW2").is_dir()


def test_file_disappearing_during_confirmation_aborts_single_file_delete(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")

    def _answer_and_remove_file():
        (isolated_logs / "R1" / "20260921T090000.log").unlink()
        return "y"

    monkeypatch.setattr(climain, "_read_confirmation_line", _answer_and_remove_file)
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 20260921T090000.log")
    out = capsys.readouterr().out
    assert "% No log file '20260921T090000.log' for device 'R1'." in out


def test_new_device_appearing_during_confirmation_aborts_global_all(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")

    def _answer_and_add_new_device():
        _write_log(isolated_logs, "R2", "20260921T100000", "b")
        return "y"

    monkeypatch.setattr(climain, "_read_confirmation_line", _answer_and_add_new_device)
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all")
    out = capsys.readouterr().out
    assert "Logging state changed while waiting for confirmation." in out
    assert (isolated_logs / "R1" / "20260921T090000.log").exists()
    assert (isolated_logs / "R2" / "20260921T100000.log").exists()


def test_new_directory_appearing_during_confirmation_aborts_global_directory(
    isolated_logs, lab_root, monkeypatch, capsys
):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")

    def _answer_and_add_new_directory():
        (isolated_logs / "R2").mkdir(parents=True)
        return "y"

    monkeypatch.setattr(climain, "_read_confirmation_line", _answer_and_add_new_directory)
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all directory")
    out = capsys.readouterr().out
    assert "Logging state changed while waiting for confirmation." in out
    assert (isolated_logs / "R1" / "20260921T090000.log").exists()
    assert (isolated_logs / "R2").is_dir()


# ==========================================================================
# Directory auto-recreation compatibility
# ==========================================================================


def test_device_directory_is_recreated_by_future_logging(isolated_logs, lab_root, monkeypatch):
    _write_log(isolated_logs, "DW1", "20260921T090000", "a")
    _answer(monkeypatch, "y")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging DW1 directory")
    assert not (isolated_logs / "DW1").exists()

    _fake_transport(monkeypatch)
    terminal.open_device_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    assert _wait_until(lambda: (isolated_logs / "DW1").is_dir() and any((isolated_logs / "DW1").iterdir()))
