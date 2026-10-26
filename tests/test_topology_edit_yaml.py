# SPDX-License-Identifier: Apache-2.0
"""Topology `edit` presentation: each link is a one-line flow mapping, top-level
sections are separated by one blank line, and nothing about the data model,
the committed-file serializer, or other definitions' edit text changes."""

from __future__ import annotations

import copy

import pytest
import yaml

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import editor
from network_lab_mcp.cli import main as climain

LONG = "GigabitEthernet0/0/0/10"
TOPO = {
    "name": "t1",
    "description": "",
    "devices": {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}},
    "links": [
        {"a": "R1", "a_interface": "TenGigE0/0/0/0", "b": "R2", "b_interface": "TenGigE0/0/0/0"},
        {"a": "R1", "a_interface": LONG, "b": "R2", "b_interface": LONG},
    ],
}


def dump(data):
    return editor.dump_topology_for_edit(data)


def test_links_are_one_line_flow_mappings_over_85_chars():
    data = copy.deepcopy(TOPO)
    data["links"].append({"a": "R1", "a_interface": "x" * 200, "b": "R2", "b_interface": "y" * 200})
    lines = dump(data).split("\n")
    link_lines = lines[lines.index("links:") + 1 : -1]
    assert len(link_lines) == 3
    assert any(len(line) > 85 for line in link_lines)
    assert link_lines[1] == f"  - {{a: R1, a_interface: {LONG}, b: R2, b_interface: {LONG}}}"
    assert all(line.startswith("  - {") and line.endswith("}") for line in link_lines)


def test_layout_blank_lines_indent_and_trailing_newline():
    assert dump(TOPO) == (
        "name: t1\n\n"
        "description: ''\n\n"
        "devices:\n  R1:\n    type: iosxr\n  R2:\n    type: iosxr\n\n"
        "links:\n"
        "  - {a: R1, a_interface: TenGigE0/0/0/0, b: R2, b_interface: TenGigE0/0/0/0}\n"
        f"  - {{a: R1, a_interface: {LONG}, b: R2, b_interface: {LONG}}}\n"
    )
    assert dump(TOPO).endswith("}\n") and not dump(TOPO).endswith("\n\n")


def test_extra_top_level_keys_also_get_one_blank_line():
    data = {**copy.deepcopy(TOPO), "extra": {"k": "v"}}
    text = dump(data)
    assert "\n\nextra:\n  k: v\n" in text
    assert list(yaml.safe_load(text)) == list(data)


def test_roundtrip_special_strings_and_key_order():
    links = [
        {
            "b_interface": "a, b [c] {d}: e",
            "a": "R 1",
            "a_interface": "it's \"q\" 日本語 #x",
            "b": "R2",
            "note": "first line\nsecond line\n",
        },
        {"a": "R3", "b": "R4", "a_interface": "- x", "b_interface": "*y &z ! % @"},
    ]
    data = {"name": "t", "description": "d", "devices": {"R1": {"type": "iosxr"}}, "links": links}
    original = copy.deepcopy(data)
    text = dump(data)
    parsed = yaml.safe_load(text)
    assert parsed == original and data == original
    assert [list(link) for link in parsed["links"]] == [list(link) for link in links]
    link_lines = text.split("links:\n")[1].splitlines()
    assert len(link_lines) == 2  # embedded newline stays escaped: still one physical line
    assert "&" not in text.replace("&z", "") and "*" not in text.replace("*y", "")


def test_empty_and_missing_sections():
    assert dump({"name": "t", "description": "", "devices": {}, "links": []}) == (
        "name: t\n\ndescription: ''\n\ndevices: {}\n\nlinks: []\n"
    )
    text = dump({"name": "t", "devices": {"R1": {"type": "iosxr"}}})
    assert "links" not in text


def test_optional_link_fields_kept():
    data = {"name": "t", "links": [{"a": "R1", "a_interface": "Gi0/0", "b": "R2", "b_interface": "Gi0/1", "description": "test"}]}
    assert "  - {a: R1, a_interface: Gi0/0, b: R2, b_interface: Gi0/1, description: test}\n" in dump(data)


def test_candidate_not_mutated_and_no_anchors():
    shared = {"a": "R1", "b": "R2"}
    data = {"name": "t", "links": [shared, copy.deepcopy(shared)]}
    before = copy.deepcopy(data)
    text = dump(data)
    assert data == before and type(data["links"][0]) is dict and type(data["links"][0]["a"]) is str
    assert "&id" not in text and "*id" not in text


def test_other_sections_keep_default_width():
    long_desc = "word " * 40
    data = {"name": "t", "description": long_desc, "links": []}
    ref = yaml.safe_dump({"description": long_desc}, sort_keys=False, default_flow_style=False, allow_unicode=True)
    assert ref in dump(data)


# ---- CLI integration ----


def _session(root, *cmds):
    s = cfgmod.CliSession(root)
    s.enter_configure()
    for c in cmds:
        assert climain.execute_command_line(s, c)
    return s


def _use_editor(monkeypatch, fake_editor, body):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", f"python3 {fake_editor(body)}")


