# SPDX-License-Identifier: Apache-2.0
"""`show configuration` -> paste replay consistency for the running-config
selection delta.

Replay is guaranteed from the context the output was taken in for GLOBAL,
RUNNING and the name-less Common submodes (`config-running-<kind>`, whose
output is kind-scoped: only that kind's `<name>` / `no <name>` lines). The
output is re-pasted into a Fresh Session on the same committed baseline.
Description edits render through the named submode (`<kind> <name>` /
`description <text>` / `exit`). The named submodes keep their existing scope.

Topology/scenario have no running-level `no`: clearing one renders as a
block entering the Common submode and naming the COMMITTED selection
(`no <name>`), never the old, unreplayable `no topology`.

All tests use the isolated `lab_root` fixture."""

from __future__ import annotations

import copy
import shutil

import pytest
import yaml

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain

EXTRA = {
    "topologies": ("sample_lab", ["topo_b"]),
    "scenarios": ("sample", ["scen_b"]),
    "references": ("sample", ["ref_b", "ref_c"]),
    "access-info": ("sample_lab", ["ai_b"]),
}
KEYWORD_NAMES = ["topology", "scenario", "reference", "access-info", "show", "exit", "no", "description"]


def _clone(root, folder, src, dst):
    shutil.copy(root / folder / f"{src}.yaml", root / folder / f"{dst}.yaml")
    path = root / folder / f"{dst}.yaml"
    data = yaml.safe_load(path.read_text())
    data["name"] = dst
    path.write_text(yaml.safe_dump(data, sort_keys=False))


@pytest.fixture()
def root(lab_root):
    for folder, (src, names) in EXTRA.items():
        for name in names:
            _clone(lab_root, folder, src, name)
    return lab_root


def _fresh(root, *cmds):
    s = cfgmod.CliSession(root)
    s.enter_configure()
    for cmd in cmds:
        assert climain.execute_command_line(s, cmd)
    return s


def _paste(s, text, capsys):
    capsys.readouterr()
    climain.execute_input_block(s, text)
    return capsys.readouterr().out


def _run(s, text, capsys):
    return _paste(s, text, capsys)


def _state(s):
    return (
        s.mode,
        copy.deepcopy(s.settings_candidate),
        s.definition_kind,
        s.definition_name,
        copy.deepcopy(s.definition_candidate),
        s.settings_dirty(),
        s.overall_dirty(),
        copy.deepcopy(s.committed_settings),
    )


def _to(s, ctx):
    """Navigate an existing session to `global` or `running`."""
    while s.mode != "global":
        assert climain.execute_command_line(s, "exit")
    if ctx == "running":
        assert climain.execute_command_line(s, "running-config")


# (kind, operation) -> running-mode setup script (fresh global session)
OPS = {
    ("access-info", "select"): "running-config\nno access-info\naccess-info ai_b\nexit",
    ("access-info", "switch"): "running-config\naccess-info ai_b\nexit",
    ("access-info", "unset"): "running-config\nno access-info",
    ("access-info", "reselect"): "running-config\nno access-info\naccess-info sample_lab",
    ("access-info", "netzero"): "running-config\nno access-info\naccess-info sample_lab\nexit",
    ("topology", "switch"): "running-config\ntopology topo_b\nexit",
    ("topology", "unset"): "running-config\ntopology\nno sample_lab",
    ("topology", "reselect"): "running-config\ntopology\nno sample_lab\nsample_lab",
    ("topology", "unset_select"): "running-config\ntopology\nno sample_lab\ntopo_b",
    ("topology", "netzero"): "running-config\ntopology topo_b\nexit\ntopology sample_lab\nexit",
    ("scenario", "switch"): "running-config\nscenario scen_b\nexit",
    ("scenario", "unset"): "running-config\nscenario\nno sample",
    ("scenario", "reselect"): "running-config\nscenario\nno sample\nsample",
    ("scenario", "netzero"): "running-config\nscenario scen_b\nexit\nscenario sample\nexit",
    ("reference", "select"): "running-config\nreference ref_b\nexit",
    ("reference", "switch"): "running-config\nno reference sample\nreference ref_b\nexit",
    ("reference", "unset"): "running-config\nno reference sample",
    ("reference", "reselect"): "running-config\nno reference sample\nreference sample\nexit",
    ("reference", "netzero"): "running-config\nreference ref_b\nexit\nno reference ref_b\nexit",
    ("reference", "order"): "running-config\nreference ref_c\nreference ref_b\nexit",
}


