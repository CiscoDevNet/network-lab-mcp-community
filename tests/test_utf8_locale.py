# SPDX-License-Identifier: Apache-2.0
"""UTF-8 Safe Editor Launch: locale decision rules (mock validator) and the
isolation properties of the real validation subprocess."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from network_lab_mcp.cli import editor, utf8_locale
from network_lab_mcp.cli.utf8_locale import LocaleCheck

_CATEGORIES = ("LC_CTYPE", "LC_COLLATE", "LC_MESSAGES", "LC_MONETARY", "LC_NUMERIC", "LC_TIME")


@pytest.fixture(autouse=True)
def _utf8_locale_validation_stub():
    """Override the conftest stub: this module uses explicit validators."""


def make_validator(available, calls=None):
    """Test-only approximation. Production validation uses libc CODESET.
    Resolves LC_ALL > LC_<cat> > LANG per category (empty = unset, nothing
    set = C); C/POSIX are always valid but never UTF-8; anything not in
    `available` is invalid; UTF-8 is guessed from the effective LC_CTYPE name."""
    available = set(available)

    def effective(env, category):
        for key in ("LC_ALL", category, "LANG"):
            if env.get(key):
                return env[key]
        return "C"

    def validate(env, timeout=3.0):
        if calls is not None:
            calls.append(dict(env))
        for category in _CATEGORIES:
            name = effective(env, category)
            if name not in ("C", "POSIX") and name not in available:
                return LocaleCheck(False, "invalid_locale")
        ctype = effective(env, "LC_CTYPE").lower()
        if "utf-8" in ctype or "utf8" in ctype:
            return LocaleCheck(True, "ok")
        return LocaleCheck(False, "non_utf8_codeset")

    return validate


def run(env, available):
    return utf8_locale.prepare_editor_env(env, make_validator(available))


def test_a_valid_utf8_untouched():
    env = {"LANG": "en_US.UTF-8", "LC_CTYPE": "en_US.UTF-8"}
    assert run(env, ["en_US.UTF-8"]) == env


def test_b_actual_failure_condition():
    out = run({"LANG": "en_JP.utf-8", "LC_CTYPE": "C"}, ["en_US.UTF-8"])
    assert out == {"LANG": "en_JP.utf-8", "LC_CTYPE": "C", "LC_ALL": "en_US.UTF-8"}


def test_c_lc_all_c():
    out = run({"LANG": "en_US.UTF-8", "LC_ALL": "C"}, ["en_US.UTF-8"])
    assert out == {"LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}


def test_d_only_c_utf8():
    out = run({"LANG": "C", "LC_CTYPE": "C"}, ["C.UTF-8"])
    assert out["LC_ALL"] == "C.UTF-8" and out["LANG"] == "C" and out["LC_CTYPE"] == "C"


def test_e_existing_c_utf8():
    env = {"LANG": "C.UTF-8"}
    assert run(env, ["C.UTF-8"]) == env


def test_f_lc_all_utf8_wins():
    env = {"LANG": "en_US.UTF-8", "LC_CTYPE": "C", "LC_ALL": "en_US.UTF-8"}
    assert run(env, ["en_US.UTF-8"]) == env


def test_g1_invalid_locale_with_alternative():
    out = run({"LANG": "invalid_LOCALE", "LC_CTYPE": "invalid_LOCALE"}, ["en_US.UTF-8"])
    assert out["LC_ALL"] == "en_US.UTF-8"
    assert out["LANG"] == "invalid_LOCALE" and out["LC_CTYPE"] == "invalid_LOCALE"


def test_g2_invalid_locale_no_alternative():
    with pytest.raises(utf8_locale.UnavailableUTF8Locale):
        run({"LANG": "invalid_LOCALE", "LC_CTYPE": "invalid_LOCALE"}, [])


def test_h_no_utf8_candidate():
    with pytest.raises(utf8_locale.UnavailableUTF8Locale):
        run({"LANG": "C", "LC_CTYPE": "C"}, [])


def test_i_empty_lc_all():
    out = run({"LANG": "en_US.UTF-8", "LC_CTYPE": "C", "LC_ALL": ""}, ["en_US.UTF-8"])
    assert out["LC_ALL"] == "en_US.UTF-8"


def test_j_valid_non_utf8_locale():
    out = run(
        {"LANG": "ja_JP.eucJP", "LC_ALL": "ja_JP.eucJP"}, ["ja_JP.eucJP", "ja_JP.UTF-8"]
    )
    assert out == {"LANG": "ja_JP.eucJP", "LC_ALL": "ja_JP.UTF-8"}


def test_k_all_unset():
    assert run({}, ["en_US.UTF-8"]) == {"LC_ALL": "en_US.UTF-8"}


def test_l_final_revalidation_failure():
    inner = make_validator(["en_US.UTF-8"])
    seen = []

    def validate(env, timeout=3.0):
        seen.append(env.get("LC_ALL"))
        if seen.count("en_US.UTF-8") >= 2:  # candidate probe ok, final check fails
            return LocaleCheck(False, "validation_error")
        return inner(env, timeout)

    with pytest.raises(utf8_locale.UnavailableUTF8Locale):
        utf8_locale.prepare_editor_env({"LANG": "C"}, validate)
    assert seen.count("en_US.UTF-8") == 2


def test_m_unsafe_name_builds_no_candidate_and_fixed_candidate_used():
    env = {"LANG": "invalid;command", "LC_CTYPE": "C"}
    assert all(";" not in c for c in utf8_locale.candidate_locales(env))
    out = run(env, ["en_US.UTF-8"])
    assert out["LC_ALL"] == "en_US.UTF-8" and out["LANG"] == "invalid;command"


def test_p_valid_ctype_invalid_lang_needs_all_category_check():
    env = {"LANG": "en_JP.utf-8", "LC_CTYPE": "en_US.UTF-8"}
    assert run(env, ["en_US.UTF-8"])["LC_ALL"] == "en_US.UTF-8"


def test_q_language_candidate_preferred():
    env = {"LANG": "en_US.UTF-8", "LC_CTYPE": "C"}
    out = run(env, ["en_US.UTF-8", "C.UTF-8"])
    assert out["LC_ALL"] == "en_US.UTF-8"


@pytest.mark.parametrize("bad", ["LC_TIME", "LC_MESSAGES"])
def test_single_invalid_category_triggers_repair(bad):
    env = {"LANG": "en_US.UTF-8", bad: "xx_XX.UTF-8"}
    assert run(env, ["en_US.UTF-8"])["LC_ALL"] == "en_US.UTF-8"


def test_valid_lc_all_overrides_invalid_individual_category():
    env = {"LC_ALL": "en_US.UTF-8", "LC_TIME": "xx_XX.UTF-8"}
    assert run(env, ["en_US.UTF-8"]) == env


def test_candidate_order_dedup_and_derivation():
    names = utf8_locale.candidate_locales({"LANG": "en_US"})
    assert names == ["en_US.UTF-8", "C.UTF-8", "en_US.utf8", "C.utf8", "UTF-8"]
    assert utf8_locale.candidate_locales({"LANG": "ja_JP.eucJP"})[0] == "ja_JP.UTF-8"
    assert utf8_locale.candidate_locales({"LC_ALL": "C", "LANG": "en_JP.utf-8"})[0] == "en_JP.UTF-8"


@pytest.mark.parametrize("value", ["de_DE@euro", "sr_RS@latin"])
def test_modifier_locales_use_fixed_candidates(value):
    assert utf8_locale.candidate_locales({"LANG": value}) == list(utf8_locale._FIXED_CANDIDATES)


def test_total_time_budget_is_enforced():
    now = [0.0]
    calls = []

    def validate(env, timeout):
        calls.append(timeout)
        now[0] += 4.0
        return LocaleCheck(False, "timeout")

    with pytest.raises(utf8_locale.UnavailableUTF8Locale):
        utf8_locale.prepare_editor_env({"LANG": "C"}, validate, clock=lambda: now[0])
    assert len(calls) <= 3 and all(t <= 3.0 for t in calls)


@pytest.mark.parametrize("failure", [True, False])
def test_parent_environment_unchanged(monkeypatch, fake_editor, failure):
    monkeypatch.setattr(
        utf8_locale, "validate_utf8_locale", make_validator([] if failure else ["en_US.UTF-8"])
    )
    for key in ("LC_ALL", "LC_CTYPE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setenv("EDITOR", f"python3 {fake_editor('open(path, chr(119)).write(chr(110)+chr(58)+chr(32)+chr(120))')}")
    before = os.environ.copy()
    try:
        editor.edit_yaml_candidate({"n": "o"})
    except editor.EditorError:
        assert failure
    assert os.environ == before


# ---- validation subprocess isolation ----


def _stub_executable(tmp_path, body):
    script = tmp_path / "stub_python"
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(0o700)
    return str(script)


def test_validation_runs_in_root_with_devnull_stdin_and_exact_token(monkeypatch, tmp_path):
    # Prints the token only if cwd is "/" and stdin is at EOF immediately.
    exe = _stub_executable(
        tmp_path, '[ "$(pwd)" = / ] && [ -z "$(cat)" ] && printf "NLM_CODESET=UTF-8\\n"'
    )
    monkeypatch.setattr(sys, "executable", exe)
    assert utf8_locale.validate_utf8_locale({"LANG": "C"}) == LocaleCheck(True, "ok")


@pytest.mark.parametrize(
    "body,reason",
    [
        ("printf 'NLM_CODESET=UTF-8\\nextra\\n'", "invalid_output"),
        ("printf 'NLM_CODESET=UTF-8'", "invalid_output"),
        ("printf 'something\\n'", "invalid_output"),
        ("printf '\\377\\n'", "invalid_output"),
        ("printf 'NLM_CODESET=UTF-8\\n'; exit 1", "validation_error"),
        ("exit 2", "invalid_locale"),
        ("exit 3", "non_utf8_codeset"),
    ],
)
def test_unexpected_output_fails_closed(monkeypatch, tmp_path, body, reason):
    monkeypatch.setattr(sys, "executable", _stub_executable(tmp_path, body))
    assert utf8_locale.validate_utf8_locale({}) == LocaleCheck(False, reason)


def test_validation_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "executable", _stub_executable(tmp_path, "sleep 5"))
    assert utf8_locale.validate_utf8_locale({}, timeout=0.2) == LocaleCheck(False, "timeout")


@pytest.mark.parametrize("value", ["", "/nonexistent/python"])
def test_unusable_sys_executable_fails_safely(monkeypatch, value):
    monkeypatch.setattr(sys, "executable", value)
    assert utf8_locale.validate_utf8_locale({"LANG": "C"}) == LocaleCheck(False, "process_launch_failed")


def test_validation_command_and_environment(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b"NLM_CODESET=UTF-8\n", stderr=b"")

    monkeypatch.setattr(utf8_locale.subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    env = {
        "LANG": "en_US.UTF-8", "LC_TIME": "C", "LOCPATH": "rel/dir", "PATH": "/usr/bin",
        "PYTHONPATH": "/evil", "PYTHONSTARTUP": "/evil.py", "PYTHONUTF8": "1",
        "PYTHONCOERCECLOCALE": "1", "SECRET_TOKEN": "x",
    }
    assert utf8_locale.validate_utf8_locale(env).ok
    assert captured["cwd"] == "/" and captured["stdin"] == subprocess.DEVNULL
    assert captured["shell"] is False and captured["timeout"] == 3.0
    assert captured["command"][1:4] == ["-X", "utf8=0", "-S"]
    assert ("-P" in captured["command"]) == (sys.version_info >= (3, 11))
    child = captured["env"]
    assert child["PYTHONUTF8"] == "0" and child["PYTHONCOERCECLOCALE"] == "0"
    assert child["LOCPATH"] == str(tmp_path / "rel" / "dir")
    assert not {"PYTHONPATH", "PYTHONSTARTUP", "SECRET_TOKEN"} & set(child)
    assert child["LANG"] == "en_US.UTF-8" and child["LC_TIME"] == "C"
    assert env["LOCPATH"] == "rel/dir"  # editor env is never rewritten


def test_fake_locale_py_in_cwd_is_not_imported(monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    (tmp_path / "locale.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    result = utf8_locale.validate_utf8_locale(dict(os.environ, PYTHONPATH=str(tmp_path)))
    assert not marker.exists()
    assert result.reason in ("ok", "invalid_locale", "non_utf8_codeset")
