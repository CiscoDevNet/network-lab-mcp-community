# SPDX-License-Identifier: Apache-2.0
"""UTF-8 Safe Editor Launch against the real libc and a fake editor child
(never a real Vim). Host-dependent results are skipped or only recorded."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys

import pytest

from network_lab_mcp.cli import editor, utf8_locale

MISSING = "zz_ZZ.UTF-8"


@pytest.fixture(autouse=True)
def _utf8_locale_validation_stub():
    """Override the conftest stub: this module uses the real validator."""


@pytest.fixture(autouse=True)
def _isolated_tempdir(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def _host_utf8_locale():
    for name in ("en_US.UTF-8", "C.UTF-8", "en_US.utf8", "C.utf8"):
        if utf8_locale.validate_utf8_locale({"LC_ALL": name}).ok:
            return name
    pytest.skip("no UTF-8 locale usable by the host libc")


def test_real_utf8_lc_all_succeeds():
    assert utf8_locale.validate_utf8_locale({"LC_ALL": _host_utf8_locale()}).ok


def test_real_lc_all_takes_priority_over_lang_and_lc_ctype():
    env = {"LC_ALL": _host_utf8_locale(), "LANG": MISSING, "LC_CTYPE": "C"}
    assert utf8_locale.validate_utf8_locale(env).ok


def test_real_invalid_lc_all_fails():
    assert not utf8_locale.validate_utf8_locale({"LC_ALL": MISSING}).ok


def test_real_repair_selects_and_reverifies():
    name = _host_utf8_locale()
    env = {"LANG": MISSING, "LC_CTYPE": "C", "PATH": os.environ.get("PATH", "")}
    fixed = utf8_locale.prepare_editor_env(env)
    assert fixed["LC_ALL"] and fixed["LANG"] == MISSING and fixed["LC_CTYPE"] == "C"
    assert utf8_locale.validate_utf8_locale(fixed).ok
    assert "LC_ALL" not in env
    del name


def test_real_non_utf8_c_locale_is_rejected():
    assert utf8_locale.validate_utf8_locale({"LC_ALL": "C"}).reason == "non_utf8_codeset"


def test_record_valid_ctype_with_invalid_lang_hypothesis(capsys):
    """Records (does not assert) whether this libc accepts LC_CTYPE=<utf8>
    with an invalid LANG -- the macOS failure hypothesis."""
    name = _host_utf8_locale()
    result = utf8_locale.validate_utf8_locale({"LANG": MISSING, "LC_CTYPE": name})
    with capsys.disabled():
        print(
            f"\n[libc-record] os={platform.system()} {platform.release()} "
            f"libc={platform.libc_ver()} python={platform.python_version()} "
            f"LANG={MISSING} LC_CTYPE={name} -> ok={result.ok} reason={result.reason}"
        )


# ---- fake editor child ----

_YAML = (
    "name: utf8_editor_test\n"
    "title: IOS XRルーターの初期化\n"
    "description: >\n  日本語とEnglishが混在するScenarioです。\n"
    "notes:\n  - 日本語\n  - café\n  - ✅\n"
)

_RECORDER = """\
import json, locale, os, sys
path = sys.argv[-1]
rec = {k: v for k, v in os.environ.items() if k in ("LANG", "LC_ALL", "LC_CTYPE")}
try:
    locale.setlocale(locale.LC_ALL, "")
    rec["codeset"] = locale.nl_langinfo(locale.CODESET)
except locale.Error:
    rec["codeset"] = None
