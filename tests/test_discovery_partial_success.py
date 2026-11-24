# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Partial-success Discovery and the non-destructive merge, end to end through
the real CLI handler: Device Registration vs Device/Link Discovery, per-device
failures, the Device Discovery Summary / Topology Changes output, candidate
protection, and the explicit `no topology` rebuild. Collectors are faked at
the `_bootstrap_collect*` level (no terminal, tmux, ssh or device) and the
isolated `lab_root` fixture is used throughout -- the real lab/ is never read.

Acceptance IDs (D/P/M/L/C/S) are given in each test's comment/name."""

from __future__ import annotations

import pytest

from network_lab_mcp import discovery, hostkey_flow, lab, terminal
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain

LLDP_HDR = "Device ID       Local Intf                      Hold-time  Capability      Port ID"
CDP_HDR = "Device ID        Local Intrfce     Holdtme    Capability  Platform  Port ID"
LLDP_OFF = "% LLDP is not enabled\n"
CDP_OFF = "% CDP is not enabled\n"
LLDP_EMPTY = f"{LLDP_HDR}\nTotal entries displayed: 0\n"
CDP_EMPTY = f"{CDP_HDR}\n"
L3_HDR = "Interface                     IP-Address   Status Protocol Vrf-Name"
SENTINEL = "SENTINEL-pw-9d41"
NAME = "fresh_lab"  # a topology that is not committed and not the active one


def lldp(*rows):
    body = "\n".join(f"{r[0]}  {r[1]}  120  R  {r[2]}" for r in rows)
    return f"{LLDP_HDR}\n{body}\n\nTotal entries displayed: {len(rows)}\n"


def info(device_id, lldp_text=LLDP_EMPTY, cdp_text=CDP_EMPTY, l3=None):
    out = {
        "hostname": f"HOST-{device_id}",
        "show_version": "x",
        "show_lldp_neighbors": lldp_text,
        "show_cdp_neighbors": cdp_text,
    }
    if l3 is not None:
        out["show_ipv4_interface_brief"] = l3
    return out


@pytest.fixture(autouse=True)
def _env(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    closed: list[str] = []
    monkeypatch.setattr(terminal, "close_bootstrap_terminal", lambda d: closed.append(d))
    return closed


def _access(lab_root, devices, jump_hosts=None):
    """Write access-info NAME (selected as the discovery source). `devices`:
    {id: type-or-None | full dict}."""
    resolved = {}
    for i, (device_id, spec) in enumerate(devices.items()):
        if isinstance(spec, dict):
            resolved[device_id] = spec
            continue
        entry = {"address": f"192.0.2.{i + 10}", "transport": "ssh", "password": SENTINEL}
        if spec:
            entry["type"] = spec
        resolved[device_id] = entry
    data = {"name": NAME, "devices": resolved}
    if jump_hosts:
        data["jump_hosts"] = jump_hosts
    lab.write_access_info(NAME, data, lab_root)
    settings = lab.read_settings(lab_root)
    settings["active_access_info"] = NAME
    lab.write_settings(settings, lab_root)


def _collectors(monkeypatch, per_device, calls=None):
    """Fake IOS XR collector: per_device[id] is an info dict, an Exception to
    raise, or a callable(info_kwargs)."""

    def collect(device_id, cfg, include_l3=False):
        if calls is not None:
            calls.append(device_id)
        value = per_device[device_id]
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(discovery, "_bootstrap_collect", collect)


def _session(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    return session


def _discover(session, capsys, line="discover topology"):
    capsys.readouterr()
    ok = climain.execute_command_line(session, line)
    return ok, capsys.readouterr().out


def _fail(device_id, message="boom", **kwargs):
    return discovery.DeviceCollectionError(f"Device '{device_id}': {message}", **kwargs)


# ==========================================================================
# Device Registration
# ==========================================================================


def test_d01_all_connected_all_registered_complete(lab_root, monkeypatch, capsys):
    _access(lab_root, {"R1": "iosxr", "R2": "iosxr"})
    _collectors(monkeypatch, {"R1": info("R1"), "R2": info("R2")})
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok and session.definition_candidate["devices"] == {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}}
    assert "R1       iosxr   Connected    Enabled   Enabled   Complete" in out
    assert "Topology candidate updated." in out and "with limitations" not in out


def test_d02_c03_all_connections_fail_still_a_device_only_candidate(lab_root, monkeypatch, capsys):
    _access(lab_root, {"R1": "iosxr", "R2": "iosxr"})
    _collectors(monkeypatch, {"R1": _fail("R1", "login timed out"), "R2": _fail("R2", "login timed out")})
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok
    assert session.definition_candidate["devices"] == {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}}
    assert session.definition_candidate["links"] == []
    assert "R1       iosxr   Failed       Unknown   Unknown   Partial" in out
    assert "Devices registered       : 2" in out and "Devices connected        : 0" in out
    assert "Devices not connected    : 2" in out and "Devices without protocol : 0" in out  # C13
    assert "Warnings:\n  R1: login timed out\n  R2: login timed out" in out  # C04: every warning
    assert "Topology candidate updated with limitations." in out
    assert not lab.topology_exists(NAME, lab_root)  # candidate only, never auto-committed