@pytest.fixture()
def root(lab_root):
    with (lab_root / "topologies" / "t1.yaml").open("w", encoding="utf-8") as h:
        yaml.safe_dump(TOPO, h, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return lab_root


def _edit(s, monkeypatch, fake_editor, body):
    _use_editor(monkeypatch, fake_editor, body)
    assert climain.execute_command_line(s, "edit")


def test_editor_receives_flow_text(root, monkeypatch, fake_editor, tmp_path):
    cap = tmp_path / "captured.yaml"
    s = _session(root, "topology t1")
    _edit(s, monkeypatch, fake_editor, f"import shutil; shutil.copy(path, {str(cap)!r})")
    assert cap.read_text() == dump(TOPO)


def test_add_remove_modify_and_mixed_syntax(root, monkeypatch, fake_editor):
    s = _session(root, "topology t1")
    body = (
        "open(path, 'w').write('''name: t1\ndescription: ''\ndevices:\n  R1: {type: iosxr}\n  R2: {type: iosxr}\n"
        "links:\n"
        "  - {a: R1, a_interface: Gi0/0, b: R2, b_interface: Gi0/1}\n"
        "  - a: R2\n    a_interface: Gi0/2\n    b: R1\n    b_interface: Gi0/3\n''')"
    )
    _edit(s, monkeypatch, fake_editor, body)
    assert s.definition_dirty()
    assert s.definition_candidate["links"] == [
        {"a": "R1", "a_interface": "Gi0/0", "b": "R2", "b_interface": "Gi0/1"},
        {"a": "R2", "a_interface": "Gi0/2", "b": "R1", "b_interface": "Gi0/3"},
    ]
    assert type(s.definition_candidate["links"][0]) is dict


def test_invalid_yaml_and_invalid_schema_leave_candidate_and_committed(root, monkeypatch, fake_editor, capsys):
    path = root / "topologies" / "t1.yaml"
    before = path.read_bytes()
    s = _session(root, "topology t1")
    _edit(s, monkeypatch, fake_editor, "open(path, 'w').write('links: [{a: R1\\n')")
    assert "% YAML validation failed" in capsys.readouterr().out
    assert not s.definition_dirty() and s.definition_candidate == TOPO
    _use_editor(monkeypatch, fake_editor, "open(path, 'w').write('name: t1\\nlinks: nope\\n')")
    assert not climain.execute_command_line(s, "edit")
    assert "invalid 'links' section" in capsys.readouterr().out
    assert not s.definition_dirty() and s.definition_candidate == TOPO
    assert path.read_bytes() == before


def test_cancel_leaves_candidate_clean(root, monkeypatch, fake_editor, capsys):
    s = _session(root, "topology t1")
    _use_editor(monkeypatch, fake_editor, "import sys; sys.exit(1)")
    climain.execute_command_line(s, "edit")
    assert not s.definition_dirty() and s.definition_candidate == TOPO


def test_unchanged_save_keeps_state_and_commit_bytes_identical(root, monkeypatch, fake_editor):
    s = _session(root, "topology t1")
    rev = s._mutation_rev
    snapshot = copy.deepcopy(s.definition_candidate)
    _edit(s, monkeypatch, fake_editor, "pass")
    assert not s.definition_dirty() and s._mutation_rev == rev
    assert s.definition_candidate == snapshot
    assert [list(link) for link in s.definition_candidate["links"]] == [list(link) for link in snapshot["links"]]
    assert type(s.definition_candidate) is dict and all(type(x) is dict for x in s.definition_candidate["links"])

    # Case 1 (no edit) vs Case 2 (edit, unchanged): force a commit by touching the description
    # in both, since an unchanged candidate is a no-op commit.
    def committed(use_edit, tmp):
        sess = _session(root, "topology t1")
        if use_edit:
            _edit(sess, monkeypatch, fake_editor, "pass")
        sess.set_topology_description("same")
        assert climain.execute_command_line(sess, "commit")
        data = (root / "topologies" / "t1.yaml").read_bytes()
        (root / "topologies" / "t1.yaml").write_bytes(tmp)
        return data

    original = (root / "topologies" / "t1.yaml").read_bytes()
    assert committed(False, original) == committed(True, original)


def test_commit_format_golden():
    # Golden: the committed-file serializer is unchanged (block style, safe_dump defaults).
    assert yaml.safe_dump(TOPO, sort_keys=False, default_flow_style=False, allow_unicode=True) == (
        "name: t1\ndescription: ''\ndevices:\n  R1:\n    type: iosxr\n  R2:\n    type: iosxr\nlinks:\n"
        "- a: R1\n  a_interface: TenGigE0/0/0/0\n  b: R2\n  b_interface: TenGigE0/0/0/0\n"
        f"- a: R1\n  a_interface: {LONG}\n  b: R2\n  b_interface: {LONG}\n"
    )


def test_commit_writes_block_style_even_after_flow_edit(root, monkeypatch, fake_editor):
    s = _session(root, "topology t1")
    _edit(s, monkeypatch, fake_editor, "import yaml\nd = yaml.safe_load(open(path)); d['description'] = 'x'\nyaml.safe_dump(d, open(path, 'w'), sort_keys=False)")
    assert climain.execute_command_line(s, "commit")
    text = (root / "topologies" / "t1.yaml").read_text()
    assert "- a: R1\n  a_interface: TenGigE0/0/0/0\n" in text and "{" not in text
    assert lab.load_topology("t1", root)["links"] == TOPO["links"]


def test_show_running_config_unchanged_by_edit(root, monkeypatch, fake_editor):
    s = _session(root, "topology t1")
    before = climain.render_configuration_candidate(s)
    _edit(s, monkeypatch, fake_editor, "pass")
    assert climain.render_configuration_candidate(s) == before


def test_scenario_edit_text_unchanged(root, monkeypatch, fake_editor, tmp_path):
    scen = {"name": "s1", "description": "word " * 40, "steps": [{"a": 1}, {"b": 2}]}
    with (root / "scenarios" / "s1.yaml").open("w", encoding="utf-8") as h:
        yaml.safe_dump(scen, h, sort_keys=False)
    cap = tmp_path / "captured.yaml"
    s = _session(root, "scenario s1")
    _edit(s, monkeypatch, fake_editor, f"import shutil; shutil.copy(path, {str(cap)!r})")
    assert cap.read_text() == yaml.safe_dump(scen, sort_keys=False, default_flow_style=False, allow_unicode=True)
