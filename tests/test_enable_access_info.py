# SPDX-License-Identifier: Apache-2.0
"""enable-password in access-info: CLI grammar, candidate/commit/clear, YAML,
validation, abbreviation, history masking, topology isolation (A-series IDs in
test names). Everything runs against the isolated `lab_root` fixture; credentials
are synthetic."""

from __future__ import annotations

import pytest
import yaml

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain

EPW = "SENT-EPW-5e1c"
LPW = "SENT-LPW-77aa"


def _session(lab_root, device="R9"):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    climain.execute_input_block(session, f"device {device}\n type iosxe\n")
    return session


def _dev(session, name="R9"):
    return session.definition_candidate["devices"][name]


def _run(session, line):
    return climain.execute_command_line(session, line)


def test_a01_a02_a03_a04_set_change_remove_independent(lab_root):
    s = _session(lab_root)
    _run(s, f"password {LPW}")
    _run(s, f"enable-password {EPW}")
    assert _dev(s)["enable_password"] == EPW and _dev(s)["password"] == LPW
    _run(s, "enable-password SENT-EPW-other")
    assert _dev(s)["enable_password"] == "SENT-EPW-other" and _dev(s)["password"] == LPW
    _run(s, "no enable-password")
    assert "enable_password" not in _dev(s) and _dev(s)["password"] == LPW
    _run(s, f"enable-password {EPW}")
    _run(s, "no password")
    assert "password" not in _dev(s) and _dev(s)["enable_password"] == EPW


def test_a05_a06_a07_commit_reload_clear(lab_root):
    s = _session(lab_root)
    _run(s, f"enable-password {EPW}")
    _run(s, "clear")
    assert "enable_password" not in s.definition_candidate["devices"].get("R9", {})
    climain.execute_input_block(s, f"device R9\n type iosxe\n enable-password {EPW}\n")
    assert s.commit()
    reloaded = lab.load_access_info("sample_lab", lab_root)
    assert reloaded["devices"]["R9"]["enable_password"] == EPW


def test_a08_yaml_without_enable_password_still_loads(lab_root):
    data = lab.load_access_info("sample_lab", lab_root)
    assert all("enable_password" not in (d or {}) for d in data["devices"].values())


@pytest.mark.parametrize("bad", [123, True, "", "a\x07b", "line\nbreak", ["x"]])
def test_a09_validation_rejects_bad_values(lab_root, bad):
    data = lab.load_access_info("sample_lab", lab_root)
    data["devices"]["R9"] = {"type": "iosxe", "enable_password": bad}
    with pytest.raises(lab.LabConfigError) as info:
        lab.validate_access_info_data("sample_lab", data)
    assert "invalid enable_password" in str(info.value) and EPW not in str(info.value)


def test_a09_cli_rejects_control_characters(lab_root):
    out = grammar.parse("access_device", "enable-password bad\x07value")
    assert not out.ok


@pytest.mark.parametrize("value", ["P@ss'w\"rd#1", "pässwörd-日本語", "a!b$c%d&e"])
def test_a10_special_and_unicode(lab_root, value):
    s = _session(lab_root)
    climain.execute_input_block(s, f"enable-password {value}\n")
    assert _dev(s)["enable_password"] == value
    assert s.commit()
    assert lab.load_access_info("sample_lab", lab_root)["devices"]["R9"]["enable_password"] == value


def test_a11_a12_a13_show_renders_enable_password(lab_root):
    s = _session(lab_root)
    _run(s, f"password {LPW}")
    _run(s, f"enable-password {EPW}")
    lines = climain.render_access_info_block(s.definition_candidate)
    assert f"  password {LPW}" in lines and f"  enable-password {EPW}" in lines
    assert lines.index(f"  password {LPW}") + 1 == lines.index(f"  enable-password {EPW}")


def test_a14_a15_help_and_no_help():
    ctx = grammar.CliContext()
    top = grammar.help("access_device", "", ctx)
    assert ("enable-password", "Set the device privileged EXEC password") in [(h.token, h.description) for h in top.lines]
    no = grammar.help("access_device", "no ", ctx)
    assert any("enable-password" in h.token for h in no.lines)
    arg = grammar.help("access_device", "enable-password ", ctx)
    assert [h.token for h in arg.lines] == ["<enable-password>"]