@pytest.mark.parametrize("ctx", ["running", "global"])
@pytest.mark.parametrize("op", list(OPS), ids=lambda o: f"{o[0]}-{o[1]}")
def test_round_trip_matrix(root, capsys, op, ctx):
    src = _fresh(root)
    _run(src, OPS[op], capsys)
    _to(src, ctx)
    text = climain.render_configuration_candidate(src)
    committed_bytes = (root / "settings.yaml").read_bytes()

    dst = _fresh(root, *(["running-config"] if ctx == "running" else []))
    out = _paste(dst, text, capsys) if text else ""
    assert out == "", out
    assert dst.mode == ctx  # structural `!`/`exit` returns to the output's own context
    assert dst.settings_candidate == src.settings_candidate
    assert dst.settings_dirty() == src.settings_dirty()
    assert dst.committed_settings == src.committed_settings
    assert (root / "settings.yaml").read_bytes() == committed_bytes
    if op[1] in ("netzero", "reselect"):
        assert text == "" and not dst.settings_dirty()
    # re-rendering the replayed session reproduces the same text
    assert climain.render_configuration_candidate(dst) == text


@pytest.mark.parametrize("kw,name", [("topology", "sample_lab"), ("scenario", "sample")])
def test_unset_forms_per_context(root, capsys, kw, name):
    s = _fresh(root, "running-config", kw, f"no {name}")
    assert climain.render_configuration_candidate(s) == f"no {name}"  # Common: kind-scoped
    _paste(s, "exit", capsys)
    assert climain.render_configuration_candidate(s) == f"!\n {kw}\n  no {name}\n!"  # Running
    _paste(s, "exit", capsys)
    assert climain.render_configuration_candidate(s) == f"running-config\n {kw}\n  no {name}\n !\nexit"  # Global


def test_unset_name_comes_from_committed_settings(root, capsys):
    s = _fresh(root, "running-config", "topology", "topo_b", "no topo_b")
    # switched away and then unset again: the COMMITTED name is what is removed
    assert climain.render_configuration_candidate(s) == "no sample_lab"


def test_unset_topology_still_cannot_commit_but_reselect_can(root, capsys):
    s = _fresh(root, "running-config", "topology", "no sample_lab")
    before = (root / "settings.yaml").read_bytes()
    out = _run(s, "commit", capsys)
    assert "missing a valid active topology" in out
    assert (root / "settings.yaml").read_bytes() == before


# ------------------------------------------------------ structural bang


CROSS = "running-config\ntopology\nno sample_lab\nexit\nscenario scen_b\nexit\nreference ref_b\nexit\nno reference sample\nno access-info\nexit"


@pytest.mark.parametrize("ctx", ["running", "global"])
def test_cross_kind_candidate_replays_in_every_scope(root, capsys, ctx):
    src = _fresh(root)
    _run(src, CROSS, capsys)
    _to(src, ctx)
    text = climain.render_configuration_candidate(src)
    for fragment in ("no sample_lab", "scen_b", "ref_b", "no reference sample", "no access-info"):
        assert fragment in text  # cross-kind: the whole running delta is shown
    dst = _fresh(root, *(["running-config"] if ctx == "running" else []))
    assert _paste(dst, text, capsys) == ""
    assert _state(dst) == _state(src)


def test_global_output_followed_by_definition_deletion_runs_at_global(root, capsys):
    """A running-section ending in a block must not leave the session in
    running mode: the following `no reference sample` is the DEFINITION
    deletion, not a selection removal."""
    src = _fresh(root)
    _run(src, "running-config\ntopology\nno sample_lab\nexit\nexit\nno reference ref_c", capsys)
    assert src.mode == "global"
    text = climain.render_configuration_candidate(src)
    assert text.endswith("exit\nno reference ref_c") or "no reference ref_c" in text.splitlines()[-1]
    dst = _fresh(root)
    assert _paste(dst, text, capsys) == ""
    assert dst.mode == "global"
    assert _state(dst) == _state(src)
    assert lab.reference_exists("ref_c", root)  # nothing committed/deleted


def test_global_selection_unset_plus_definition_delete_of_same_reference(root, capsys):
    s = _fresh(root, "running-config", "no reference sample", "exit")
    _run(s, "no reference sample", capsys)
    text = climain.render_configuration_candidate(s)
    assert text == "running-config\n no reference sample\n!\nno reference sample"
    dst = _fresh(root)
    assert _paste(dst, text, capsys) == ""
    assert _state(dst) == _state(s)


