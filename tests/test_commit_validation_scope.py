# SPDX-License-Identifier: Apache-2.0
"""Commit validation scope: strict validation of the changed definition
plus baseline/candidate comparison of running-config findings.

Unrelated pre-existing missing references are warnings; new or worsened
ones, and any defect in the changed definition itself, fail the commit.
Everything runs against the isolated `lab_root` fixture with fictional
credentials."""

from __future__ import annotations

import hashlib

import pytest
import yaml

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain

SENTINEL = "SENTINEL-pw-do-not-leak"


def _break_reference(lab_root, name):
    """Make the selected reference `name` pre-exist as a missing definition."""
    settings = lab.read_settings(lab_root)
    refs = settings.setdefault("active_references", [])
    if name not in refs:
        refs.append(name)
        lab.write_settings(settings, lab_root)
    path = lab_root / "references" / f"{name}.yaml"
    if path.exists():
        path.unlink()
    assert not lab.reference_exists(name, lab_root)


def _digests(lab_root):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(lab_root.rglob("*.yaml"))}


def _new_access_info(lab_root, name="sr-mpls", password=SENTINEL):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition(name)
    session.enter_access_info_device("R1")
    session.set_device_field("type", "iosxr")
    session.set_device_field("address", "192.0.2.10")
    session.set_device_field("password", password)
    return session


def test_t01_valid_new_access_info_no_warning(lab_root, capsys):
    session = _new_access_info(lab_root)
    climain.execute_command_line(session, "commit")
    out = capsys.readouterr().out
    assert "Commit complete." in out and "Warning" not in out
    assert lab.access_info_exists("sr-mpls", lab_root)
    assert session.last_commit_report.warnings == ()


@pytest.mark.parametrize("missing", ["sample", "other_ref", "x"])
def test_t02_new_access_info_with_unrelated_missing_reference(lab_root, capsys, missing):
    _break_reference(lab_root, missing)
    settings_before = (lab_root / "settings.yaml").read_bytes()
    session = _new_access_info(lab_root)
    assert session.commit() is True
    assert session.last_commit_report.warnings == (f"reference '{missing}'",)
    assert lab.access_info_exists("sr-mpls", lab_root)
    assert (lab_root / "settings.yaml").read_bytes() == settings_before
    assert not session.overall_dirty()
    assert session.mode == "access_info" or session.mode.startswith("access")


def test_t02_cli_output(lab_root, capsys):
    _break_reference(lab_root, "sample")
    session = _new_access_info(lab_root)
    capsys.readouterr()
    climain.execute_command_line(session, "commit")
    out = capsys.readouterr().out
    assert "Commit complete." in out
    assert "% Warning: Running-config references missing definition:\n  reference 'sample'" in out
    assert "The access-info 'sr-mpls' was committed successfully." in out
    assert "The existing running-config was not modified." in out
    assert SENTINEL not in out


def test_t03_invalid_definition_fails_and_retains_candidate(lab_root, capsys):
    session = _new_access_info(lab_root)
    session.set_device_field("jump_host", "nope")
    before = _digests(lab_root)
    with pytest.raises(cfgmod.CommitValidationError) as exc:
        session.commit()
    assert SENTINEL not in str(exc.value)
    assert _digests(lab_root) == before
    assert session.definition_dirty()
    assert "R1" in session.definition_candidate["devices"]