def test_d03_d04_some_devices_fail_the_rest_is_reflected(lab_root, monkeypatch, capsys):
    _access(lab_root, {"R1": "iosxr", "R2": "iosxr", "R3": "iosxr"})
    _collectors(
        monkeypatch,
        {
            "R1": info("R1", lldp_text=lldp(("HOST-R3", "Gi0/0/0/1", "Gi0/0/0/1"))),
            "R2": _fail("R2", "authentication failed"),
            "R3": info("R3", lldp_text=lldp(("HOST-R1", "Gi0/0/0/1", "Gi0/0/0/1"))),
        },
    )
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    candidate = session.definition_candidate
    assert ok and sorted(candidate["devices"]) == ["R1", "R2", "R3"]
    assert candidate["links"] == [{"a": "R1", "a_interface": "Gi0/0/0/1", "b": "R3", "b_interface": "Gi0/0/0/1"}]
    assert "Warnings:\n  R2: authentication failed" in out
    assert "Devices with neighbors   : 2" in out and "Discovered links         : 1" in out


def test_d06_typeless_device_is_registered_by_id_only_and_not_attempted(lab_root, monkeypatch, capsys):
    _access(lab_root, {"R1": "iosxr", "R2": None})
    calls: list[str] = []
    _collectors(monkeypatch, {"R1": info("R1")}, calls)
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok and calls == ["R1"]  # no OS guessing, no connection to R2
    assert session.definition_candidate["devices"] == {"R1": {"type": "iosxr"}, "R2": {}}
    assert "R2       -       Not attempted   N/A" in out or "Not attempted" in out
    assert "R2: Device type is not set in access-info; discovery was not attempted." in out


@pytest.mark.parametrize("unsupported", ["nxos", "host"])
def test_d07_unsupported_type_is_registered_with_its_type_and_not_attempted(lab_root, monkeypatch, capsys, unsupported):
    _access(lab_root, {"R1": "iosxr", "X1": unsupported})
    calls: list[str] = []
    _collectors(monkeypatch, {"R1": info("R1")}, calls)
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok and calls == ["R1"]
    assert session.definition_candidate["devices"]["X1"] == {"type": unsupported}
    assert f"X1: Device type '{unsupported}' is not supported for discovery; discovery was not attempted." in out
    assert "X1       " + unsupported in out and "Not attempted" in out


def test_d08_only_unsupported_devices_give_a_device_only_candidate_without_any_connection(lab_root, monkeypatch, capsys):
    _access(lab_root, {"H1": "host", "H2": None})
    calls: list[str] = []
    _collectors(monkeypatch, {}, calls)
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok and calls == []
    assert session.definition_candidate["devices"] == {"H1": {"type": "host"}, "H2": {}}
    assert session.definition_candidate["links"] == []
    assert "Topology candidate updated with limitations." in out


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "iosxr", "transport": "ssh"},  # no address
        {"type": "iosxr", "address": "192.0.2.50"},  # no transport
    ],
)
def test_d09_incomplete_connection_info_is_registered_and_not_attempted(lab_root, monkeypatch, capsys, entry):
    _access(lab_root, {"R1": "iosxr", "R2": entry})
    calls: list[str] = []
    _collectors(monkeypatch, {"R1": info("R1")}, calls)
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok and calls == ["R1"]
    assert session.definition_candidate["devices"]["R2"] == {"type": "iosxr"}
    assert "R2: Connection information is incomplete; discovery was not attempted." in out
    assert "192.0.2.50" not in out