def test_global_access_info_body_edit_with_selection_change_replays(root, capsys):
    """Global composite: running selection change + a definition body edit.
    access-info definition blocks close with `!`, so full-state equality holds."""
    src = _fresh(root)
    _run(src, "running-config\ntopology\nno sample_lab\nexit\nexit\naccess-info sample_lab\ndevice R2\naddress 192.0.2.99\nexit\nexit", capsys)
    assert src.mode == "global" and src.definition_kind == "access_info"
    text = climain.render_configuration_candidate(src)
    assert "no sample_lab" in text and "192.0.2.99" in text
    dst = _fresh(root)
    assert _paste(dst, text, capsys) == ""
    assert _state(dst) == _state(src)
    assert lab.load_access_info("sample_lab", root)["devices"]["R2"]["address"] == "192.0.2.12"  # committed untouched


def test_global_topology_body_edit_replay_restores_global(root, capsys):
    """Topology device/definition modes close with a structural `!` like
    access-info, so the replay returns to global and a following global line
    (here a definition deletion) is not read inside the definition mode."""
    src = _fresh(root)
    _run(src, "running-config\ntopology topo_b\nexit\nexit\ntopology sample_lab\ndescription changed body\ndevice R3\ntype iosxr\nexit\ndevice R1\ntype iosxe\nexit\nexit", capsys)
    text = climain.render_configuration_candidate(src)
    assert "topo_b" in text and "changed body" in text and "device R3" in text and "device R1" in text
    dst = _fresh(root)
    assert _paste(dst, text, capsys) == ""
    assert _state(dst) == _state(src)
    assert dst.mode == "global"
    assert lab.load_topology("sample_lab", root).get("description") != "changed body"
    # a following Global command runs at global: it is the definition deletion
    # (and is refused/handled by the existing single-dirty-definition rule), never
    # a device/topology-mode line
    assert _paste(dst, "running-config\nexit", capsys) == ""
    assert dst.mode == "global"


@pytest.mark.parametrize(
    "tail",
    [
        "scenario scen_b\nexit",  # running-level line after an unset block
        "reference ref_b\nexit",
        "no access-info",
    ],
)
def test_block_followed_by_other_sections_pastes_in_running(root, capsys, tail):
    src = _fresh(root)
    _run(src, f"running-config\ntopology\nno sample_lab\nexit\n{tail}", capsys)
    _to(src, "running")
    text = climain.render_configuration_candidate(src)
    dst = _fresh(root, "running-config")
    assert _paste(dst, text, capsys) == ""
    assert _state(dst) == _state(src)


@pytest.mark.parametrize("lead", ["!\n", "!\n!\n", ""])
@pytest.mark.parametrize("trail", ["", "\n!", "\n!\n!"])
def test_leading_and_trailing_bangs_are_safe_in_running(root, capsys, lead, trail):
    src = _fresh(root)
    _run(src, "running-config\ntopology\nno sample_lab", capsys)
    _to(src, "running")
    text = climain.render_configuration_candidate(src)
    dst = _fresh(root, "running-config")
    _paste(dst, lead + text + trail, capsys)
    assert dst.settings_candidate == src.settings_candidate
    assert dst.committed_settings == src.committed_settings


# ----------------------------------------------------- keyword collisions