def test_a16_completion_offers_keyword_never_values():
    ctx = grammar.CliContext()
    result = grammar.complete("access_device", "enable-p", ctx)
    assert "enable-password" in result.candidates
    values = grammar.complete("access_device", "enable-password S", ctx)
    assert values.candidates == []


@pytest.mark.parametrize(
    "text,ok,action",
    [
        ("end", True, "access_device.end"),
        ("enable-password X", True, "access_device.set_enable_password"),
        ("ena X", True, "access_device.set_enable_password"),
        ("enable-p X", True, "access_device.set_enable_password"),
        ("en X", False, None),
        ("e X", False, None),
        ("no enable-password", True, "access_device.clear_enable_password"),
    ],
)
def test_a17_a21_a23_abbreviation_resolution(text, ok, action):
    result = grammar.parse("access_device", text)
    assert result.ok is ok
    if ok:
        assert result.action == action
    else:
        assert result.error.kind == "ambiguous"


def test_a22_ambiguous_abbreviation_does_not_change_candidate(lab_root):
    s = _session(lab_root)
    before = dict(_dev(s))
    _run(s, f"en {EPW}")
    assert _dev(s) == before and s.mode == "access_device"


def test_a18_a29_multiline_paste_set_change_remove(lab_root):
    s = _session(lab_root)
    climain.execute_input_block(s, f"enable-password {EPW}\npassword {LPW}\nenable-password SENT-EPW-2\n")
    assert _dev(s)["enable_password"] == "SENT-EPW-2" and _dev(s)["password"] == LPW
    climain.execute_input_block(s, "no enable-password\n")
    assert "enable_password" not in _dev(s)


def test_a19_jump_host_has_no_enable_password():
    assert grammar.parse("access_jump_host", "enable-password X").ok is False
    assert "enable_password" not in cfgmod.JUMP_HOST_FIELD_ORDER


@pytest.mark.parametrize(
    "line",
    [f"enable-password {EPW}", f"enable-p {EPW}", f"ena {EPW}", f"en {EPW}", f"e {EPW}", f"enable-password {EPW} extra", f"enable-passw\x07 {EPW}", f"password {LPW}"],
)
def test_a20_a24_a25_a36_a37_history_never_keeps_credentials(line):
    history = climain.MaskingHistory()
    history.append_string(line)
    history.append_string("show\n" if False else "show")
    stored = list(history.get_strings())
    assert stored == ["show"]
    block = climain.MaskingHistory()
    block.append_string(f"device R9\n {line}\n exit")
    assert not any(EPW in s or LPW in s for s in block.get_strings())


def test_a20_no_enable_password_is_recallable_and_value_free():
    history = climain.MaskingHistory()
    history.append_string("no enable-password")
    assert list(history.get_strings()) == ["no enable-password"]


def test_a26_a27_a28_type_independent_storage(lab_root):
    s = _session(lab_root)
    _run(s, f"enable-password {EPW}")
    _run(s, "type iosxr")
    assert _dev(s)["enable_password"] == EPW  # not removed on type change
    _run(s, "no type")
    assert _dev(s)["enable_password"] == EPW
    assert s.commit()  # unset / non-ios type still stores and commits
    assert lab.load_access_info("sample_lab", lab_root)["devices"]["R9"]["enable_password"] == EPW


@pytest.mark.parametrize("field", ["enable_password", "enable-password"])
def test_a30_s01_s32_s33_topology_rejects_enable_password(field):
    with pytest.raises(lab.LabConfigError):
        lab.validate_topology_no_access_fields("t", {"R1": {"type": "iosxe", field: EPW}})


def test_s01_committed_topology_carries_no_enable_password(lab_root):
    s = _session(lab_root)
    _run(s, f"enable-password {EPW}")
    s.commit()
    topo_text = (lab_root / "topologies" / "sample_lab.yaml").read_text()
    assert EPW not in topo_text
    assert EPW in (lab_root / "access-info" / "sample_lab.yaml").read_text()
    assert EPW not in yaml.safe_dump(lab.load_topology("sample_lab", lab_root))