def test_t06_newly_selected_missing_reference_fails(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.settings_candidate.setdefault("active_references", []).append("missing-ref")
    with pytest.raises(cfgmod.CommitValidationError) as exc:
        session.commit()
    assert exc.value.errors == ["Reference 'missing-ref' does not exist."]
    assert session.settings_dirty()


def test_t07_delete_active_definition_fails(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition("reference", "sample")
    before = _digests(lab_root)
    with pytest.raises(cfgmod.CommitValidationError) as exc:
        session.commit()
    assert "Cannot remove reference 'sample' because it is active in running-config." in exc.value.errors
    assert _digests(lab_root) == before


def test_t09_create_and_select_in_one_commit(lab_root):
    _break_reference(lab_root, "legacy")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_reference_definition("fresh")
    session.add_reference("fresh")
    assert session.commit() is True
    assert lab.reference_exists("fresh", lab_root)
    assert "fresh" in lab.read_settings(lab_root)["active_references"]
    assert session.last_commit_report.settings_written is True
    assert session.last_commit_report.warnings == ("reference 'legacy'",)


def test_t10_multiple_findings_sorted_and_deduplicated(lab_root):
    for name in ("zeta", "alpha", "mid"):
        _break_reference(lab_root, name)
    session = _new_access_info(lab_root)
    session.commit()
    assert session.last_commit_report.warnings == ("reference 'alpha'", "reference 'mid'", "reference 'zeta'")
    assert list(session.last_commit_report.warnings) == sorted(session.last_commit_report.warnings)
    assert len(set(session.last_commit_report.warnings)) == len(session.last_commit_report.warnings)


def test_t11_new_source_for_already_missing_target_fails(lab_root):
    """Same missing target, different source (a different selection field)."""
    settings = lab.read_settings(lab_root)
    settings["active_scenario"] = "ghost"
    lab.write_settings(settings, lab_root)
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.settings_candidate["active_references"].append("ghost")
    with pytest.raises(cfgmod.CommitValidationError) as exc:
        session.commit()
    assert exc.value.errors == ["Reference 'ghost' does not exist."]


def test_t12_worsened_occurrence_count_fails(lab_root):
    """Same source/field/target, one more occurrence (reference-selection list)."""
    _break_reference(lab_root, "dup")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.settings_candidate["active_references"].append("dup")  # bypasses add_reference's duplicate guard
    with pytest.raises(cfgmod.CommitValidationError):
        session.commit()


def test_t13_resolved_finding_disappears(lab_root):
    _break_reference(lab_root, "gone")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_reference_definition("gone")
    session.commit()
    assert session.last_commit_report.warnings == ()


def test_reorder_is_not_a_new_finding(lab_root):
    _break_reference(lab_root, "gone")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.settings_candidate["active_references"].reverse()
    assert session.commit() is True
    assert session.last_commit_report.warnings == ("reference 'gone'",)


def test_t14_reload_matches_candidate(lab_root):
    session = _new_access_info(lab_root)
    expected = session.definition_candidate
    session.commit()
    assert lab.load_access_info("sr-mpls", lab_root) == expected


def test_t15_t24_failure_keeps_disk_and_unrelated_files(lab_root):
    _break_reference(lab_root, "sample")
    session = _new_access_info(lab_root)
    session.set_device_field("jump_host", "nope")
    before = _digests(lab_root)
    with pytest.raises(cfgmod.CommitValidationError):
        session.commit()
    assert _digests(lab_root) == before


def test_t24_unrelated_files_not_rewritten(lab_root):
    _break_reference(lab_root, "sample")
    before = _digests(lab_root)
    session = _new_access_info(lab_root)
    session.commit()
    after = _digests(lab_root)
    assert {k: v for k, v in after.items() if k in before} == before


def test_t16_write_failure_keeps_candidate(lab_root, monkeypatch):
    session = _new_access_info(lab_root)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(lab.os, "replace", boom)
    with pytest.raises(OSError):
        session.commit()
    assert not lab.access_info_exists("sr-mpls", lab_root)
    assert session.definition_dirty()
    assert session.last_commit_report is None


def test_t19_clean_commit_is_noop(lab_root, capsys, monkeypatch):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    calls = []
    monkeypatch.setattr(lab, "write_settings", lambda *a, **k: calls.append(1))
    before = _digests(lab_root)
    climain.execute_command_line(session, "commit")
    assert "No changes to commit." in capsys.readouterr().out
    assert calls == [] and _digests(lab_root) == before


def test_t31_changed_definition_still_strictly_validated(lab_root):
    """Pre-existing invalid access-info A (unknown jump host, identical before
    and after), unrelated field edited: Stage A still fails the commit.

    load_access_info() itself rejects such a file, so the CLI cannot open it
    for editing; the session state is built directly to prove commit() never
    excuses a persistent defect in the changed definition."""
    bad = {"name": "A", "devices": {"R1": {"type": "iosxr", "jump_host": "ghost"}}}
    path = lab_root / "access-info" / "A.yaml"
    path.write_text(yaml.safe_dump(bad))
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.definition_kind, session.definition_name = "access_info", "A"
    session.definition_original = yaml.safe_load(path.read_text())
    session.definition_candidate = yaml.safe_load(path.read_text())
    session.definition_candidate["devices"]["R1"]["address"] = "192.0.2.77"
    with pytest.raises(cfgmod.CommitValidationError) as exc:
        session.commit()
    assert "unknown jump host 'ghost'" in exc.value.errors[0]
    assert yaml.safe_load(path.read_text()) == bad
    assert session.definition_dirty()


def test_t32_reselect_same_value_is_noop_or_not_reported_as_modified(lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.select_topology(lab.read_settings(lab_root)["active_topology"])
    assert session.commit() is False


def test_t32_settings_write_suppresses_unmodified_line(lab_root, capsys):
    _break_reference(lab_root, "sample")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_reference_definition("newref")
    session.add_reference("newref")
    capsys.readouterr()
    climain.execute_command_line(session, "commit")
    out = capsys.readouterr().out
    assert "The existing running-config was not modified." not in out
    assert "reference 'sample'" in out


def test_t20_no_credential_in_output(lab_root, capsys, caplog):
    _break_reference(lab_root, "sample")
    session = _new_access_info(lab_root)
    capsys.readouterr()
    climain.execute_command_line(session, "commit")
    cap = capsys.readouterr()
    assert SENTINEL not in cap.out + cap.err + caplog.text


def test_t28_delete_inactive_definition_with_unrelated_warning(lab_root, capsys):
    _break_reference(lab_root, "sample")
    lab.write_access_info("spare", {"name": "spare", "devices": {}}, lab_root)
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.remove_definition("access_info", "spare")
    capsys.readouterr()
    climain.execute_command_line(session, "commit")
    out = capsys.readouterr().out
    assert not lab.access_info_exists("spare", lab_root)
    assert "The access-info 'spare' was removed successfully." in out


# ---- T10: warning determinism --------------------------------------------

_T10_NAMES = ("alpha", "sample", "zebra")


def _t10_run(lab_root, order, capsys):
    """Fresh baseline with every name in `order` selected but undefined,
    then commit an unrelated valid access-info. Returns (report, CLI text).
    The CLI text holds no timestamps/paths/session ids, so it is compared
    byte-for-byte."""
    settings = lab.read_settings(lab_root)
    settings["active_references"] = list(order)
    lab.write_settings(settings, lab_root)
    for name in _T10_NAMES:
        path = lab_root / "references" / f"{name}.yaml"
        if path.exists():
            path.unlink()
    session = _new_access_info(lab_root)
    capsys.readouterr()
    climain.execute_command_line(session, "commit")
    cap = capsys.readouterr()
    assert SENTINEL not in cap.out + cap.err
    return session.last_commit_report, cap.out


def _clone_lab(lab_root, tmp_path, tag):
    import shutil

    dst = tmp_path / f"clone-{tag}" / "lab"
    shutil.copytree(lab_root, dst)
    return dst


@pytest.mark.parametrize("order", [("alpha", "sample", "zebra"), ("zebra", "alpha", "sample"), ("sample", "zebra", "alpha")])
def test_t10_a_b_stable_sorted_warnings_independent_of_selection_order(lab_root, capsys, order):
    report, out = _t10_run(lab_root, order, capsys)
    assert report.warnings == ("reference 'alpha'", "reference 'sample'", "reference 'zebra'")
    assert "missing definitions:" in out
    assert out.count("reference '") == 3
    assert "The existing running-config was not modified." in out


def test_t10_c_d_equivalent_independent_runs_byte_identical(lab_root, tmp_path, capsys):
    results = []
    for tag, order in enumerate([("alpha", "sample", "zebra"), ("zebra", "alpha", "sample")] + [("alpha", "sample", "zebra")] * 3):
        root = _clone_lab(lab_root, tmp_path, tag)
        results.append(_t10_run(root, order, capsys))
    assert len({r for r in results}) == 1


def test_t10_noop_second_commit_is_not_a_determinism_substitute(lab_root, capsys):
    _t10_run(lab_root, _T10_NAMES, capsys)
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    capsys.readouterr()
    climain.execute_command_line(session, "commit")
    assert capsys.readouterr().out.strip() == "No changes to commit."


# ---- T26: finding identity ------------------------------------------------


def _findings(lab_root, settings, existing=()):
    session = cfgmod.CliSession(lab_root)
    return session._running_config_findings(settings, lambda kind, name: (kind, name) in existing)


def test_t26_same_source_different_field_same_target_name_are_distinct(lab_root):
    """running-config is one source; scenario/topology/reference selections
    are different fields. A missing 'ghost' in each is three findings."""
    f = _findings(lab_root, {"active_topology": "ghost", "active_scenario": "ghost", "active_references": ["ghost"]})
    assert len(f) == 3 and set(f.values()) == {1}
    assert {k[2] for k in f} == {"active_topology", "active_scenario", "active_references"}


def test_t26_occurrence_count_and_order_independence(lab_root):
    a = _findings(lab_root, {"active_references": ["x", "y"]})
    b = _findings(lab_root, {"active_references": ["y", "x"]})
    assert a == b
    assert _findings(lab_root, {"active_references": ["x", "x"]})[("running-config", "", "active_references", "reference", "x")] == 2


def test_t26_new_field_with_same_missing_target_fails_commit(lab_root):
    settings = lab.read_settings(lab_root)
    settings["active_topology"] = "ghost"  # baseline: topology 'ghost' missing
    lab.write_settings(settings, lab_root)
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.settings_candidate["active_scenario"] = "ghost"  # new field, same target name
    with pytest.raises(cfgmod.CommitValidationError) as exc:
        session.commit()
    assert exc.value.errors == ["Scenario 'ghost' does not exist."]


def test_t26_resolved_and_persistent_mixed(lab_root):
    _break_reference(lab_root, "keep")
    _break_reference(lab_root, "fixme")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_reference_definition("fixme")
    session.commit()
    assert session.last_commit_report.warnings == ("reference 'keep'",)


def test_t26_other_reference_kinds_not_modelled():
    """Access-info jump-host/device references are internal to one definition
    and covered by Stage A (validate_device_jump_host_references); no other
    cross-definition reference field exists in the schema, so same-source
    multi-field identity is only reachable via running-config selections."""
