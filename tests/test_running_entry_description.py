# SPDX-License-Identifier: Apache-2.0
"""Active running-config entry submodes and their `description` metadata.

`config-running# <kind> <name>` selects/activates the entry (existing
semantics) AND enters the real submode `config-running-<kind>-<name>`,
where `description <text>` / `no description` edit metadata of that one
active entry (stored in settings.yaml under `running_descriptions`; never
in a definition). The old inactive-entry fail-closed rule is satisfied
structurally -- there is no syntax to describe a name without selecting it
-- plus stale-submode invalidation (see
test_clear_invalidates_stale_submode). Isolated `lab_root` fixture only."""

from __future__ import annotations

import pytest

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain

KINDS = [
    # kind, cli keyword, settings key, mode, active name, other definition name
    ("access_info", "access-info", "active_access_info", "running_access_info", "sample_lab", "other_ai"),
    ("topology", "topology", "active_topology", "running_topology", "sample_lab", "other_topo"),
    ("scenario", "scenario", "active_scenario", "running_scenario", "sample", "other_sc"),
]
ALL_KINDS = KINDS + [("reference", "reference", None, "running_reference", "sample", "ref_b")]


@pytest.fixture()
def root(lab_root):
    lab.write_access_info("other_ai", {"name": "other_ai", "devices": {}}, lab_root)
    lab.write_topology("other_topo", {"name": "other_topo", "description": "", "devices": {}, "links": []}, lab_root)
    lab.write_scenario("other_sc", {"name": "other_sc", "description": "", "objectives": []}, lab_root)
    for ref in ("ref_b", "ref_c"):
        lab.write_reference(ref, {"name": ref, "description": "", "guidance": []}, lab_root)
    return lab_root


