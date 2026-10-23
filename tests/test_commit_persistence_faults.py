# SPDX-License-Identifier: Apache-2.0
"""Fault injection across the commit's write boundaries.

Commit writes the definition file first, then settings.yaml; each file is
written via temp file + os.replace (atomic per file), but the two writes are
not one transaction. These tests record what actually happens when a failure
is injected at each boundary and that a retry converges. Isolated lab_root,
fictional credentials only."""

from __future__ import annotations

import hashlib
import os

import pytest

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain

SENTINEL = "SENTINEL-pw-do-not-leak"
real_replace = os.replace


def _snap(lab_root):
    return {str(p.relative_to(lab_root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(lab_root.rglob("*")) if p.is_file()}


def _session(lab_root):
    s = cfgmod.CliSession(lab_root)
    s.enter_configure()
    s.enter_access_info_definition("sr-mpls")
    s.enter_access_info_device("R1")
    s.set_device_field("type", "iosxr")
    s.set_device_field("address", "192.0.2.10")
    s.set_device_field("password", SENTINEL)
    s.go_to_global()
    s.select_access_info("sr-mpls")
    return s


def _fail_replace(monkeypatch, when):
    """Fail os.replace when `when(dst_name, call_index)`; record every call."""
    calls = []

    def fake(src, dst):
        calls.append(os.path.basename(str(dst)))
        if when(os.path.basename(str(dst)), len(calls)):
            raise OSError("injected replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fake)
    return calls


def _no_new_broken_refs(lab_root):
    s = lab.read_settings(lab_root)
    assert lab.access_info_exists(s["active_access_info"], lab_root)


def test_t29_a_first_write_fails(lab_root, monkeypatch, capsys):
    s = _session(lab_root)
    before = _snap(lab_root)
    calls = _fail_replace(monkeypatch, lambda name, i: i == 1)
    capsys.readouterr()
    with pytest.raises(OSError):
        s.commit()
    assert calls == ["sr-mpls.yaml"]  # injection fired at the definition's replace
    after = _snap(lab_root)
    assert {k: v for k, v in after.items() if not k.endswith(".tmp")} == before
    assert not lab.access_info_exists("sr-mpls", lab_root)
    assert s.definition_dirty() and s.settings_dirty()
    assert s.last_commit_report is None
    assert s.definition_original is None  # not marked saved
    _no_new_broken_refs(lab_root)


def test_t29_b_definition_saved_settings_write_fails_before_replace(lab_root, monkeypatch, capsys):
    s = _session(lab_root)
    settings_before = (lab_root / "settings.yaml").read_bytes()
    spy = []

    def boom(*a, **k):
        spy.append(1)
        raise OSError("injected settings failure")

    monkeypatch.setattr(lab, "write_settings", boom)
    with pytest.raises(OSError):
        s.commit()
    assert spy == [1]
    assert lab.access_info_exists("sr-mpls", lab_root)  # definition saved
    assert (lab_root / "settings.yaml").read_bytes() == settings_before
    assert lab.read_settings(lab_root)["active_access_info"] == "sample_lab"  # not yet selected
    _no_new_broken_refs(lab_root)
    assert s.last_commit_report is None
    assert s.settings_dirty() and not s.definition_dirty()  # definition now clean, selection pending
    assert s.settings_candidate["active_access_info"] == "sr-mpls"


def test_t29_c_settings_replace_fails_and_retry_converges(lab_root, monkeypatch, capsys):
    s = _session(lab_root)
    settings_before = (lab_root / "settings.yaml").read_bytes()
    calls = _fail_replace(monkeypatch, lambda name, i: name == "settings.yaml")
    capsys.readouterr()
    climain_out = None
    with pytest.raises(OSError):
        s.commit()
    assert calls == ["sr-mpls.yaml", "settings.yaml"]  # definition replaced, then injected failure
    assert (lab_root / "settings.yaml").read_bytes() == settings_before
    assert lab.load_access_info("sr-mpls", lab_root) == s.definition_original
    assert s.settings_dirty() and s.last_commit_report is None
    # temp-file residue: observed, not guaranteed clean
    leftovers = [p.name for p in lab_root.rglob("*.tmp")]
    assert leftovers in ([], ["settings.yaml.tmp"])
    assert "settings.yaml" in {p.name for p in lab_root.iterdir()}

    # ---- T29-D/E: retry (injection removed) ----
    monkeypatch.setattr(os, "replace", real_replace)
    definition_before = (lab_root / "access-info" / "sr-mpls.yaml").read_bytes()
    writes = []
    orig_write = lab.write_access_info
    monkeypatch.setitem(cfgmod._DEFINITION_WRITERS, "access_info", lambda *a, **k: (writes.append(a[0]), orig_write(*a, **k)))
    capsys.readouterr()
    climain.execute_command_line(s, "commit")
    out = capsys.readouterr().out
    assert writes == []  # Pattern A: saved definition is not rewritten
    assert (lab_root / "access-info" / "sr-mpls.yaml").read_bytes() == definition_before
    assert lab.read_settings(lab_root)["active_access_info"] == "sr-mpls"
    assert "Commit complete." in out and SENTINEL not in out
    assert s.last_commit_report.definition is None and s.last_commit_report.settings_written is True
    assert "was committed successfully" not in out  # honest: no definition write happened on retry
    assert not s.overall_dirty()
    _no_new_broken_refs(lab_root)
    assert not list(lab_root.rglob("*.tmp"))  # retry's replace consumed the temp name


def test_t29_cli_failure_never_prints_success(lab_root, monkeypatch, capsys):
    s = _session(lab_root)
    _fail_replace(monkeypatch, lambda name, i: name == "settings.yaml")
    capsys.readouterr()
    climain.execute_command_line(s, "commit")  # must not raise / crash the CLI
    cap = capsys.readouterr()
    assert "% Commit failed: could not write configuration (OSError)" in cap.out
    assert "injected" not in cap.out and s.settings_dirty()
    assert "Commit complete." not in cap.out
    assert SENTINEL not in cap.out + cap.err


def test_t29_f_retry_validates_saved_definition_again(lab_root, monkeypatch):
    """After a partial persist, a later edit of the saved definition is still
    strictly validated (a stale 'saved' definition is never trusted)."""
    s = _session(lab_root)
    monkeypatch.setattr(lab, "write_settings", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(OSError):
        s.commit()
    monkeypatch.undo()
    s.enter_access_info_definition("sr-mpls")
    s.enter_access_info_device("R1")
    s.set_device_field("jump_host", "nope")
    with pytest.raises(cfgmod.CommitValidationError):
        s.commit()