def test_d10_s05_jump_hosts_are_not_registered_and_no_private_data_is_copied(lab_root, monkeypatch, capsys):
    _access(
        lab_root,
        {"R1": {"type": "iosxr", "address": "192.0.2.11", "transport": "ssh", "jump_host": "jump1", "password": SENTINEL}},
        jump_hosts={"jump1": {"address": "198.51.100.7", "username": "jumpuser", "password": SENTINEL}},
    )
    _collectors(monkeypatch, {"R1": info("R1")})
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok and session.definition_candidate["devices"] == {"R1": {"type": "iosxr"}}
    assert "jump1" not in str(session.definition_candidate) and "jump1" not in out
    blob = str(session.definition_candidate) + out
    for secret in (SENTINEL, "192.0.2.11", "198.51.100.7", "jumpuser", "ssh"):
        assert secret not in blob.replace("Discovering topology from access-info", "")


def test_d11_invalid_access_info_refuses_the_candidate_update(lab_root, capsys):
    (lab_root / "access-info" / f"{NAME}.yaml").write_text(
        "name: fresh_lab\ndevices:\n  R1:\n    type: not-a-type\n    address: 192.0.2.1\n    transport: ssh\n"
    )
    settings = lab.read_settings(lab_root)
    settings["active_access_info"] = NAME
    lab.write_settings(settings, lab_root)
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok is False and "% Topology candidate not updated." in out
    assert "Device Discovery Summary" not in out and session.definition_candidate is None


# ==========================================================================
# Fatal / security classification
# ==========================================================================


def test_s01_changed_host_key_is_not_a_warning(lab_root, monkeypatch):
    _access(lab_root, {"R1": "iosxr", "R2": "iosxr"})
    _collectors(
        monkeypatch,
        {"R1": info("R1"), "R2": hostkey_flow.HostKeyChanged("R2", "target", "192.0.2.11", 22)},
    )
    with pytest.raises(hostkey_flow.HostKeyChanged):
        discovery.discover_topology(lab_root)


def test_s03_unexpected_worker_exception_refuses_the_candidate_update(lab_root, monkeypatch, capsys):
    _access(lab_root, {"R1": "iosxr", "R2": "iosxr"})
    _collectors(monkeypatch, {"R1": info("R1"), "R2": RuntimeError("programming error")})
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok is False and "unexpected collection failure" in out and "% Topology candidate not updated." in out
    assert "Device Discovery Summary" not in out and session.definition_candidate is None


def test_s04_failure_output_never_contains_credentials_or_addresses(lab_root, monkeypatch, capsys):
    _access(lab_root, {"R1": "iosxr", "R2": "iosxr"})
    _collectors(monkeypatch, {"R1": info("R1"), "R2": _fail("R2", "Timed out waiting for the device prompt.")})
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok
    for secret in (SENTINEL, "192.0.2.10", "192.0.2.11"):
        assert secret not in out and secret not in str(session.definition_candidate)


# ==========================================================================
# Non-destructive merge through the CLI
# ==========================================================================


def _commit_existing(lab_root, devices, links):
    lab.write_topology(
        NAME, {"name": NAME, "description": "kept", "devices": devices, "links": links}, lab_root
    )


LINK_12 = {"a": "R1", "a_interface": "Gi0/0/0/1", "b": "R2", "b_interface": "Gi0/0/0/1"}


@pytest.mark.parametrize(
    "per_device",
    [
        pytest.param({"R1": _fail("R1"), "R2": info("R2")}, id="one-side-fails"),  # M08
        pytest.param({"R1": _fail("R1"), "R2": _fail("R2")}, id="both-sides-fail"),  # M09
        pytest.param({"R1": info("R1", LLDP_OFF, CDP_OFF), "R2": info("R2", LLDP_OFF, CDP_OFF)}, id="protocols-off"),  # M11
        pytest.param({"R1": info("R1"), "R2": info("R2")}, id="zero-neighbors"),  # M07
    ],
)
def test_existing_links_survive_any_failed_or_empty_observation(lab_root, monkeypatch, capsys, per_device):
    _commit_existing(lab_root, {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}}, [LINK_12])
    _access(lab_root, {"R1": "iosxr", "R2": "iosxr"})
    _collectors(monkeypatch, per_device)
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok and session.definition_candidate["links"] == [LINK_12]
    assert session.definition_candidate["devices"] == {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}}
    assert "Links retained           : 1" in out and "Topology candidate unchanged." in out
    assert lab.load_topology(NAME, lab_root)["links"] == [LINK_12]


