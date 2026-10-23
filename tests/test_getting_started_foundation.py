# SPDX-License-Identifier: Apache-2.0
"""Knowledge-sharing foundation: getting_started / cisco_platform_guidance defaults.

Reads the real tracked files under lab/ (read-only) and copies them into an
isolated tmp lab_root for CLI/MCP-path checks. Never touches
lab/settings.yaml or any private file."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain

REPO_LAB = Path(__file__).resolve().parent.parent / "lab"

GUIDANCE_FIELDS = ["workflow", "scenario_guidance", "reference_guidance", "decision_rules", "knowledge_lifecycle"]
FRESH_RUNNING_CONFIG = """!
 access-info
  sample_lab
   description Placeholder access information for the initial sample configuration
!
 topology
  sample_lab
   description Placeholder topology; it can also be generated automatically from access-info and LLDP neighbor information using the discovery command
!
 scenario
  getting_started
   description Default onboarding scenario for Network Lab MCP
!
 reference
  cisco_platform_guidance
   description Guidance for determining whether Cisco platform-specific engineering knowledge applies to a target device
!"""
LEAK_TERMS = [
    "Tell me what you want",
    "message",
    "workflow",
    "understand_user_intent",
    "scenario_guidance",
    "reference_guidance",
    "decision_rules",
    "knowledge_lifecycle",
    "How devices can be accessed",
    "Inspect the active running configuration",
]


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture()
def fresh_root(tmp_path):
    """Fresh initialization: copy the tracked template, as README instructs."""
    root = tmp_path / "lab"
    for sub in ("access-info", "topologies", "scenarios", "references"):
        shutil.copytree(REPO_LAB / sub, root / sub, ignore=shutil.ignore_patterns("test_lab.yaml", "pagent*", "multi_flow*"))
    shutil.copy(REPO_LAB / "principles.yaml", root / "principles.yaml")
    shutil.copy(REPO_LAB / "settings.example.yaml", root / "settings.yaml")
    return root


# ---- structure / responsibility -------------------------------------------

def test_getting_started_structure():
    data = _load(REPO_LAB / "scenarios" / "getting_started.yaml")
    assert data["name"] == "getting_started"
    assert data["description"].strip()
    assert data["message"].startswith("Tell me what you want to build, investigate, or validate.")
    assert "YAML" in data["message"]
    assert len(data["message"].splitlines()) <= 3  # short; not a second scenario body
    for field in GUIDANCE_FIELDS:
        assert data[field], field
    assert data["workflow"][0] == "understand_user_intent"
    assert {"purpose", "create_when", "refine_when", "include", "avoid"} <= set(data["scenario_guidance"])
    assert {"purpose", "create_when", "refine_when", "include", "avoid"} <= set(data["reference_guidance"])


def test_getting_started_is_scenario_and_cisco_guidance_is_reference():
    assert (REPO_LAB / "scenarios" / "getting_started.yaml").is_file()
    assert not (REPO_LAB / "references" / "getting_started.yaml").exists()
    assert (REPO_LAB / "references" / "cisco_platform_guidance.yaml").is_file()
    assert not (REPO_LAB / "scenarios" / "cisco_platform_guidance.yaml").exists()
    guidance = _load(REPO_LAB / "references" / "cisco_platform_guidance.yaml")
    assert guidance["name"] == "cisco_platform_guidance"
    assert "message" not in guidance and "workflow" not in guidance


def test_network_lab_basics_is_removed_from_tracked_state():
    tracked = subprocess.run(
        ["git", "ls-files", "lab", ".gitignore", "README.md", "docs", "src"],
        cwd=REPO_LAB.parent, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert "lab/references/network_lab_basics.yaml" not in tracked
    for rel in tracked:
        path = REPO_LAB.parent / rel
        if path.is_file():
            # Compare raw bytes: tracked files include binary images (e.g. .webp)
            # that are not valid UTF-8 text.
            assert b"network_lab_basics" not in path.read_bytes(), rel
    assert "network_lab_basics" not in _load(REPO_LAB / "settings.example.yaml")["active_references"]


def test_network_lab_basics_not_discoverable(fresh_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: fresh_root)
    names = [e["name"] for e in lab.get_execution_instructions()["available_references"]]
    assert "cisco_platform_guidance" in names
    assert "network_lab_basics" not in names


def test_persisted_missing_reference_is_not_migrated(fresh_root, monkeypatch):
    settings = lab.read_settings(fresh_root)
    settings["active_references"] = ["network_lab_basics"]
    (fresh_root / "settings.yaml").write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    before = (fresh_root / "settings.yaml").read_bytes()
    monkeypatch.setattr(lab, "find_lab_root", lambda: fresh_root)
    with pytest.raises(lab.LabConfigError, match="network_lab_basics"):
        lab.get_execution_instructions()
    assert (fresh_root / "settings.yaml").read_bytes() == before
    assert lab.get_active_reference_names(lab.read_settings(fresh_root)) == ["network_lab_basics"]


def test_no_sample_connectivity_scenario_or_generic_sample_scenario():
    for sub in ("scenarios", "references"):
        names = {p.stem for p in (REPO_LAB / sub).glob("*.yaml")}
        assert "sample_connectivity" not in names
    assert not (REPO_LAB / "scenarios" / "sample.yaml").exists()


def test_getting_started_is_vendor_neutral():
    text = (REPO_LAB / "scenarios" / "getting_started.yaml").read_text(encoding="utf-8").lower()
    for term in ("cisco", "ios xr", "iosxr", "nx-os"):
        assert term not in text


# ---- optional message ------------------------------------------------------

@pytest.mark.parametrize(
    "body,expected",
    [
        ({"name": "s", "objectives": ["x"]}, None),
        ({"name": "s", "message": "hello"}, "hello"),
        ({"name": "s", "message": "line1\nline2\n"}, "line1\nline2\n"),
    ],
)
def test_scenario_message_optional_and_preserved(lab_root, body, expected):
    lab.validate_scenario_data("s", body)
    lab.write_scenario("s", body, lab_root)
    loaded = lab.load_scenario("s", lab_root)
    assert loaded.get("message") == expected
    if expected is None:
        assert "message" not in loaded and loaded == body


def test_message_and_guidance_reach_mcp_consumption_path(fresh_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: fresh_root)
    result = lab.get_execution_instructions()
    assert result["scenario"]["name"] == "getting_started"
    content = result["scenario"]["content"]
    assert content["message"].startswith("Tell me what you want to build")
    for field in GUIDANCE_FIELDS:
        assert field in content
    assert [r["name"] for r in result["references"]] == ["cisco_platform_guidance"]


# ---- fresh vs existing settings -------------------------------------------

def _running_text(root):
    return climain.render_committed_running_config(cfgmod.CliSession(root))


def test_fresh_default_running_config(fresh_root):
    assert _running_text(fresh_root) == FRESH_RUNNING_CONFIG


def test_fresh_default_running_config_has_no_definition_leakage(fresh_root):
    text = _running_text(fresh_root)
    for term in LEAK_TERMS:
        assert term not in text
    for sub in ("scenarios", "references", "access-info", "topologies"):
        for p in (fresh_root / sub).glob("*.yaml"):
            assert "running_descriptions" not in p.read_text(encoding="utf-8")


def test_show_configuration_has_no_definition_leakage(fresh_root, capsys):
    session = cfgmod.CliSession(fresh_root)
    session.enter_configure()
    climain.execute_command_line(session, "running-config")
    climain.execute_command_line(session, "scenario getting_started")
    climain.execute_command_line(session, "description Edited scenario description")
    climain.execute_command_line(session, "exit")
    climain.execute_command_line(session, "reference cisco_platform_guidance")
    climain.execute_command_line(session, "description Edited reference description")
    climain.execute_command_line(session, "exit")
    capsys.readouterr()
    climain.execute_command_line(session, "show configuration")
    climain.execute_command_line(session, "show running-config")
    out = capsys.readouterr().out
    assert "Edited scenario description" in out and "Edited reference description" in out
    for term in LEAK_TERMS:
        assert term not in out


EXISTING = {
    "active_access_info": "real_lab",
    "active_topology": "production_lab",
    "active_scenario": "existing_scenario",
    "active_references": ["pagent_pkts", "custom_reference"],
    "running_descriptions": {"scenario": {"existing_scenario": "mine"}},
}


def test_existing_settings_not_redefaulted(tmp_path):
    root = tmp_path / "lab"
    root.mkdir()
    (root / "settings.yaml").write_text(yaml.safe_dump(EXISTING, sort_keys=False), encoding="utf-8")
    settings = lab.read_settings(root)
    assert settings == EXISTING
    assert lab.get_active_scenario_name(settings) == "existing_scenario"
    assert lab.get_active_reference_names(settings) == ["pagent_pkts", "custom_reference"]
    assert "getting_started" not in _running_text(root)
    assert "cisco_platform_guidance" not in _running_text(root)
    assert " mine" in _running_text(root)


def test_existing_settings_without_descriptions_and_empty_references(tmp_path):
    root = tmp_path / "lab"
    root.mkdir()
    legacy = {"active_topology": "t", "active_scenario": "s", "active_references": []}
    (root / "settings.yaml").write_text(yaml.safe_dump(legacy), encoding="utf-8")
    settings = lab.read_settings(root)
    assert settings == legacy
    assert lab.get_active_reference_names(settings) == []
    assert lab.get_active_access_info_name(settings) is None
    text = _running_text(root)
    assert "cisco_platform_guidance" not in text and "getting_started" not in text and "description" not in text
    assert cfgmod.normalized_settings(settings) == legacy


# ---- CLI submodes for the new defaults ------------------------------------

def test_submodes_for_new_defaults(fresh_root, capsys):
    session = cfgmod.CliSession(fresh_root)
    session.enter_configure()
    climain.execute_command_line(session, "running-config")
    climain.execute_command_line(session, "scenario getting_started")
    assert session.mode == "running_scenario"
    climain.execute_command_line(session, "no description")
    climain.execute_command_line(session, "exit")
    assert session.mode == "running"
    climain.execute_command_line(session, "reference cisco_platform_guidance")
    assert session.mode == "running_reference"
    climain.execute_command_line(session, "description Changed")
    climain.execute_command_line(session, "exit")
    capsys.readouterr()
    climain.execute_command_line(session, "show configuration")
    out = capsys.readouterr().out
    assert "Changed" in out
    for term in LEAK_TERMS:
        assert term not in out
    climain.execute_command_line(session, "commit")
    desc = lab.read_settings(fresh_root)["running_descriptions"]
    assert desc["reference"]["cisco_platform_guidance"] == "Changed"
    assert "scenario" not in desc
    # definition files never receive running descriptions
    assert "Changed" not in (fresh_root / "references" / "cisco_platform_guidance.yaml").read_text(encoding="utf-8")
    assert "getting_started" in lab.list_scenario_names(fresh_root)
    assert "cisco_platform_guidance" in lab.list_reference_names(fresh_root)


# ---- knowledge discovery / read-only inspection ------------------

import hashlib

from network_lab_mcp import mcp_server


@pytest.fixture()
def kroot(lab_root, monkeypatch):
    lab.write_scenario("sc_b", {"name": "sc_b", "description": "Second scenario.", "objectives": ["b"]}, lab_root)
    lab.write_scenario("sc_legacy", {"objectives": ["no description"]}, lab_root)
    lab.write_reference("ref_b", {"name": "ref_b", "description": "Ref B.", "guidance": ["b"]}, lab_root)
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    return lab_root


def _snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def test_catalog_lists_stored_metadata_only_deterministically(kroot):
    out = lab.get_execution_instructions()
    names = [e["name"] for e in out["available_scenarios"]]
    assert names == sorted(names) and {"sample", "sc_b", "sc_legacy"} <= set(names)
    assert {e["name"]: e["description"] for e in out["available_scenarios"]}["sc_b"] == "Second scenario."
    assert {e["name"]: e["description"] for e in out["available_scenarios"]}["sc_legacy"] is None
    rnames = [e["name"] for e in out["available_references"]]
    assert rnames == sorted(rnames) and {"ref_b", "sample"} <= set(rnames)
    for e in out["available_scenarios"] + out["available_references"]:
        assert set(e) == {"name", "description"}
    assert out["inspected_scenarios"] == [] and out["inspected_references"] == []
    assert "sample_lab" not in str(out["available_scenarios"] + out["available_references"])
    assert out == lab.get_execution_instructions()


def test_default_call_is_compatible_and_has_no_nonactive_bodies(kroot):
    out = lab.get_execution_instructions()
    assert out["scenario"]["name"] == "sample"
    assert [r["name"] for r in out["references"]] == ["sample"]
    assert out["principles"]
    assert "Second scenario." not in str(out["scenario"]) + str(out["references"]) + str(out["inspected_scenarios"])
    assert "objectives" not in str(out["available_scenarios"])


def test_inspect_non_active_is_read_only(kroot):
    before = _snapshot(kroot)
    out = lab.get_execution_instructions(["sc_b", "sc_b", "sc_legacy"], ["ref_b", "sample"])
    assert [e["name"] for e in out["inspected_scenarios"]] == ["sc_b", "sc_legacy"]
    assert out["inspected_scenarios"][0]["content"]["objectives"] == ["b"]
    assert [e["name"] for e in out["inspected_references"]] == ["ref_b", "sample"]
    assert out["scenario"]["name"] == "sample"
    assert [r["name"] for r in out["references"]] == ["sample"]
    assert _snapshot(kroot) == before


@pytest.mark.parametrize("bad", ["missing", "../settings", "/etc/passwd", "sc_b.yaml", "..", "sample_lab", ""])
def test_inspect_rejects_unknown_and_paths(kroot, bad):
    with pytest.raises(lab.LabConfigError):
        lab.get_execution_instructions([bad])
    with pytest.raises(lab.LabConfigError):
        lab.get_execution_instructions(None, [bad])


def test_inspect_rejects_malformed_and_symlink(kroot, tmp_path):
    (kroot / "scenarios" / "broken.yaml").write_text("- not\n- mapping\n", encoding="utf-8")
    with pytest.raises(lab.LabConfigError):
        lab.get_execution_instructions(["broken"])
    outside = tmp_path / "outside.yaml"
    outside.write_text("name: outside\ndescription: secret\n", encoding="utf-8")
    (kroot / "references" / "link.yaml").symlink_to(outside)
    with pytest.raises(lab.LabConfigError):
        lab.get_execution_instructions(None, ["link"])
    assert "link" not in [e["name"] for e in lab.get_execution_instructions()["available_references"]]


def test_mcp_tool_schema_optional_args_and_error(kroot):
    import anyio

    tools = {t.name: t for t in anyio.run(mcp_server.mcp.list_tools)}
    schema = tools["get_execution_instructions"].input_schema
    assert not schema.get("required")
    assert {"inspect_scenarios", "inspect_references"} <= set(schema["properties"])
    assert len(tools) == 7
    with pytest.raises(mcp_server.ToolError):
        mcp_server.get_execution_instructions(["nope"])


def test_catalog_not_in_running_config_views(fresh_root, capsys):
    session = cfgmod.CliSession(fresh_root)
    session.enter_configure()
    climain.execute_command_line(session, "show running-config")
    climain.execute_command_line(session, "show configuration")
    out = capsys.readouterr().out
    for term in ("available_scenarios", "available_references", "inspected_"):
        assert term not in out


def test_getting_started_expresses_proposal_and_human_commit_boundary():
    data = _load(REPO_LAB / "scenarios" / "getting_started.yaml")
    rules = " ".join(data["decision_rules"]).lower()
    assert "reuse" in rules and "propose" in rules and "cli" in rules and "human" in rules
    assert "persist" in rules  # explicitly: the AI does not persist


def test_persisted_generic_sample_selection_is_not_migrated(tmp_path):
    root = tmp_path / "lab"
    root.mkdir()
    legacy = {"active_access_info": "sample", "active_topology": "sample", "active_scenario": "s", "active_references": []}
    (root / "settings.yaml").write_text(yaml.safe_dump(legacy), encoding="utf-8")
    assert lab.read_settings(root) == legacy
    assert "sample_lab" not in _running_text(root)


def test_fresh_default_sample_lab_submodes_and_completion(fresh_root):
    session = cfgmod.CliSession(fresh_root)
    session.enter_configure()
    climain.execute_command_line(session, "running-config")
    climain.execute_command_line(session, "access-info sample_lab")
    assert session.mode == "running_access_info"
    climain.execute_command_line(session, "exit")
    climain.execute_command_line(session, "topology sample_lab")
    assert session.mode == "running_topology"
    assert "sample_lab" in lab.list_access_info_names(fresh_root)
    assert "sample_lab" in lab.list_topology_names(fresh_root)
    assert "sample" not in lab.list_access_info_names(fresh_root)
    assert "sample" not in lab.list_topology_names(fresh_root)


# ---- Cisco platform guidance reference -------------------------------------

CISCO_REF = REPO_LAB / "references" / "cisco_platform_guidance.yaml"


def _cisco():
    return _load(CISCO_REF)


def test_cisco_platform_guidance_loads_through_reference_path():
    root_ref = lab.load_reference("cisco_platform_guidance", REPO_LAB)
    assert root_ref["name"] == "cisco_platform_guidance"
    assert "applies" in root_ref["description"] and len(root_ref["description"]) > 50


def test_cisco_device_types_match_device_type_ssot():
    declared = set(_cisco()["cisco_device_types"])
    cisco_in_ssot = {k for k, v in lab.DEVICE_TYPES.items() if v.startswith("Cisco")}
    assert declared == cisco_in_ssot
    assert "host" not in declared
    for key in declared:
        assert lab.normalize_device_type(key) == key


def test_cisco_guidance_policy_concepts_present():
    d = _cisco()
    assert {"device_type", "operating_system", "product_family", "software_release", "feature_domain", "question_type"} == set(d["routing_inputs"])
    assert d["applicability_principles"] and d["cross_platform_safety"]
    assert d["protocol_vs_implementation"]["broadly_reusable_concepts"]
    assert "YANG module availability" in d["protocol_vs_implementation"]["verify_for_the_target_platform"]
    assert d["documentation_classes"] == ["Configuration Guides", "Command References", "Release Notes"]
    assert set(d["source_selection"]) == {"configuration", "command_syntax", "feature_support", "data_model", "hardware"}
    assert d["yang"]["source"]["url"] == "https://github.com/YangModels/yang/tree/main/vendor/cisco"
    telemetry = " ".join(d["telemetry"]["principles"]).lower()
    assert "yang" in telemetry and "do not invent" in telemetry
    assert any("distinct" in t for t in d["yang"]["lookup_guidance"])
    assert d["lab_evidence"] and d["reusable_knowledge"]


def test_cisco_guidance_is_discoverable_inspectable_and_default_active(fresh_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: fresh_root)
    before = (fresh_root / "settings.yaml").read_bytes()
    default = lab.get_execution_instructions()
    entry = {e["name"]: e for e in default["available_references"]}["cisco_platform_guidance"]
    assert entry["description"]
    assert [r["name"] for r in default["references"]] == ["cisco_platform_guidance"]
    inspected = lab.get_execution_instructions(None, ["cisco_platform_guidance"])
    assert inspected["inspected_references"][0]["content"]["name"] == "cisco_platform_guidance"
    assert [r["name"] for r in inspected["references"]] == ["cisco_platform_guidance"]
    assert (fresh_root / "settings.yaml").read_bytes() == before


def test_example_settings_activate_only_cisco_guidance():
    settings = _load(REPO_LAB / "settings.example.yaml")
    assert settings["active_references"] == ["cisco_platform_guidance"]
