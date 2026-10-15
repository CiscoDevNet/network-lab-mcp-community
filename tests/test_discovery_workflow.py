"""`discover topology` candidate workflow: reuses the existing candidate/
commit/clear system exactly like a manually typed `topology <name>` --
never a separate Discovery datastore. `discovery.discover_topology()`
(the real network/tmux operation) is monkeypatched throughout; these
tests exercise only cli/config.py's candidate merge and cli/main.py's
handler/rendering, using the isolated `lab_root` fixture."""

from __future__ import annotations

import pytest

from network_lab_mcp import discovery, lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain


def _fake_result(
    *,
    default_topology_name="discovered_lab",
    devices=None,
    managed_links=None,
    unresolved=None,
    conflicts=None,
):
    return discovery.DiscoveryResult(
        access_info_name="sample_lab",
        default_topology_name=default_topology_name,
        iosxr_target_count=2,
        connected_count=2,
        observation_count=2,
        devices=devices if devices is not None else {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}},
        managed_links=managed_links
        if managed_links is not None
        else [discovery.ManagedLink("R1", "GigabitEthernet0/0/0/2", "R2", "GigabitEthernet0/0/0/2")],
        unresolved=unresolved or [],
        conflicts=conflicts or [],
        identity_map={"R1": "HOST-R1", "R2": "HOST-R2"},
    )


def _run_discover(session, monkeypatch, result):
    monkeypatch.setattr(discovery, "discover_topology", lambda lab_root=None, include_l3=False: result)
    climain.execute_command_line(session, "discover topology")


@pytest.fixture(autouse=True)
def _patch_lab_root(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)


# ---- new topology candidate (no existing committed topology) ----


def test_new_target_topology_creates_candidate_only(lab_root, monkeypatch):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    _run_discover(session, monkeypatch, _fake_result())

    assert session.mode == "topology"
    assert session.definition_name == "discovered_lab"
    assert session.definition_candidate["devices"]["R1"]["type"] == "iosxr"
    assert session.definition_candidate["devices"]["R2"]["type"] == "iosxr"
    assert len(session.definition_candidate["links"]) == 1
    assert not lab.topology_exists("discovered_lab", lab_root)  # not written to disk


def test_new_target_topology_show_running_config_is_empty_before_commit(lab_root, monkeypatch, capsys):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    _run_discover(session, monkeypatch, _fake_result())
    capsys.readouterr()

    climain.execute_command_line(session, "show running-config")
    assert capsys.readouterr().out == ""


def test_clear_after_discovery_discards_candidate_and_writes_nothing(lab_root, monkeypatch):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    _run_discover(session, monkeypatch, _fake_result())

    climain.execute_command_line(session, "clear")
    assert session.definition_candidate is None
    assert session.mode == "global"
    assert not lab.topology_exists("discovered_lab", lab_root)


def test_commit_persists_discovered_topology_but_does_not_select_it(lab_root, monkeypatch):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    _run_discover(session, monkeypatch, _fake_result())

    climain.execute_command_line(session, "commit")
    assert lab.topology_exists("discovered_lab", lab_root)
    persisted = lab.load_topology("discovered_lab", lab_root)
    assert persisted["devices"]["R1"]["type"] == "iosxr"
    # No access-info credentials leak into the topology candidate/commit.
    assert "address" not in persisted["devices"]["R1"]
    assert "password" not in persisted["devices"]["R1"]

    # discover topology never selects active_topology itself.
    settings = lab.read_settings(lab_root)
    assert settings["active_topology"] != "discovered_lab"


def test_mcp_visibility_unchanged_until_explicit_running_config_selection(lab_root, monkeypatch):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    before = lab.get_active_topology()["active_topology"]

    _run_discover(session, monkeypatch, _fake_result())
    assert lab.get_active_topology()["active_topology"] == before  # candidate only, MCP unaffected

    climain.execute_command_line(session, "commit")
    assert lab.get_active_topology()["active_topology"] == before  # committed but not selected

    session.mode = "running"
    climain.execute_command_line(session, "topology discovered_lab")
    climain.execute_command_line(session, "commit")
    assert lab.get_active_topology()["active_topology"] == "discovered_lab"


# ---- existing topology merge (target name already committed) ----