def test_m02_new_device_is_added_and_existing_info_is_untouched(lab_root, monkeypatch, capsys):
    _commit_existing(lab_root, {"R1": {"type": "iosxr", "role": "pe"}}, [])
    _access(lab_root, {"R1": "iosxr", "R2": "iosxr"})
    _collectors(monkeypatch, {"R1": info("R1"), "R2": info("R2")})
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert session.definition_candidate["devices"] == {"R1": {"type": "iosxr", "role": "pe"}, "R2": {"type": "iosxr"}}
    assert session.definition_candidate["description"] == "kept"
    assert "Devices added            : 1" in out and "Devices retained         : 1" in out


def test_m10_device_removed_from_access_info_stays_in_the_topology(lab_root, monkeypatch, capsys):
    _commit_existing(lab_root, {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}}, [LINK_12])
    _access(lab_root, {"R1": "iosxr"})
    _collectors(monkeypatch, {"R1": info("R1")})
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok and sorted(session.definition_candidate["devices"]) == ["R1", "R2"]
    assert session.definition_candidate["links"] == [LINK_12]
    assert "Devices registered       : 1" in out  # this run's registration plan, not the topology size
    assert "Devices retained         : 2" in out


def test_m12_repeating_the_same_discovery_changes_nothing(lab_root, monkeypatch, capsys):
    _access(lab_root, {"R1": "iosxr", "R2": "iosxr"})
    per = {
        "R1": info("R1", lldp(("HOST-R2", "Gi0/0/0/1", "Gi0/0/0/1"))),
        "R2": info("R2", lldp(("HOST-R1", "Gi0/0/0/1", "Gi0/0/0/1"))),
    }
    _collectors(monkeypatch, per)
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok and "Links added              : 1" in out
    first = session.definition_candidate
    session.go_to_global()
    ok, out = _discover(session, capsys)
    assert ok and session.definition_candidate == first
    assert "Topology candidate unchanged." in out and "Links added              : 0" in out
    assert "Devices added            : 0" in out and "Devices updated          : 0" in out


def test_m13_new_link_contradicting_a_stored_one_refuses_the_update(lab_root, monkeypatch, capsys):
    _commit_existing(
        lab_root,
        {d: {"type": "iosxr"} for d in ("R1", "R2", "R3")},
        [{"a": "R1", "a_interface": "Gi0/0/0/0", "b": "R2", "b_interface": "Gi0/0/0/0"}],
    )
    _access(lab_root, {d: "iosxr" for d in ("R1", "R2", "R3")})
    _collectors(
        monkeypatch,
        {
            "R1": info("R1", lldp(("HOST-R3", "Gi0/0/0/0", "Gi0/0/0/0"))),
            "R2": info("R2"),
            "R3": info("R3", lldp(("HOST-R1", "Gi0/0/0/0", "Gi0/0/0/0"))),
        },
    )
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok is False
    assert "conflicts with existing link R1 Gi0/0/0/0 <-> R2 Gi0/0/0/0" in out
    assert "% Topology candidate not updated." in out and "Device Discovery Summary" not in out
    assert session.definition_candidate is None
    assert len(lab.load_topology(NAME, lab_root)["links"]) == 1


def test_m15_conflicting_device_type_refuses_the_update(lab_root, monkeypatch, capsys):
    _commit_existing(lab_root, {"R1": {"type": "iosxe"}}, [])
    _access(lab_root, {"R1": "iosxr"})
    _collectors(monkeypatch, {"R1": info("R1")})
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok is False and "Device 'R1': access-info type 'iosxr' conflicts" in out
    assert session.definition_candidate is None