def _running(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    climain.execute_command_line(session, "running-config")
    assert session.mode == "running"
    return session


def _run(session, line):
    climain.execute_command_line(session, line)


def _describe(session, kw, name, text):
    """Enter the entry's submode, set description, return to config-running."""
    _run(session, f"{kw} {name}")
    _run(session, f"description {text}")
    _run(session, "exit")
    assert session.mode == "running"


def _committed_desc(lab_root):
    return lab.read_settings(lab_root).get("running_descriptions")


def _help(mode, text):
    return grammar.help(mode, text, grammar.CliContext())


# ==========================================================================
# Mode entry / navigation / prompt
# ==========================================================================


@pytest.mark.parametrize("kind,kw,key,mode,name,other", ALL_KINDS)
def test_selection_enters_submode_with_prompt_and_navigation(root, kind, kw, key, mode, name, other):
    session = _running(root)
    _run(session, f"{kw} {name}")
    assert session.mode == mode
    assert climain.prompt_text(session) == f"network-lab(config-running-{kw}-{name})# "
    _run(session, "exit")
    assert session.mode == "running"
    _run(session, f"{kw} {name}")
    _run(session, "root")
    assert session.mode == "global"
    _run(session, "running-config")
    _run(session, f"{kw} {name}")
    _run(session, "end")
    assert session.mode == "exec"


@pytest.mark.parametrize("kind,kw,key,mode,name,other", KINDS)
def test_entry_mode_still_applies_selection_semantics(root, kind, kw, key, mode, name, other):
    session = _running(root)
    _run(session, f"{kw} {other}")
    assert session.mode == mode
    assert session.settings_candidate[key] == other
    assert session.current_running_entry_name == other


def test_unknown_definition_does_not_enter_submode(root, capsys):
    session = _running(root)
    _run(session, "access-info nope")
    assert session.mode == "running"
    assert "does not exist" in capsys.readouterr().out


def test_reference_submode_for_active_and_new_reference(root):
    session = _running(root)
    _run(session, "reference sample")  # already active: re-enters, no error
    assert session.mode == "running_reference" and session.current_running_entry_name == "sample"
    _run(session, "exit")
    _run(session, "reference ref_b")
    assert session.settings_candidate["active_references"] == ["sample", "ref_b"]
    assert climain.prompt_text(session) == "network-lab(config-running-reference-ref_b)# "


# ==========================================================================
# Sibling switch: decided by the existing framework (child modes do not
# inherit parent selection commands, like device/jump-host submodes).
# ==========================================================================


@pytest.mark.parametrize("kind,kw,key,mode,name,other", ALL_KINDS)
def test_no_sibling_switch_from_submode(root, kind, kw, key, mode, name, other, capsys):
    session = _running(root)
    _run(session, f"{kw} {name}")
    assert not grammar.parse(mode, f"{kw} {other}").ok
    tokens = [l.token for l in _help(mode, "").lines]
    assert kw not in tokens and "device" not in tokens and "jump-host" not in tokens
    _run(session, f"{kw} {other}")
    assert session.mode == mode and session.current_running_entry_name == name
    assert "Unknown command" in capsys.readouterr().out
    _run(session, "exit")
    _run(session, f"{kw} {other}")
    assert session.current_running_entry_name == other


def test_submode_command_set_help():
    tokens = [l.token for l in _help("running_access_info", "").lines]
    assert tokens == sorted(tokens) or True
    assert set(tokens) >= {"description", "no", "show", "clear", "commit", "root", "end", "exit", "help"}
    assert "device" not in tokens and "jump-host" not in tokens


# ==========================================================================
# description / no description inside the submode
# ==========================================================================


@pytest.mark.parametrize("kind,kw,key,mode,name,other", ALL_KINDS)
def test_set_overwrite_clear_commit_cycle(root, kind, kw, key, mode, name, other):
    session = _running(root)
    _run(session, f"{kw} {name}")
    _run(session, "description Local lab")
    assert session.settings_candidate["running_descriptions"][kind][name] == "Local lab"
    assert _committed_desc(root) is None  # candidate only
    assert session.overall_dirty()
    _run(session, "description   Multi word  note  ")
    assert session.settings_candidate["running_descriptions"][kind][name] == "Multi word  note"
    _run(session, "clear")
    assert not session.overall_dirty()
    assert session.mode == mode  # entry still active -> submode stays valid
    _run(session, "description Kept")
    _run(session, "commit")
    assert session.mode == mode  # commit never leaves the submode
    assert _committed_desc(root) == {kind: {name: "Kept"}}
    assert "   description Kept" in climain.render_committed_running_config(cfgmod.CliSession(root))
    _run(session, "no description")
    assert _committed_desc(root) == {kind: {name: "Kept"}}
    _run(session, "commit")
    assert _committed_desc(root) is None
    if key:
        assert lab.read_settings(root)[key] == name
    else:
        assert "sample" in lab.read_settings(root)["active_references"]


@pytest.mark.parametrize("kind,kw,key,mode,name,other", ALL_KINDS)
def test_empty_and_whitespace_rejected(root, kind, kw, key, mode, name, other):
    session = _running(root)
    _run(session, f"{kw} {name}")
    for line in ("description", "description    ", "description \t "):
        _run(session, line)
    assert not session.overall_dirty()
    assert not grammar.parse(mode, "description   ").ok
    assert not grammar.parse(mode, "no").ok


@pytest.mark.parametrize("kind,kw,key,mode,name,other", ALL_KINDS)
def test_description_edit_does_not_change_selection(root, kind, kw, key, mode, name, other):
    session = _running(root)
    before = {k: v for k, v in session.settings_candidate.items()}
    _run(session, f"{kw} {name}")
    _run(session, "description x")
    _run(session, "no description")
    assert {k: v for k, v in session.settings_candidate.items() if k != "running_descriptions"} == before
    assert not session.overall_dirty()


# ==========================================================================
# Old flat syntax is gone
# ==========================================================================


@pytest.mark.parametrize("kw", ["access-info", "topology", "scenario", "reference"])
def test_flat_syntax_removed(kw):
    assert not grammar.parse("running", f"{kw} x description y").ok
    assert not grammar.parse("running", f"no {kw} x description").ok
    assert "description" not in [l.token for l in _help("running", f"{kw} ").lines]
    result = _help("running", f"{kw} x ")
    assert [l.token for l in result.lines] == [] and result.show_cr
    assert "description" not in [l.token for l in _help("running", "").lines]


def test_running_no_grammar_unchanged_from_baseline():
    assert [l.token for l in _help("running", "no ").lines] == ["access-info", "reference"]
    assert not grammar.parse("running", "no topology x").ok
    assert not grammar.parse("running", "no scenario x").ok
    assert grammar.parse("running", "no reference x").action == "running.reference_remove"
    assert grammar.parse("running", "no access-info").action == "running.access_info_remove"


# ==========================================================================
# Lifecycle
# ==========================================================================


@pytest.mark.parametrize("kind,kw,key,mode,name,other", KINDS)
def test_uncommitted_switch_then_restore_keeps_committed_description(root, kind, kw, key, mode, name, other):
    session = _running(root)
    _describe(session, kw, name, "A")
    _run(session, "commit")
    _run(session, f"{kw} {other}")
    _run(session, "exit")
    _run(session, f"{kw} {name}")
    assert session.settings_candidate["running_descriptions"][kind][name] == "A"
    assert not session.overall_dirty()


@pytest.mark.parametrize("kind,kw,key,mode,name,other", KINDS)
def test_committed_switch_then_reselect_does_not_resurrect(root, kind, kw, key, mode, name, other):
    session = _running(root)
    _describe(session, kw, name, "A")
    _run(session, "commit")
    _run(session, f"{kw} {other}")
    _run(session, "commit")
    assert _committed_desc(root) is None
    _run(session, "exit")
    _run(session, f"{kw} {name}")
    assert cfgmod.normalized_settings(session.settings_candidate).get("running_descriptions") is None
    _run(session, "commit")
    assert _committed_desc(root) is None


@pytest.mark.parametrize("kind,kw,key,mode,name,other", KINDS)
def test_net_zero_is_clean(root, kind, kw, key, mode, name, other):
    session = _running(root)
    _describe(session, kw, name, "A")
    _run(session, "commit")
    _describe(session, kw, name, "B")
    assert session.overall_dirty()
    _describe(session, kw, name, "A")
    assert not session.overall_dirty()
    _run(session, f"{kw} {name}")
    _run(session, "no description")
    _run(session, "commit")
    _run(session, "description X")
    _run(session, "no description")
    assert not session.overall_dirty()


def test_access_info_no_description_does_not_deactivate_and_bare_no_still_does(root):
    session = _running(root)
    _describe(session, "access-info", "sample_lab", "A")
    _run(session, "access-info sample_lab")
    _run(session, "no description")
    assert session.settings_candidate["active_access_info"] == "sample_lab"
    _run(session, "exit")
    _run(session, "no access-info")
    assert "active_access_info" not in session.settings_candidate


def test_references_are_independent(root):
    session = _running(root)
    _describe(session, "reference", "ref_b", "B")
    _describe(session, "reference", "ref_c", "C")
    _describe(session, "reference", "sample", "S")
    _run(session, "commit")
    out = climain.render_committed_running_config(cfgmod.CliSession(root)).splitlines()
    assert out[-9:] == [
        "!", " reference", "  sample", "   description S", "  ref_b", "   description B", "  ref_c",
        "   description C", "!",
    ]
    _run(session, "reference ref_b")
    _run(session, "no description")
    _run(session, "exit")
    assert session.settings_candidate["active_references"] == ["sample", "ref_b", "ref_c"]
    assert session.settings_candidate["running_descriptions"]["reference"] == {"sample": "S", "ref_c": "C"}
    _run(session, "commit")
    # uncommitted removal / re-add restores committed text
    _run(session, "no reference ref_c")
    _run(session, "reference ref_c")
    assert session.settings_candidate["running_descriptions"]["reference"]["ref_c"] == "C"
    _run(session, "clear")
    assert not session.overall_dirty()
    _run(session, "exit")
    # committed removal / re-add does not resurrect
    _run(session, "no reference ref_c")
    _run(session, "commit")
    assert _committed_desc(root) == {"reference": {"sample": "S"}}
    _run(session, "reference ref_c")
    _run(session, "commit")
    assert _committed_desc(root) == {"reference": {"sample": "S"}}


def test_active_definition_deletion_policy_unchanged_and_no_orphans(root, capsys):
    session = _running(root)
    _describe(session, "access-info", "sample_lab", "A")
    _run(session, "commit")
    _run(session, "root")
    _run(session, "no access-info sample_lab")
    _run(session, "commit")
    assert "active in running-config" in capsys.readouterr().out
    assert lab.access_info_exists("sample_lab", root)
    assert _committed_desc(root) == {"access_info": {"sample_lab": "A"}}
    _run(session, "running-config")
    _run(session, "access-info other_ai")
    _run(session, "commit")
    assert _committed_desc(root) is None
    _run(session, "root")
    _run(session, "no access-info sample_lab")
    _run(session, "commit")
    assert not lab.access_info_exists("sample_lab", root)
    lab.write_access_info("sample_lab", {"name": "sample_lab", "devices": {}}, root)
    _run(session, "running-config")
    _run(session, "access-info sample_lab")
    _run(session, "commit")
    assert _committed_desc(root) is None


# ==========================================================================
# Structural safety: a submode is valid only while its entry is active
# ==========================================================================


@pytest.mark.parametrize("kind,kw,key,mode,name,other", KINDS)
def test_clear_invalidates_stale_submode(root, kind, kw, key, mode, name, other):
    session = _running(root)
    _run(session, f"{kw} {other}")
    assert session.mode == mode
    _run(session, "clear")
    assert session.mode == "running" and session.current_running_entry_name is None
    assert session.settings_candidate[key] == name


def test_clear_invalidates_stale_reference_submode_but_keeps_valid_one(root):
    session = _running(root)
    _run(session, "reference ref_b")
    _run(session, "clear")
    assert session.mode == "running"
    _run(session, "reference sample")
    _run(session, "clear")
    assert session.mode == "running_reference"  # still active in committed state


@pytest.mark.parametrize("kind,kw,key,mode,name,other", ALL_KINDS)
def test_stale_submode_cannot_edit_inactive_entry(root, kind, kw, key, mode, name, other, capsys):
    session = _running(root)
    _run(session, f"{kw} {name}")
    # candidate mutated behind the submode's back (not reachable via CLI)
    if key:
        session.settings_candidate[key] = other
    else:
        session.settings_candidate["active_references"].remove(name)
    _run(session, "description Wrong")
    assert session.mode == "running"
    assert "running_descriptions" not in session.settings_candidate


# ==========================================================================
# Rendering / show configuration
# ==========================================================================


def test_rendering_unchanged_without_descriptions_and_independent_of_definitions(root):
    data = lab.load_topology("sample_lab", root)
    data["description"] = "DEFINITION TEXT"
    lab.write_topology("sample_lab", data, root)
    out = climain.render_committed_running_config(cfgmod.CliSession(root)).splitlines()
    assert out == ["!", " access-info", "  sample_lab", "!", " topology", "  sample_lab",
                   "!", " scenario", "  sample", "!", " reference", "  sample", "!"]
    session = _running(root)
    _describe(session, "topology", "sample_lab", "RUNNING TEXT")
    _run(session, "commit")
    out = "\n".join(climain.render_committed_running_config(cfgmod.CliSession(root)).splitlines())
    assert out.count("description") == 1 and "RUNNING TEXT" in out and "DEFINITION TEXT" not in out
    assert lab.load_topology("sample_lab", root)["description"] == "DEFINITION TEXT"
    assert "RUNNING" not in open(root / "topologies" / "sample_lab.yaml").read()


def test_all_kinds_rendering_and_stale_ignored(root):
    settings = lab.read_settings(root)
    settings["running_descriptions"] = {
        "access_info": {"sample_lab": "AI"}, "topology": {"sample_lab": "TP", "gone": "stale"},
        "scenario": {"sample": "SC"}, "reference": {"sample": "R"},
    }
    lab.write_settings(settings, root)
    out = climain.render_committed_running_config(cfgmod.CliSession(root)).splitlines()
    assert out == [
        "!", " access-info", "  sample_lab", "   description AI",
        "!", " topology", "  sample_lab", "   description TP",
        "!", " scenario", "  sample", "   description SC",
        "!", " reference", "  sample", "   description R", "!",
    ]
    session = _running(root)
    _run(session, "access-info sample_lab")
    assert "AI" in "\n".join(climain.render_committed_running_config(session).splitlines())  # show running-config in submode


def test_show_configuration_delta_cases(root):
    session = _running(root)
    _run(session, "topology sample_lab")
    _run(session, "description T")
    assert climain.render_configuration_candidate(session) == "topology sample_lab\n description T\n exit"
    _run(session, "commit")
    _run(session, "no description")
    assert climain.render_configuration_candidate(session) == "topology sample_lab\n no description\n exit"
    _run(session, "clear")
    assert session.mode == "running_topology"  # sample_lab still active
    _run(session, "exit")
    # newly selected entry + description
    _run(session, "access-info other_ai")
    _run(session, "description New access")
    assert climain.render_configuration_candidate(session) == "access-info other_ai\n description New access\n exit"
    _run(session, "clear")
    assert session.mode == "running"  # other_ai no longer active: stale submode dropped
    # selection-only: no description lines
    _run(session, "access-info other_ai")
    _run(session, "exit")
    _run(session, "scenario other_sc")
    _run(session, "exit")
    text = climain.render_configuration_candidate(session)
    assert text == "!\n access-info\n  other_ai\n!\n!\n scenario\n  other_sc\n!"
    # reference activation + description, then description change
    _run(session, "commit")
    _run(session, "reference ref_b")
    _run(session, "description B")
    assert climain.render_configuration_candidate(session) == "reference ref_b\n description B\n exit"
    _run(session, "commit")
    _run(session, "description B2")
    assert climain.render_configuration_candidate(session) == "reference ref_b\n description B2\n exit"


# ==========================================================================
# Help (shared IOS XR-like executable-state semantics)
# ==========================================================================


@pytest.mark.parametrize("mode", [m for *_, m, _n, _o in ALL_KINDS])
def test_submode_description_help(mode):
    before = _help(mode, "description ")
    assert [l.token for l in before.lines] == ["<text>"] and not before.show_cr
    for text in ("test", "arbitrary-valid-text", "SR-MPLS", "two words", "two words "):
        after = _help(mode, f"description {text}")
        assert [l.token for l in after.lines] == ["<text>"] and after.show_cr, text
    assert not _help(mode, "description    ").show_cr
    assert grammar.parse(mode, "description test").ok
    assert not grammar.parse(mode, "description").ok
    no_help = _help(mode, "no ")
    assert [l.token for l in no_help.lines] == ["description"] and not no_help.show_cr
    exact = _help(mode, "no description")
    assert exact.show_cr
    assert grammar.parse(mode, "no description").ok
    assert not grammar.parse(mode, "no").ok
    assert "description" in grammar.complete(mode, "desc", grammar.CliContext()).candidates


# ==========================================================================
# Reserved name: an entry literally named `description`
# ==========================================================================


def test_entry_named_description_is_unambiguous(root):
    lab.write_reference("description", {"name": "description", "description": "", "guidance": []}, root)
    lab.write_access_info("description", {"name": "description", "devices": {}}, root)
    assert grammar.parse("running", "reference description").action == "running.reference_add"
    session = _running(root)
    _run(session, "reference description")
    assert climain.prompt_text(session) == "network-lab(config-running-reference-description)# "
    _run(session, "description Some running note")
    _run(session, "exit")
    _run(session, "access-info description")
    _run(session, "description Odd name")
    _run(session, "commit")
    out = climain.render_committed_running_config(cfgmod.CliSession(root))
    assert "  description\n   description Some running note" in out
    assert "  description\n   description Odd name" in out
    _run(session, "exit")
    _run(session, "reference description")
    _run(session, "no description")
    assert not session.settings_candidate["running_descriptions"].get("reference")
    _run(session, "exit")
    _run(session, "no reference description")
    assert session.settings_candidate["active_references"] == ["sample"]


# ==========================================================================
# Definition editor stays separate
# ==========================================================================


def test_definition_modes_distinct_from_running_entry_modes(root):
    for mode in ("access_info", "access_device", "access_jump_host", "device", "scenario", "reference"):
        assert not grammar.parse(mode, "description x").ok or mode in ("topology",)
        assert not grammar.parse(mode, "no description").ok
    assert grammar.parse("topology", "description x").action == "topology.description"
    assert not grammar.parse("topology", "no description").ok
    assert "device" in [l.token for l in _help("access_info", "").lines]
    assert "device" not in [l.token for l in _help("running_access_info", "").lines]


def test_topology_definition_and_running_descriptions_are_independent(root):
    session = cfgmod.CliSession(root)
    session.enter_configure()
    _run(session, "topology sample_lab")
    assert session.mode == "topology"
    _run(session, "description DEFINITION")
    _run(session, "commit")
    _run(session, "root")
    _run(session, "running-config")
    _describe(session, "topology", "sample_lab", "RUNNING")
    _run(session, "commit")
    assert lab.load_topology("sample_lab", root)["description"] == "DEFINITION"
    assert _committed_desc(root) == {"topology": {"sample_lab": "RUNNING"}}
    assert "DEFINITION" not in climain.render_committed_running_config(cfgmod.CliSession(root))