def test_existing_topology_rebuild_preserves_description_but_drops_unmanaged_devices(lab_root, monkeypatch, capsys):
    lab.write_topology(
        "sample_lab",
        {
            "name": "sample_lab",
            "description": "hand-authored description",
            "devices": {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}, "PC1": {"type": "host"}},
            "links": [{"a": "R1", "b": "R2"}],
        },
        lab_root,
    )
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    _run_discover(
        session,
        monkeypatch,
        _fake_result(
            default_topology_name="sample_lab",
            managed_links=[discovery.ManagedLink("R1", "GigabitEthernet0/0/0/2", "R2", "GigabitEthernet0/0/0/2")],
        ),
    )

    candidate = session.definition_candidate
    assert candidate["description"] == "hand-authored description"
    # Rebuilt from this Discovery run alone (IC-1): a device that is not in
    # the active access-info and the hand-authored link are NOT carried
    # over; the removal is announced before the candidate is replaced.
    assert "PC1" not in candidate["devices"]
    assert candidate["links"] == [
        {"a": "R1", "a_interface": "GigabitEthernet0/0/0/2", "b": "R2", "b_interface": "GigabitEthernet0/0/0/2"}
    ]
    out = capsys.readouterr().out
    assert "device PC1" in out
    assert "link   R1 <-> R2" in out
    # Nothing is persisted before commit.
    assert lab.load_topology("sample_lab", lab_root)["devices"]["PC1"]["type"] == "host"


def test_existing_topology_merge_does_not_duplicate_an_already_present_discovered_link(lab_root, monkeypatch):
    lab.write_topology(
        "sample_lab",
        {
            "name": "sample_lab",
            "description": "",
            "devices": {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}},
            "links": [
                {"a": "R1", "a_interface": "GigabitEthernet0/0/0/2", "b": "R2", "b_interface": "GigabitEthernet0/0/0/2"}
            ],
        },
        lab_root,
    )
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    _run_discover(
        session,
        monkeypatch,
        _fake_result(
            default_topology_name="sample_lab",
            managed_links=[discovery.ManagedLink("R2", "GigabitEthernet0/0/0/2", "R1", "GigabitEthernet0/0/0/2")],
        ),
    )
    assert len(session.definition_candidate["links"]) == 1  # same link, unordered endpoints -- not duplicated


def test_existing_topology_rebuild_drops_a_link_not_observed_this_run(lab_root, monkeypatch):
    lab.write_topology(
        "sample_lab",
        {
            "name": "sample_lab",
            "description": "",
            "devices": {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}},
            "links": [{"a": "R1", "b": "R2"}],
        },
        lab_root,
    )
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    _run_discover(session, monkeypatch, _fake_result(default_topology_name="sample_lab", managed_links=[]))
    assert session.definition_candidate["links"] == []
    # The committed definition is untouched until commit.
    assert lab.load_topology("sample_lab", lab_root)["links"] == [{"a": "R1", "b": "R2"}]


# ---- no partial candidate mutation on Discovery failure ----


def test_failed_discovery_leaves_prior_state_untouched(lab_root, monkeypatch):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.enter_access_info_device("R1")
    session.set_device_field("port", "2222")
    climain.execute_command_line(session, "root")  # candidate preserved, per root's own semantics
    candidate_before = session.definition_candidate
    mode_before = session.mode
    kind_before = session.definition_kind
    name_before = session.definition_name

    def _raise(lab_root=None, include_l3=False):
        raise discovery.DiscoveryError("simulated device login failure")

    monkeypatch.setattr(discovery, "discover_topology", _raise)
    climain.execute_command_line(session, "discover topology")

    assert session.mode == mode_before
    assert session.definition_kind == kind_before
    assert session.definition_name == name_before
    assert session.definition_candidate == candidate_before


# ---- candidate-switch safety guard ----


def test_discover_topology_blocked_while_a_different_dirty_definition_is_open(lab_root, monkeypatch, capsys):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_scenario_definition("brand_new_scenario")
    session.definition_candidate["description"] = "dirty, uncommitted"
    session.go_to_global()  # `root`'s own effect: back to global, candidate preserved

    monkeypatch.setattr(discovery, "discover_topology", lambda lab_root=None, include_l3=False: _fake_result())
    climain.execute_command_line(session, "discover topology")

    out = capsys.readouterr().out
    assert "Uncommitted changes exist" in out
    assert session.definition_kind == "scenario"  # untouched -- discovery never ran


# ---- topology links are actually visible for review before commit ----


def test_discovered_links_are_visible_via_show_configuration(lab_root, monkeypatch, capsys):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    _run_discover(session, monkeypatch, _fake_result())
    capsys.readouterr()

    climain.execute_command_line(session, "show configuration")
    out = capsys.readouterr().out
    assert "GigabitEthernet0/0/0/2" in out
    assert "R1" in out and "R2" in out