def test_m16_a_typeless_access_info_never_removes_the_stored_type(lab_root, monkeypatch, capsys):
    _commit_existing(lab_root, {"R1": {"type": "iosxr"}}, [])
    _access(lab_root, {"R1": None})
    _collectors(monkeypatch, {})
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok and session.definition_candidate["devices"] == {"R1": {"type": "iosxr"}}
    assert "Topology candidate unchanged." in out


# ==========================================================================
# L3 through the CLI
# ==========================================================================

L3_OK = f"{L3_HDR}\nGigabitEthernet0/0/0/1   10.0.12.1   Up Up default\nGigabitEthernet0/0/0/2   unassigned   Up Up default\n"


def test_l01_l3_is_collected_even_when_no_neighbor_protocol_is_usable(lab_root, monkeypatch, capsys):
    """The measured lab case: LLDP disabled and CDP unsupported, yet the L3
    interfaces are readable -- they are merged, and the device is still Partial."""
    _access(lab_root, {"R1": "iosxr"})
    lnt_version = "Cisco IOS XR Software, Version 26.2.2 LNT\n"
    unsupported = "% Invalid input detected at '^' marker.\n"
    _collectors(monkeypatch, {"R1": info("R1", LLDP_OFF, unsupported, l3=L3_OK) | {"show_version": lnt_version}})
    session = _session(lab_root)
    ok, out = _discover(session, capsys, "discover topology l3")
    assert ok
    assert session.definition_candidate["devices"]["R1"] == {
        "type": "iosxr",
        "interfaces": {"GigabitEthernet0/0/0/1": {"ipv4_address": "10.0.12.1", "vrf": "default"}},
    }
    assert "R1       iosxr   Connected    Disabled   Unsupported   Partial" in out
    assert "Devices without protocol : 1" in out and "R1: No usable neighbor discovery protocol." in out
    assert "L3 interfaces added      : 1" in out and "L3 enrichment            : 1/1 devices, 1 interfaces" in out


def test_l07_a_changed_address_updates_the_field_and_an_unassigned_one_never_blanks_it(lab_root, monkeypatch, capsys):
    _commit_existing(
        lab_root,
        {
            "R1": {
                "type": "iosxr",
                "interfaces": {
                    "GigabitEthernet0/0/0/1": {"ipv4_address": "10.9.9.9", "vrf": "default"},
                    "GigabitEthernet0/0/0/2": {"ipv4_address": "10.0.23.1", "vrf": "default"},
                    "GigabitEthernet0/0/0/3": {"ipv4_address": "10.0.34.1", "vrf": "default"},
                },
            }
        },
        [],
    )
    _access(lab_root, {"R1": "iosxr"})
    _collectors(monkeypatch, {"R1": info("R1", l3=L3_OK)})
    session = _session(lab_root)
    ok, out = _discover(session, capsys, "discover topology l3")
    interfaces = session.definition_candidate["devices"]["R1"]["interfaces"]
    assert interfaces["GigabitEthernet0/0/0/1"]["ipv4_address"] == "10.0.12.1"  # L07: updated
    assert interfaces["GigabitEthernet0/0/0/2"]["ipv4_address"] == "10.0.23.1"  # L08: `unassigned` != unset
    assert interfaces["GigabitEthernet0/0/0/3"]["ipv4_address"] == "10.0.34.1"  # L06: not listed, kept
    assert "L3 interfaces updated    : 1" in out and "L3 interfaces retained   : 2" in out


def test_l10_l3_command_cut_short_by_a_session_end_reflects_nothing_from_it(lab_root, monkeypatch, capsys):
    _access(lab_root, {"R1": "iosxr"})
    partial = info("R1", lldp(("HOST-R2", "Gi0/0/0/1", "Gi0/0/0/1")))  # login + identity + LLDP + CDP completed
    _collectors(
        monkeypatch,
        {"R1": _fail("R1", "Session 'network-lab-discovery-R1' exited while waiting for expected output.", collected=partial, session_ended=True)},
    )
    session = _session(lab_root)
    ok, out = _discover(session, capsys, "discover topology l3")
    assert ok and "interfaces" not in session.definition_candidate["devices"]["R1"]
    assert "R1       iosxr   Connected    Enabled   Enabled   Partial" in out  # C15: still Connected, but Partial
    assert "R1: Session 'network-lab-discovery-R1' exited while waiting for expected output." in out
    assert "L3 enrichment            : 0/1 devices, 0 interfaces" in out