raw = open(path, "rb").read()
rec["before_utf8"] = raw.decode("utf-8")
{action}
open(os.environ["REC_PATH"], "w", encoding="utf-8").write(json.dumps(rec))
"""


def _fake(tmp_path, monkeypatch, action):
    script = tmp_path / "recording_editor.py"
    script.write_text(_RECORDER.replace("{action}", action), encoding="utf-8")
    monkeypatch.setenv("REC_PATH", str(tmp_path / "rec.json"))
    monkeypatch.setenv("PYTHONCOERCECLOCALE", "0")
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", f"{sys.executable} -X utf8=0 -S {script}")
    return tmp_path / "rec.json"


def _data():
    import yaml

    return yaml.safe_load(_YAML)


def test_japanese_yaml_round_trip_with_repaired_child_env(monkeypatch, tmp_path):
    name = _host_utf8_locale()
    rec = _fake(tmp_path, monkeypatch, "")
    for key in ("LC_ALL", "LC_CTYPE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LANG", MISSING)
    result = editor.edit_yaml_candidate(_data())
    seen = json.loads(rec.read_text(encoding="utf-8"))
    assert result == _data()
    assert seen["LC_ALL"] and seen["LANG"] == MISSING
    assert seen["codeset"].lower().replace("-", "") == "utf8"
    assert "日本語とEnglish" in seen["before_utf8"] and "�" not in seen["before_utf8"]
    assert not seen["before_utf8"].startswith("﻿")
    del name


def test_editor_saves_japanese_back(monkeypatch, tmp_path):
    _host_utf8_locale()
    action = "open(path, 'w', encoding='utf-8').write('name: x\\ntitle: 日本語✅\\n')"
    _fake(tmp_path, monkeypatch, action)
    assert editor.edit_yaml_candidate({"name": "x"}) == {"name": "x", "title": "日本語✅"}


def test_bom_is_tolerated_on_read_and_not_in_result(monkeypatch, tmp_path):
    _host_utf8_locale()
    action = "open(path, 'wb').write(b'\\xef\\xbb\\xbfname: bom\\ntitle: \\xe6\\x97\\xa5\\n')"
    _fake(tmp_path, monkeypatch, action)
    result = editor.edit_yaml_candidate({"name": "x"})
    assert result == {"name": "bom", "title": "日"}
    assert all("﻿" not in k for k in result)


def test_invalid_utf8_is_a_clean_error_and_cleans_up(monkeypatch, tmp_path):
    _host_utf8_locale()
    _fake(tmp_path, monkeypatch, "open(path, 'wb').write(b'name: \\xff\\xfe\\n')")
    with pytest.raises(editor.EditorError, match="UTF-8"):
        editor.edit_yaml_candidate({"name": "x"})
    assert list(tmp_path.glob("network-lab-mcp-*.yaml")) == []


def test_unrepairable_locale_does_not_launch_editor_or_create_temp_file(monkeypatch, tmp_path):
    marker = tmp_path / "launched"
    _fake(tmp_path, monkeypatch, f"open({str(marker)!r}, 'w').write('x')")
    monkeypatch.setattr(utf8_locale, "validate_utf8_locale", lambda env, timeout=3.0: utf8_locale.LocaleCheck(False, "invalid_locale"))
    with pytest.raises(editor.EditorError, match="UTF-8 locale"):
        editor.edit_yaml_candidate({"name": "x"})
    assert not marker.exists()
    assert list(tmp_path.glob("network-lab-mcp-*.yaml")) == []


@pytest.mark.skipif(sys.version_info < (3, 10), reason="EncodingWarning needs Python 3.10+")
def test_no_default_encoding_dependence_in_edit_path(tmp_path):
    script = tmp_path / "run_edit.py"
    script.write_text(
        "import os, sys, tempfile\n"
        f"tempfile.tempdir = {str(tmp_path)!r}\n"
        "from network_lab_mcp.cli import editor, utf8_locale\n"
        "utf8_locale.validate_utf8_locale = lambda env, timeout=3.0: utf8_locale.LocaleCheck(True, 'ok')\n"
        "os.environ.pop('VISUAL', None)\n"
        "os.environ['EDITOR'] = sys.executable + ' -c pass'\n"
        "print(editor.edit_yaml_candidate({'t': '日本語'}))\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-X", "warn_default_encoding", "-W", "error::EncodingWarning", str(script)],
        capture_output=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