@pytest.mark.parametrize("name", KEYWORD_NAMES)
@pytest.mark.parametrize("selected", [True, False])
def test_keyword_named_topology_unset_replays_without_drift(root, capsys, name, selected):
    """A topology literally named like a CLI keyword: committed-selected
    (unset via `no <name>`) or unselected (stays untouched)."""
    _clone(root, "topologies", "sample_lab", name)
    if selected:
        data = lab.read_settings(root)
        data["active_topology"] = name
        (root / "settings.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    src = _fresh(root)
    active = lab.read_settings(root)["active_topology"]
    _run(src, f"running-config\ntopology\nno {active}\nexit\nscenario scen_b\nexit", capsys)
    assert "active_topology" not in src.settings_candidate
    _to(src, "running")
    text = climain.render_configuration_candidate(src)
    assert f"  no {active}\n" in text
    dst = _fresh(root, "running-config")
    assert _paste(dst, text, capsys) == ""
    assert _state(dst) == _state(src)
    assert lab.topology_exists(name, root)  # no definition side effect
    assert (dst.settings_candidate.get("active_scenario"), dst.mode) == ("scen_b", "running")


@pytest.mark.parametrize("name", KEYWORD_NAMES)
def test_keyword_named_scenario_unset_replays_without_drift(root, capsys, name):
    _clone(root, "scenarios", "sample", name)
    data = lab.read_settings(root)
    data["active_scenario"] = name
    (root / "settings.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    src = _fresh(root)
    _run(src, f"running-config\nscenario\nno {name}\nexit\ntopology topo_b\nexit", capsys)
    _to(src, "running")
    text = climain.render_configuration_candidate(src)
    dst = _fresh(root, "running-config")
    assert _paste(dst, text, capsys) == ""
    assert _state(dst) == _state(src)
    assert lab.scenario_exists(name, root)


# ----------------------------------------------- scope / unspecified contexts


# ------------------------------------------------ Common submodes: kind-scoped

COMMON_MODE = {"access-info": "running_access_info_common", "topology": "running_topology_common", "scenario": "running_scenario_common", "reference": "running_reference_common"}
COMMON_OPS = {
    ("access-info", "select"): ("running-config\nno access-info\naccess-info ai_b\nexit", "ai_b"),
    ("access-info", "switch"): ("running-config\naccess-info ai_b\nexit", "ai_b"),
    ("access-info", "unset"): ("running-config\nno access-info", "no sample_lab"),
    ("access-info", "reselect"): ("running-config\nno access-info\naccess-info sample_lab\nexit", ""),
    ("topology", "switch"): ("running-config\ntopology topo_b\nexit", "topo_b"),
    ("topology", "unset"): ("running-config\ntopology\nno sample_lab", "no sample_lab"),
    ("topology", "unset_select"): ("running-config\ntopology\nno sample_lab\ntopo_b", "topo_b"),
    ("topology", "netzero"): ("running-config\ntopology topo_b\nexit\ntopology sample_lab\nexit", ""),
    ("scenario", "switch"): ("running-config\nscenario scen_b\nexit", "scen_b"),
    ("scenario", "unset"): ("running-config\nscenario\nno sample", "no sample"),
    ("scenario", "netzero"): ("running-config\nscenario scen_b\nexit\nscenario sample\nexit", ""),
    ("reference", "select"): ("running-config\nreference ref_b\nexit", "ref_b"),
    ("reference", "switch"): ("running-config\nno reference sample\nreference ref_b\nexit", "no sample\nref_b"),
    ("reference", "unset"): ("running-config\nno reference sample", "no sample"),
    ("reference", "reselect"): ("running-config\nno reference sample\nreference sample\nexit", ""),
    ("reference", "order"): ("running-config\nreference ref_c\nexit\nreference ref_b\nexit", "ref_c\nref_b"),
    ("reference", "netzero"): ("running-config\nreference ref_b\nexit\nno reference ref_b\nexit", ""),
}


@pytest.mark.parametrize("op", list(COMMON_OPS), ids=lambda o: f"{o[0]}-{o[1]}")
def test_common_output_is_kind_scoped_and_replays_in_the_same_common_submode(root, capsys, op):
    kind = op[0]
    setup, expected = COMMON_OPS[op]
    src = _fresh(root)
    _run(src, setup, capsys)
    _to(src, "running")
    assert climain.execute_command_line(src, kind)  # enter the Common submode
    assert src.mode == COMMON_MODE[kind]
    text = climain.render_configuration_candidate(src)
    assert text == expected
    dst = _fresh(root, "running-config", kind)
    assert (_paste(dst, text, capsys) if text else "") == ""
    assert dst.mode == COMMON_MODE[kind]  # stays in the Common submode
    assert dst.settings_candidate == src.settings_candidate
    assert dst.settings_dirty() == src.settings_dirty()
    assert dst.committed_settings == src.committed_settings
    assert climain.render_configuration_candidate(dst) == text


def test_cross_kind_candidate_each_common_submode_shows_only_its_own_kind(root, capsys):
    base = _fresh(root)
    _run(base, "running-config\nno access-info\naccess-info ai_b\nexit\ntopology\nno sample_lab\nexit\nscenario scen_b\nexit\nreference ref_b\nexit\nno reference sample\nexit", capsys)
    expect = {"access-info": "ai_b", "topology": "no sample_lab", "scenario": "scen_b", "reference": "no sample\nref_b"}
    assert climain.render_configuration_candidate(_to(base, "running") or base) != ""
    for kind, text in expect.items():
        _to(base, "running")
        climain.execute_command_line(base, kind)
        assert climain.render_configuration_candidate(base) == text, kind
        # replay into a Fresh Session's same submode changes ONLY this kind
        dst = _fresh(root, "running-config", kind)
        assert _paste(dst, text, capsys) == ""
        changed = {k for k in dst.settings_candidate if dst.settings_candidate.get(k) != dst.committed_settings.get(k)} | {
            k for k in dst.committed_settings if dst.settings_candidate.get(k) != dst.committed_settings.get(k)
        }
        key = {"access-info": "active_access_info", "topology": "active_topology", "scenario": "active_scenario", "reference": "active_references"}[kind]
        assert changed == {key}, (kind, changed)
    # unchanged kinds print nothing in their own Common submode
    clean = _fresh(root, "running-config", "scenario")
    _run(clean, "no sample", capsys)
    _run(clean, "exit", capsys)
    climain.execute_command_line(clean, "topology")
    assert climain.render_configuration_candidate(clean) == ""


@pytest.mark.parametrize("kind,folder,mode_name", [("topology", "topologies", "topology"), ("scenario", "scenarios", "scenario"), ("access-info", "access-info", "access-info"), ("reference", "references", "reference")])
def test_common_collision_with_submode_keyword_fails_closed(root, capsys, kind, folder, mode_name):
    """A definition literally named `show` cannot be typed as `<name>` in the
    Common submode (the keyword wins). The Common renderer then emits a `%`
    line, which stops a paste instead of silently running `show`; unselecting
    it (`no show`) and a `description`-named definition are expressible."""
    src_name = {"topologies": "sample_lab", "scenarios": "sample", "references": "sample", "access-info": "sample_lab"}[folder]
    _clone(root, folder, src_name, "show")
    _clone(root, folder, src_name, "description")
    src = _fresh(root, "running-config", kind)
    _run(src, "show", capsys)  # the keyword: prints nothing, selects nothing
    assert not src.settings_dirty()
    # `description` is not a Common keyword, so it selects the definition
    _run(src, "description", capsys)
    assert src.settings_dirty()
    assert climain.render_configuration_candidate(src).splitlines()[-1] == "description"
    # the keyword-named one, via the parent path, renders as the fail-closed line
    src2 = _fresh(root, "running-config", f"{kind} show", "exit", kind)
    text = climain.render_configuration_candidate(src2)
    assert text.startswith("% ") and "collides" in text
    dst = _fresh(root, "running-config", kind)
    out = _paste(dst, text, capsys)
    assert out != "" and not dst.settings_dirty()
    # unselecting a keyword-named committed selection is safe: `no show`
    data = lab.read_settings(root)
    key = {"topology": "active_topology", "scenario": "active_scenario", "access-info": "active_access_info"}.get(kind)
    if key:
        data[key] = "show"
        (root / "settings.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
        s = _fresh(root, "running-config", kind, "no show")
        t = climain.render_configuration_candidate(s)
        assert t == "no show"
        d = _fresh(root, "running-config", kind)
        assert _paste(d, t, capsys) == "" and d.settings_candidate == s.settings_candidate


# ------------------------------------------------------ description replay

DESC_SETUP = {
    "add": "running-config\nscenario sample\ndescription hello\nexit",
    "topology-add": "running-config\ntopology sample_lab\ndescription tp\nexit",
    "access-info-add": "running-config\naccess-info sample_lab\ndescription ai\nexit",
    "reference-add": "running-config\nreference sample\ndescription rf one\nexit",
    "select+desc": "running-config\nscenario scen_b\ndescription new sc\nexit",
    "reference-new+desc": "running-config\nreference ref_b\ndescription rb\nexit\nreference ref_c\nexit",
    "multi-kind": "running-config\nscenario sample\ndescription s\nexit\ntopology sample_lab\ndescription t\nexit\nreference sample\ndescription r\nexit\nreference ref_b\nexit",
    "unset+desc": "running-config\ntopology\nno sample_lab\nexit\nscenario sample\ndescription keepme\nexit",
}


def _described(root, capsys):
    """Committed baseline carrying descriptions, so modify/remove are testable."""
    s = _fresh(root)
    _run(s, "running-config\nscenario sample\ndescription old\nexit\ntopology sample_lab\ndescription oldt\nexit\nreference sample\ndescription oldr\nexit", capsys)
    assert climain.execute_command_line(s, "commit")
    return s


@pytest.mark.parametrize("ctx", ["running", "global"])
@pytest.mark.parametrize("name", list(DESC_SETUP))
def test_description_delta_replays_via_named_submode(root, capsys, name, ctx):
    src = _fresh(root)
    _run(src, DESC_SETUP[name], capsys)
    _to(src, ctx)
    text = climain.render_configuration_candidate(src)
    assert "description" in text
    dst = _fresh(root, *(["running-config"] if ctx == "running" else []))
    assert _paste(dst, text, capsys) == ""
    assert _state(dst) == _state(src)
    assert dst.mode == ctx
    assert dst.settings_candidate.get("running_descriptions") == src.settings_candidate.get("running_descriptions")
    assert climain.render_configuration_candidate(dst) == text


@pytest.mark.parametrize("ctx", ["running", "global"])
@pytest.mark.parametrize(
    "edit",
    [
        "scenario sample\ndescription changed\nexit",  # modify
        "scenario sample\nno description\nexit",  # remove
        "topology sample_lab\nno description\nexit\nreference sample\ndescription R2\nexit",  # mixed
        "scenario sample\ndescription changed\nexit\nscenario sample\ndescription old\nexit",  # net-zero
    ],
    ids=["modify", "remove", "mixed", "netzero"],
)
def test_description_modify_remove_netzero_replay(root, capsys, edit, ctx):
    _described(root, capsys)
    src = _fresh(root)
    _run(src, "running-config\n" + edit, capsys)
    _to(src, ctx)
    text = climain.render_configuration_candidate(src)
    dst = _fresh(root, *(["running-config"] if ctx == "running" else []))
    if text:
        assert _paste(dst, text, capsys) == ""
    else:
        assert not src.settings_dirty()
    assert _state(dst) == _state(src)
    assert dst.mode == ctx


def test_description_section_then_global_definition_deletion_runs_at_global(root, capsys):
    src = _fresh(root)
    _run(src, "running-config\nscenario sample\ndescription d\nexit\nexit\nno reference ref_c", capsys)
    text = climain.render_configuration_candidate(src)
    assert text.splitlines()[-1] == "no reference ref_c"
    dst = _fresh(root)
    assert _paste(dst, text, capsys) == ""
    assert dst.mode == "global" and _state(dst) == _state(src)
    assert dst.settings_candidate["active_references"] == ["sample"]  # selection untouched


@pytest.mark.parametrize("name", KEYWORD_NAMES)
def test_description_on_keyword_named_entries_replays_without_drift(root, capsys, name):
    _clone(root, "scenarios", "sample", name)
    data = lab.read_settings(root)
    data["active_scenario"] = name
    (root / "settings.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    src = _fresh(root)
    _run(src, f"running-config\nscenario {name}\ndescription about {name}\nexit\ntopology topo_b\nexit", capsys)
    for ctx in ("running", "global"):
        _to(src, ctx)
        text = climain.render_configuration_candidate(src)
        dst = _fresh(root, *(["running-config"] if ctx == "running" else []))
        assert _paste(dst, text, capsys) == ""
        assert _state(dst) == _state(src)
        assert lab.scenario_exists(name, root)


def test_named_submode_scope_and_common_has_no_description_output(root, capsys):
    src = _fresh(root, "running-config", "scenario sample")
    _run(src, "description hello", capsys)
    assert src.mode == "running_scenario"
    assert "description hello" in climain.render_configuration_candidate(src)  # named: unchanged scope
    _paste(src, "exit", capsys)
    climain.execute_command_line(src, "scenario")
    assert climain.render_configuration_candidate(src) == ""  # Common: selection only, no description
    assert climain.execute_command_line(src, "description x") is False


# ------------------------------------------------- definition section modes


def test_access_info_and_topology_definition_sections_return_to_global(root, capsys):
    for setup in (
        "access-info sample_lab\ndevice R2\naddress 192.0.2.99\nexit\nexit",
        "topology sample_lab\ndescription d\ndevice R2\ntype iosxe\nexit\nexit",
    ):
        src = _fresh(root)
        _run(src, setup, capsys)
        text = climain.render_configuration_candidate(src)
        dst = _fresh(root)
        assert _paste(dst, text + "\nrunning-config\nexit", capsys) == ""  # a following global command works
        assert dst.mode == "global" and _state(dst) == _state(src)
        dst2 = _fresh(root)  # header + extra bangs: closes the definition, extra `!` is a global no-op
        _paste(dst2, text.splitlines()[0] + "\n!\n!\n!", capsys)
        assert dst2.mode == "global"


@pytest.mark.parametrize(
    "first,second",
    [
        ("topology sample_lab\ndescription d\nexit", "running-config\nscenario scen_b\nexit\nexit"),
        ("access-info sample_lab\ndevice R2\naddress 192.0.2.99\nexit\nexit", "running-config\ntopology topo_b\nexit\nexit"),
        ("topology sample_lab\ndescription d\nexit", "no reference ref_c"),
    ],
)
def test_definition_edit_then_running_or_global_operation(root, capsys, first, second):
    src = _fresh(root)
    _run(src, first, capsys)
    if src.mode != "global":
        _paste(src, "exit", capsys)
    _run(src, second, capsys)
    _to(src, "global")
    text = climain.render_configuration_candidate(src)
    dst = _fresh(root)
    assert _paste(dst, text, capsys) == ""
    assert dst.mode == "global" and _state(dst) == _state(src)


def test_running_change_then_definition_edit_order(root, capsys):
    src = _fresh(root)
    _run(src, "running-config\nscenario scen_b\nexit\nexit\ntopology sample_lab\ndescription d\nexit", capsys)
    text = climain.render_configuration_candidate(src)
    assert text.index("running-config") < text.index("topology sample_lab")
    dst = _fresh(root)
    assert _paste(dst, text, capsys) == ""
    assert dst.mode == "global" and _state(dst) == _state(src)


def test_scenario_and_reference_definition_delta_is_display_only_and_fails_closed(root, capsys, fake_editor):
    """Scenario/reference have no CLI body grammar (only external `edit`), so
    their definition delta is review text, not commands: replay stops at the
    first line, applies nothing, and leaves no definition mode open."""
    for kind, folder, name in (("scenario", "scenarios", "sample"), ("reference", "references", "sample")):
        src = _fresh(root)
        climain.execute_command_line(src, f"{kind} {name}")
        src.definition_candidate = dict(src.definition_candidate, description="edited")
        text = climain.render_configuration_candidate(src)
        assert "edited" in text
        dst = _fresh(root)
        out = _paste(dst, text, capsys)
        assert out != ""  # visible error, nothing applied
        assert dst.settings_candidate == dst.committed_settings and not dst.settings_dirty()
        assert dst.definition_candidate is None or dst.definition_candidate == dst.definition_original
        assert lab.read_settings(root) == dst.committed_settings


def test_keyword_named_definitions_after_unset_block_do_not_capture_following_sections(root, capsys):
    _clone(root, "scenarios", "sample", "scenario")
    _clone(root, "topologies", "sample_lab", "scenario")
    _clone(root, "topologies", "sample_lab", "reference")
    src = _fresh(root)
    _run(src, "running-config\ntopology\nno sample_lab\nexit\nscenario scen_b\nexit\nreference ref_b\nexit", capsys)
    for ctx in ("running", "global"):
        _to(src, ctx)
        text = climain.render_configuration_candidate(src)
        dst = _fresh(root, *(["running-config"] if ctx == "running" else []))
        assert _paste(dst, text, capsys) == ""
        assert _state(dst) == _state(src)
        assert "active_topology" not in dst.settings_candidate


# ---------------------------------------------------------------------------
# T1: stray structural `!` in topology definition modes (multi-line paste).
# Existing implementation: `!` in `device` -> topology, in `topology` -> global
# (the definition stays selected/open); a following mode-specific line then
# fails closed with `% Unknown command`. Nothing is silently reinterpreted.
# ---------------------------------------------------------------------------

def _committed_snapshot(root):
    return {
        folder: {p.name: p.read_bytes() for p in (root / folder).glob("*.yaml")}
        for folder in ("topologies", "scenarios", "references", "access-info")
    }


def _devices(s):
    return copy.deepcopy(s.definition_candidate["devices"])


T1_CASES = {
    # id: (paste, expected_mode, expected_error_fragment, description_after, R1_type_after)
    "A_bang_after_topology_then_mode_line": ("topology sample_lab\n!\ndescription x", "global", 'Unknown command: "description"', None, "iosxr"),
    "A_bang_after_topology_only": ("topology sample_lab\n!\n", "global", None, None, "iosxr"),
    "B_bang_after_device_then_mode_line": ("topology sample_lab\ndevice R1\n!\ntype iosxe", "topology", 'Unknown command: "type"', None, "iosxr"),
    "B_bang_after_device_only": ("topology sample_lab\ndevice R1\n!\n", "topology", None, None, "iosxr"),
    "C_consecutive_bangs_then_mode_line": ("topology sample_lab\ndevice R1\n!\n!\n!\n!\ndescription x", "global", 'Unknown command: "description"', None, "iosxr"),
    "C_consecutive_bangs_only": ("topology sample_lab\ndevice R1\n!\n!\n!\n!\n", "global", None, None, "iosxr"),
    "D_bang_then_sibling_device_line": ("topology sample_lab\ndevice R1\n!\ndevice R2\ntype iosxe", "device", None, None, "iosxr"),
    "D_bang_at_topology_then_device_line": ("topology sample_lab\n!\ndevice R1\ntype iosxe", "global", 'Unknown command: "device"', None, "iosxr"),
}


@pytest.mark.parametrize("case", list(T1_CASES))
def test_stray_bang_in_topology_modes_is_fail_closed(root, capsys, case):
    paste, mode, err, _desc, r1_type = T1_CASES[case]
    before = _committed_snapshot(root)
    s = _fresh(root)
    out = _paste(s, paste, capsys)
    assert s.mode == mode
    if err:
        assert err in out
    else:
        assert out.strip() == ""
    # the mode-specific line after the stray `!` never edits R1/description
    assert s.definition_candidate["devices"]["R1"]["type"] == r1_type
    assert s.definition_candidate["description"] == "Sample lab used for tests."
    if case == "D_bang_then_sibling_device_line":
        # `!` closed R1's block, so `device R2` + `type` is a legitimate edit of R2 only
        assert s.definition_candidate["devices"]["R2"]["type"] == "iosxe"
    else:
        assert s.definition_candidate["devices"]["R2"] == lab.load_topology("sample_lab", root)["devices"]["R2"]
    assert s.settings_candidate == s.committed_settings
    assert _committed_snapshot(root) == before


def test_stray_bang_then_valid_global_command_runs_at_global(root, capsys):
    """After `!` x2 the session is at global, so a following global command is
    parsed as a global command (the same precedent as access-info): here it
    only queues a pending definition deletion; nothing committed changes and
    `clear` discards it."""
    before = _committed_snapshot(root)
    s = _fresh(root)
    out = _paste(s, "topology sample_lab\ndevice R1\n!\n!\nno reference sample", capsys)
    assert out.strip() == ""
    assert s.mode == "global"
    assert s.definition_kind == "reference" and s.definition_name == "sample"
    assert s.settings_candidate == s.committed_settings
    assert _committed_snapshot(root) == before
    assert climain.execute_command_line(s, "clear")
    assert not s.overall_dirty()
    assert _committed_snapshot(root) == before


def test_bang_then_running_config_after_topology_edit_enters_running(root, capsys):
    before = _committed_snapshot(root)
    s = _fresh(root)
    out = _paste(s, "topology sample_lab\ndescription kept\n!\nrunning-config", capsys)
    assert out.strip() == ""
    assert s.mode == "running"
    assert s.definition_candidate["description"] == "kept"
    assert _committed_snapshot(root) == before


# ---------------------------------------------------------------------------
# T2: a multi-line paste is NOT atomic. It stops at the first failing line;
# earlier candidate changes stay, later lines never run, the committed state is
# untouched and `clear` discards the candidate.
# ---------------------------------------------------------------------------

def _assert_cleared(s, root, before):
    assert climain.execute_command_line(s, "clear")
    assert not s.overall_dirty()
    assert s.settings_candidate == s.committed_settings
    assert _committed_snapshot(root) == before


def test_non_atomic_paste_global_running_section(root, capsys):
    before = _committed_snapshot(root)
    s = _fresh(root)
    out = _paste(s, "running-config\nreference ref_b\nexit\nbogus line\nreference ref_c", capsys)
    assert "% Unknown command" in out
    assert "ref_b" in s.settings_candidate["active_references"]
    assert "ref_c" not in s.settings_candidate["active_references"]
    assert s.settings_dirty()
    assert s.settings_candidate != s.committed_settings
    assert s.committed_settings == _fresh(root).committed_settings
    assert _committed_snapshot(root) == before
    _assert_cleared(s, root, before)


def test_non_atomic_paste_in_running_context(root, capsys):
    before = _committed_snapshot(root)
    s = _fresh(root, "running-config")
    out = _paste(s, "scenario scen_b\nexit\nbogus line\nscenario sample", capsys)
    assert "% Unknown command" in out
    assert s.settings_candidate["active_scenario"] == "scen_b"
    assert s.settings_dirty()
    assert _committed_snapshot(root) == before
    _assert_cleared(s, root, before)


def test_non_atomic_paste_in_definition_context(root, capsys):
    before = _committed_snapshot(root)
    s = _fresh(root)
    out = _paste(s, "topology sample_lab\ndescription first change\nbogus line\ndescription second change", capsys)
    assert "% Unknown command" in out
    assert s.mode == "topology"
    assert s.definition_candidate["description"] == "first change"
    assert s.overall_dirty()
    assert _committed_snapshot(root) == before
    _assert_cleared(s, root, before)


def test_non_atomic_paste_unsupported_scenario_body_keeps_earlier_running_part(root, capsys):
    """Display-only boundary: a Global output whose scenario YAML body cannot
    be replayed stops there; the running-config part before it stays applied
    (no rollback) and the committed state is untouched."""
    before = _committed_snapshot(root)
    src = _fresh(root)
    _run(src, "running-config\nreference ref_b\nexit\nexit", capsys)
    text = climain.render_configuration_candidate(src) + "\nname: changed\n"
    dst = _fresh(root)
    out = _paste(dst, text, capsys)
    assert 'Unknown command: "name:"' in out
    assert "ref_b" in dst.settings_candidate["active_references"]
    assert _committed_snapshot(root) == before
    _assert_cleared(dst, root, before)