# ==========================================================================
# CLI output details
# ==========================================================================


def test_c14_a_long_device_id_and_type_keep_the_table_aligned(lab_root, monkeypatch, capsys):
    long_id = "PE-ROUTER-NYC-DATACENTER-RACK42-01"
    _access(lab_root, {long_id: "iosxr", "R2": "iosxe"})
    _collectors(monkeypatch, {long_id: info(long_id)})
    monkeypatch.setattr(discovery, "_bootstrap_collect_iosxe", lambda d, c, include_l3=False: info(d))
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    rows = [line for line in out.splitlines() if "Connected" in line or "Connection" in line]
    assert len(rows) == 3  # header + two devices
    assert len({line.index("Connection") if "Connection" in line else line.index("Connected") for line in rows}) == 1
    assert len({line.index("Status") if "Status" in line else line.index("Complete") for line in rows}) == 1
    assert long_id in out


def test_c06_candidate_validation_failure_leaves_everything_untouched(lab_root, monkeypatch, capsys):
    _access(lab_root, {"R1": "iosxr"})
    _collectors(monkeypatch, {"R1": info("R1")})

    def reject(name, data):
        raise lab.LabConfigError("simulated invalid topology")

    monkeypatch.setattr(lab, "validate_topology_data", reject)
    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok is False and "Discovered topology candidate is invalid: simulated invalid topology" in out
    assert "% Topology candidate not updated." in out and "Device Discovery Summary" not in out
    assert session.definition_candidate is None and session.mode == "global"


def test_c10_ctrl_c_is_not_a_partial_success(lab_root, monkeypatch, capsys):
    _access(lab_root, {"R1": "iosxr"})
    session = _session(lab_root)

    def interrupted(lab_root=None, include_l3=False):
        raise KeyboardInterrupt

    monkeypatch.setattr(discovery, "discover_topology", interrupted)
    ok, out = _discover(session, capsys)
    assert "% Discovery interrupted." in out and "% Topology candidate not updated." in out
    assert "Device Discovery Summary" not in out
    assert session.definition_candidate is None and session.mode == "global"


# ==========================================================================
# Explicit rebuild: `no topology` + commit, then discover again
# ==========================================================================


def test_m17_rebuild_requires_an_explicit_no_topology_and_commit(lab_root, monkeypatch, capsys):
    _access(lab_root, {"R1": "iosxr", "R2": "iosxr", "R3": "iosxr"})
    _collectors(monkeypatch, {d: info(d) for d in ("R1", "R2", "R3")})
    session = _session(lab_root)
    assert _discover(session, capsys)[0]
    assert climain.execute_command_line(session, "commit")
    assert sorted(lab.load_topology(NAME, lab_root)["devices"]) == ["R1", "R2", "R3"]

    # R3 leaves the access-info: a rediscovery keeps it (non-destructive).
    _access(lab_root, {"R1": "iosxr", "R2": "iosxr"})
    session = _session(lab_root)
    assert _discover(session, capsys)[0]
    assert sorted(session.definition_candidate["devices"]) == ["R1", "R2", "R3"]
    assert climain.execute_command_line(session, "clear")

    # The documented full rebuild: delete explicitly, commit, discover again.
    session = _session(lab_root)
    assert climain.execute_command_line(session, f"no topology {NAME}")
    assert climain.execute_command_line(session, "commit")
    assert not lab.topology_exists(NAME, lab_root)
    session = _session(lab_root)
    assert _discover(session, capsys)[0]
    assert sorted(session.definition_candidate["devices"]) == ["R1", "R2"]
    assert not lab.topology_exists(NAME, lab_root)  # still only a candidate


def test_deleting_the_active_topology_is_still_refused_at_commit(lab_root, capsys):
    """Existing protection, unchanged: the active topology cannot be removed
    (select another topology in running-config first), so a rebuild of the
    active topology needs that step before `no topology`."""
    session = _session(lab_root)
    assert lab.read_settings(lab_root)["active_topology"] == "sample_lab"
    climain.execute_command_line(session, "no topology sample_lab")
    capsys.readouterr()
    climain.execute_command_line(session, "commit")
    assert "Cannot remove topology 'sample_lab' because it is active in running-config." in capsys.readouterr().out
    assert lab.topology_exists("sample_lab", lab_root)