def test_device_scoped_view_does_not_leak_other_devices_via_links(lab_root, monkeypatch, capsys):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    _run_discover(session, monkeypatch, _fake_result())
    session.enter_topology_device("R1")
    capsys.readouterr()

    climain.execute_command_line(session, "show")
    out = capsys.readouterr().out
    assert "R2" not in out


# ---- LLDP parse failure fails Discovery closed, without touching the ----
# ---- prior candidate (see test_discovery_lldp_parser.py for the parser ----
# ---- fail-closed contract itself) ----


def test_lldp_parse_failure_fails_discovery_without_touching_prior_candidate(lab_root, monkeypatch):
    def _fake_bootstrap_collect(device_id, device_config):
        if device_id == "R1":
            return {
                "hostname": "HOST-R1",
                "show_version": "Cisco IOS XR Software",
                "show_running_config": "hostname HOST-R1",
                "show_lldp_neighbors": "% Invalid input detected at '^' marker.\n",
            }
        return {
            "hostname": "HOST-R2",
            "show_version": "Cisco IOS XR Software",
            "show_running_config": "hostname HOST-R2",
            "show_lldp_neighbors": "Device ID       Local Intf                      Hold-time  Capability      Port ID\nTotal entries displayed: 0\n",
        }

    monkeypatch.setattr(discovery, "_bootstrap_collect", _fake_bootstrap_collect)

    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    candidate_before = session.definition_candidate
    mode_before = session.mode
    committed_before = lab.load_topology("sample_lab", lab_root)

    with pytest.raises(discovery.DiscoveryError):
        discovery.discover_topology(lab_root)

    # discover_topology() itself never touches the CLI session/candidate --
    # it only ever returns a DiscoveryResult or raises. Confirm the
    # candidate-application step (session.apply_discovery_result) was never
    # reached by asserting the session is exactly as it was before.
    assert session.definition_candidate == candidate_before
    assert session.mode == mode_before
    # The real committed topology this run would have merged into is also
    # completely untouched -- discover_topology() never writes to disk.
    assert lab.load_topology("sample_lab", lab_root) == committed_before


# ---- `ios` type + L3 interface enrichment through the same ----
# ---- candidate/commit/clear system, no special-casing anywhere         ----


def test_ios_type_and_l3_interfaces_survive_candidate_only_then_clear(lab_root, monkeypatch):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    _run_discover(
        session,
        monkeypatch,
        _fake_result(
            devices={
                "PAGENT": {
                    "type": "ios",
                    "interfaces": {"GigabitEthernet0/0.2000": {"ipv4_address": "10.20.0.10", "vrf": "tgn1"}},
                }
            },
            managed_links=[],
        ),
    )

    assert session.definition_candidate["devices"]["PAGENT"]["type"] == "ios"
    assert session.definition_candidate["devices"]["PAGENT"]["interfaces"]["GigabitEthernet0/0.2000"] == {
        "ipv4_address": "10.20.0.10",
        "vrf": "tgn1",
    }
    assert not lab.topology_exists("discovered_lab", lab_root)

    climain.execute_command_line(session, "clear")
    assert not lab.topology_exists("discovered_lab", lab_root)


def test_ios_type_and_l3_interfaces_survive_commit_and_reload(lab_root, monkeypatch):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    _run_discover(
        session,
        monkeypatch,
        _fake_result(
            devices={
                "PAGENT": {
                    "type": "ios",
                    "interfaces": {"GigabitEthernet0/0.2000": {"ipv4_address": "10.20.0.10", "vrf": "tgn1"}},
                }
            },
            managed_links=[],
        ),
    )
    climain.execute_command_line(session, "commit")

    reloaded = lab.load_topology("discovered_lab", lab_root)
    assert reloaded["devices"]["PAGENT"]["type"] == "ios"
    assert reloaded["devices"]["PAGENT"]["interfaces"]["GigabitEthernet0/0.2000"]["ipv4_address"] == "10.20.0.10"


def test_l3_enrichment_failure_for_one_device_does_not_lose_its_managed_link(lab_root, monkeypatch):
    """L3 enrichment is additive/best-effort -- a
    device with a valid managed link but no L3 result this run (its
    'interfaces' key simply absent from DiscoveryResult.devices) must
    still keep that link and device in the candidate."""
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    _run_discover(
        session,
        monkeypatch,
        _fake_result(devices={"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}}),
    )

    assert len(session.definition_candidate["links"]) == 1
    assert "interfaces" not in session.definition_candidate["devices"]["R1"]
