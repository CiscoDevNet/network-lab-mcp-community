# SPDX-License-Identifier: Apache-2.0
"""Canonical public sample definitions consistency.

Unlike the other test modules, this one intentionally reads the real
tracked repository files under lab/ (not an isolated lab_root fixture) --
these are the exact four canonical samples shipped in git, and this test
exists specifically to keep them coherent with each other and with
settings.example.yaml. It never touches lab/settings.yaml or any other
untracked/private file.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from network_lab_mcp import lab

REPO_ROOT = Path(__file__).resolve().parent.parent
LAB_ROOT = REPO_ROOT / "lab"


def _load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_canonical_sample_files_exist():
    assert (LAB_ROOT / "access-info" / "sample_lab.yaml").is_file()
    assert (LAB_ROOT / "topologies" / "sample_lab.yaml").is_file()
    assert (LAB_ROOT / "scenarios" / "getting_started.yaml").is_file()
    assert (LAB_ROOT / "references" / "cisco_platform_guidance.yaml").is_file()


def test_old_generic_sample_definitions_are_absent():
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "lab"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    assert "lab/access-info/sample.yaml" not in tracked
    assert "lab/topologies/sample.yaml" not in tracked


def test_canonical_definition_names_match_files():
    assert _load(LAB_ROOT / "access-info" / "sample_lab.yaml")["name"] == "sample_lab"
    assert _load(LAB_ROOT / "topologies" / "sample_lab.yaml")["name"] == "sample_lab"
    assert _load(LAB_ROOT / "scenarios" / "getting_started.yaml")["name"] == "getting_started"
    assert _load(LAB_ROOT / "references" / "cisco_platform_guidance.yaml")["name"] == "cisco_platform_guidance"


def test_settings_example_selects_defaults_for_every_definition_type():
    settings = _load(LAB_ROOT / "settings.example.yaml")
    assert settings["active_access_info"] == "sample_lab"
    assert settings["active_topology"] == "sample_lab"
    assert settings["active_scenario"] == "getting_started"
    assert settings["active_references"] == ["cisco_platform_guidance"]


def test_canonical_sample_r1_is_coherent_between_access_info_and_topology():
    access_info = _load(LAB_ROOT / "access-info" / "sample_lab.yaml")
    topology = _load(LAB_ROOT / "topologies" / "sample_lab.yaml")

    access_device = access_info["devices"]["R1"]
    topology_device = topology["devices"]["R1"]

    assert lab.normalize_device_type(access_device["type"]) == lab.normalize_device_type(
        topology_device["type"]
    )


def test_canonical_access_info_only_holds_connection_data_not_copied_into_topology():
    topology_device = _load(LAB_ROOT / "topologies" / "sample_lab.yaml")["devices"]["R1"]
    assert "address" not in topology_device
    assert "username" not in topology_device
    assert "password" not in topology_device
    assert "jump_host" not in topology_device