def test_m17_rebuilding_the_active_topology_needs_another_topology_selected_first(lab_root, monkeypatch, capsys):
    """The exact documented procedure for an *active* topology: select a
    different topology (running-config), commit, then `no topology <name>`,
    commit, discover, review, commit (and re-select it if desired)."""
    lab.write_topology("other_lab", {"name": "other_lab", "description": "", "devices": {"R1": {}}, "links": []}, lab_root)
    settings = lab.read_settings(lab_root)
    settings["active_access_info"] = "sample_lab"
    lab.write_settings(settings, lab_root)
    lab.write_access_info(
        "sample_lab",
        {"name": "sample_lab", "devices": {"R1": {"type": "iosxr", "address": "192.0.2.11", "transport": "ssh"}}},
        lab_root,
    )
    _collectors(monkeypatch, {"R1": info("R1")})

    session = _session(lab_root)
    for line in ("running-config", "topology other_lab", "exit", "commit"):
        assert climain.execute_command_line(session, line), line
    assert lab.read_settings(lab_root)["active_topology"] == "other_lab"

    session = _session(lab_root)
    for line in ("no topology sample_lab", "commit"):
        assert climain.execute_command_line(session, line), line
    assert not lab.topology_exists("sample_lab", lab_root)

    session = _session(lab_root)
    ok, out = _discover(session, capsys)
    assert ok and session.definition_candidate["devices"] == {"R1": {"type": "iosxr"}}
    assert session.definition_candidate["links"] == []
    assert climain.execute_command_line(session, "commit")
    assert lab.load_topology("sample_lab", lab_root)["devices"] == {"R1": {"type": "iosxr"}}
    assert lab.read_settings(lab_root)["active_topology"] == "other_lab"  # never auto-selected


# ==========================================================================
# MCP / terminal compatibility with partial topologies
# ==========================================================================


def test_s11_s12_a_device_only_topology_works_on_the_mcp_read_path(lab_root, monkeypatch):
    """A topology with typeless devices, no links and no interfaces (what a
    failed or unsupported discovery yields) loads, is returned by
    get_active_topology() and resolves a device for terminal_open() -- no
    schema change was needed."""
    lab.write_topology(
        "partial_lab",
        {"name": "partial_lab", "description": "", "devices": {"R1": {"type": "iosxr"}, "R2": {}}, "links": []},
        lab_root,
    )
    lab.write_access_info(
        "partial_lab",
        {
            "name": "partial_lab",
            "devices": {
                "R1": {"type": "iosxr", "address": "192.0.2.11", "transport": "ssh"},
                "R2": {"address": "192.0.2.12", "transport": "ssh"},
            },
        },
        lab_root,
    )
    settings = lab.read_settings(lab_root)
    settings.update({"active_topology": "partial_lab", "active_access_info": "partial_lab"})
    lab.write_settings(settings, lab_root)

    active = lab.get_active_topology()
    assert active["active_topology"] == "partial_lab"
    assert active["topology"]["devices"] == {"R1": {"type": "iosxr"}, "R2": {}}
    assert "address" not in str(active)  # never any access-info
    for device in ("R1", "R2"):
        topology_name, device_config = lab.get_device(device)
        assert topology_name == "partial_lab" and device_config["transport"] == "ssh"


def test_s08_discovery_cleanup_never_touches_a_managed_session_of_the_same_device(monkeypatch):
    """close_bootstrap_terminal() (Discovery's only cleanup) acts on the
    Discovery-namespace session; a managed session for the same device name
    lives in a different namespace and survives."""
    import subprocess

    monkeypatch.undo()  # this test needs the real close_bootstrap_terminal (autouse fixture patched it)
    managed = terminal.derive_production_session_name("R1")
    try:
        terminal._ensure_managed_session(managed, ["cat"])
        assert terminal.close_bootstrap_terminal("R1") is False  # no Discovery session existed
        alive = subprocess.run(
            ["tmux", "-L", terminal.TMUX_SOCKET_NAME, "has-session", "-t", managed], capture_output=True
        )
        assert alive.returncode == 0
    finally:
        terminal._close_session(managed)
