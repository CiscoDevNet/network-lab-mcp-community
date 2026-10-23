# SPDX-License-Identifier: Apache-2.0
"""Topology `links` validation (lab.validate_topology_links(), called from
lab.validate_topology_data()) -- the same SSOT used by load_topology(),
write_topology() (and therefore the CLI's commit path), and get_device().

`links` has no structured CLI editing command; it can only be authored by
`discover topology`, the external `edit` (topology mode), or by directly
editing the committed YAML by hand -- so validation is the only guard
against a malformed value reaching disk or breaking the renderer/
Discovery merge logic."""

from __future__ import annotations

import pytest

from network_lab_mcp import lab


def _topology(links=None, devices=None):
    return {
        "name": "t",
        "description": "",
        "devices": devices if devices is not None else {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}},
        "links": links,
    }


def test_valid_sample_topology_passes():
    lab.validate_topology_data("sample", _topology(links=[{"a": "R1", "b": "R2"}]))


def test_links_omitted_is_valid():
    data = {"name": "t", "devices": {"R1": {"type": "iosxr"}}}
    lab.validate_topology_data("t", data)


def test_empty_links_list_is_valid():
    lab.validate_topology_data("t", _topology(links=[]))


def test_links_not_a_list_rejected():
    with pytest.raises(lab.LabConfigError):
        lab.validate_topology_data("t", _topology(links={"a": "R1", "b": "R2"}))


def test_link_entry_not_a_mapping_rejected():
    with pytest.raises(lab.LabConfigError):
        lab.validate_topology_data("t", _topology(links=["not-a-mapping"]))


def test_link_missing_a_rejected():
    with pytest.raises(lab.LabConfigError):
        lab.validate_topology_data("t", _topology(links=[{"b": "R2"}]))


def test_link_missing_b_rejected():
    with pytest.raises(lab.LabConfigError):
        lab.validate_topology_data("t", _topology(links=[{"a": "R1"}]))


def test_link_empty_endpoint_rejected():
    with pytest.raises(lab.LabConfigError):
        lab.validate_topology_data("t", _topology(links=[{"a": "", "b": "R2"}]))


def test_link_unknown_endpoint_device_rejected():
    with pytest.raises(lab.LabConfigError):
        lab.validate_topology_data("t", _topology(links=[{"a": "R1", "b": "R9"}]))


def test_link_malformed_interface_field_rejected():
    with pytest.raises(lab.LabConfigError):
        lab.validate_topology_data("t", _topology(links=[{"a": "R1", "b": "R2", "a_interface": ""}]))


def test_link_non_string_interface_field_rejected():
    with pytest.raises(lab.LabConfigError):
        lab.validate_topology_data("t", _topology(links=[{"a": "R1", "b": "R2", "a_interface": 123}]))


def test_valid_parallel_links_between_same_devices_accepted():
    lab.validate_topology_data(
        "t",
        _topology(
            links=[
                {"a": "R1", "a_interface": "Gi0/0/0/2", "b": "R2", "b_interface": "Gi0/0/0/2"},
                {"a": "R1", "a_interface": "Gi0/0/0/3", "b": "R2", "b_interface": "Gi0/0/0/3"},
            ]
        ),
    )


def test_valid_discovery_generated_link_shape_accepted():
    lab.validate_topology_data(
        "t",
        _topology(
            links=[{"a": "R1", "a_interface": "GigabitEthernet0/0/0/2", "b": "R2", "b_interface": "GigabitEthernet0/0/0/2"}]
        ),
    )


def test_exact_duplicate_link_rejected():
    link = {"a": "R1", "a_interface": "Gi0/0/0/2", "b": "R2", "b_interface": "Gi0/0/0/2"}
    with pytest.raises(lab.LabConfigError):
        lab.validate_topology_data("t", _topology(links=[dict(link), dict(link)]))


def test_duplicate_link_with_reversed_endpoint_order_rejected():
    with pytest.raises(lab.LabConfigError):
        lab.validate_topology_data(
            "t",
            _topology(
                links=[
                    {"a": "R1", "a_interface": "Gi0/0/0/2", "b": "R2", "b_interface": "Gi0/0/0/2"},
                    {"a": "R2", "a_interface": "Gi0/0/0/2", "b": "R1", "b_interface": "Gi0/0/0/2"},
                ]
            ),
        )


def test_no_interface_links_between_same_pair_are_still_duplicates_if_identical():
    with pytest.raises(lab.LabConfigError):
        lab.validate_topology_data("t", _topology(links=[{"a": "R1", "b": "R2"}, {"a": "R1", "b": "R2"}]))


# ---- malformed topology cannot be committed ----


def test_malformed_links_cannot_be_written_to_disk(lab_root):
    with pytest.raises(lab.LabConfigError):
        lab.write_topology("bad_links", _topology(links=[{"a": "R1", "b": "R9"}]), lab_root)
    assert not lab.topology_exists("bad_links", lab_root)


def test_malformed_topology_cannot_be_committed_via_cli(lab_root):
    from network_lab_mcp.cli import config as cfgmod

    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("new_bad_topology"))
    session.definition_candidate["links"] = [{"a": "R1", "b": "does-not-exist"}]
    session.definition_candidate["devices"] = {"R1": {"type": "iosxr"}}

    with pytest.raises(cfgmod.CommitValidationError):
        session.commit()
    assert not lab.topology_exists("new_bad_topology", lab_root)


# ---- external-editor malformed YAML path (topology mode's `edit`) ----


def test_external_editor_valid_links_are_accepted_at_candidate_replace(lab_root):
    from network_lab_mcp.cli import config as cfgmod

    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("edited_topology"))
    edited = {
        "name": "edited_topology",
        "description": "",
        "devices": {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}},
        "links": [{"a": "R1", "b": "R2"}],
    }
    session.replace_definition_candidate(edited)
    assert session.definition_candidate == edited


def test_external_editor_malformed_endpoint_is_rejected_immediately(lab_root):
    """replace_definition_candidate() (the external-editor round trip's
    install step) validates eagerly with the exact same SSOT validator
    commit() uses -- an invalid edit never even reaches the candidate, let
    alone disk."""
    from network_lab_mcp.cli import config as cfgmod

    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("edited_topology_2"))
    original_candidate = session.definition_candidate

    with pytest.raises(lab.LabConfigError):
        session.replace_definition_candidate(
            {
                "name": "edited_topology_2",
                "description": "",
                "devices": {"R1": {"type": "iosxr"}},
                "links": [{"a": "R1", "b": "UNKNOWN"}],
            }
        )

    assert session.definition_candidate == original_candidate  # rejected edit never installed
    assert not lab.topology_exists("edited_topology_2", lab_root)
