# SPDX-License-Identifier: Apache-2.0
"""lab.py regression tests: topology/access-info/scenario/reference loading
and validation, the device.type enum SSOT, and MCP-facing device access
resolution (including the temporary global-uniqueness limitation and the
topology/access-info type-mismatch fail-closed check)."""

from __future__ import annotations

import pytest

from network_lab_mcp import lab


def test_load_topology_valid(lab_root):
    topology = lab.load_topology("sample_lab", lab_root)
    assert topology["name"] == "sample_lab"
    assert set(topology["devices"]) == {"R1", "R2"}


def test_load_topology_rejects_invalid_device_name(lab_root):
    (lab_root / "topologies" / "broken.yaml").write_text(
        "name: broken\ndevices:\n  '':\n    type: iosxr\nlinks: []\n",
        encoding="utf-8",
    )
    with pytest.raises(lab.LabConfigError):
        lab.load_topology("broken", lab_root)


# ---- topology must not carry private access fields ----


def test_load_topology_rejects_access_fields(lab_root):
    for field, value in (
        ("address", "192.0.2.1"),
        ("transport", "ssh"),
        ("port", 22),
        ("username", "example-user"),
        ("password", "example-password"),
    ):
        name = f"has_{field}"
        (lab_root / "topologies" / f"{name}.yaml").write_text(
            f"name: {name}\ndevices:\n  R1:\n    type: iosxr\n    {field}: {value}\nlinks: []\n",
            encoding="utf-8",
        )
        with pytest.raises(lab.LabConfigError, match="access field"):
            lab.load_topology(name, lab_root)


def test_write_topology_rejects_access_fields(lab_root):
    data = {"name": "bad", "devices": {"R1": {"type": "iosxr", "password": "secret"}}, "links": []}
    with pytest.raises(lab.LabConfigError, match="access field"):
        lab.write_topology("bad", data, lab_root)
    assert not (lab_root / "topologies" / "bad.yaml").exists()


# ---- device.type enum ----


def test_normalize_device_type_accepts_exact_and_case_insensitive():
    assert lab.normalize_device_type("iosxr") == "iosxr"
    assert lab.normalize_device_type("IOSXE") == "iosxe"
    assert lab.normalize_device_type("Ios") == "ios"
    assert lab.normalize_device_type("NxOs") == "nxos"
    assert lab.normalize_device_type("host") == "host"
    assert lab.normalize_device_type("HOST") == "host"
    assert lab.normalize_device_type("Host") == "host"


def test_normalize_device_type_accepts_unambiguous_abbreviation():
    assert lab.normalize_device_type("nx") == "nxos"
    assert lab.normalize_device_type("h") == "host"


def test_normalize_device_type_exact_ios_wins_over_abbreviation_ambiguity():
    """Exact match must win before abbreviation resolution --
    `ios` is never rejected merely because it is also a prefix of
    `iosxr`/`iosxe`."""
    assert lab.normalize_device_type("ios") == "ios"


@pytest.mark.parametrize("value", ["i", "io"])
def test_normalize_device_type_rejects_ambiguous_short_prefix(value):
    with pytest.raises(lab.LabConfigError, match="Ambiguous"):
        lab.normalize_device_type(value)


def test_normalize_device_type_rejects_ambiguous_iosx_between_iosxr_iosxe():
    with pytest.raises(lab.LabConfigError, match="Ambiguous") as exc_info:
        lab.normalize_device_type("iosx")
    assert "iosxr" in str(exc_info.value) and "iosxe" in str(exc_info.value)


def test_normalize_device_type_rejects_unknown_value():
    with pytest.raises(lab.LabConfigError, match="Invalid device type"):
        lab.normalize_device_type("junos")


def test_load_topology_accepts_all_supported_device_types(lab_root):
    for device_type in ("iosxr", "iosxe", "ios", "nxos", "host"):
        path = lab_root / "topologies" / f"types_{device_type}.yaml"
        path.write_text(
            f"name: types_{device_type}\ndevices:\n  R1:\n    type: {device_type}\nlinks: []\n",
            encoding="utf-8",
        )
        topology = lab.load_topology(f"types_{device_type}", lab_root)
        assert topology["devices"]["R1"]["type"] == device_type


def test_load_topology_allows_missing_device_type(lab_root):
    (lab_root / "topologies" / "no_type.yaml").write_text(
        "name: no_type\ndevices:\n  R1: {}\nlinks: []\n",
        encoding="utf-8",
    )
    topology = lab.load_topology("no_type", lab_root)
    assert "type" not in topology["devices"]["R1"]


def test_load_topology_rejects_unsupported_device_type(lab_root):
    for index, bad_type in enumerate(("junos", "linux", "router", "switch", "generic", "none")):
        name = f"bad_type_{index}"
        (lab_root / "topologies" / f"{name}.yaml").write_text(
            f"name: {name}\ndevices:\n  R1:\n    type: {bad_type}\nlinks: []\n",
            encoding="utf-8",
        )
        with pytest.raises(lab.LabConfigError, match="Invalid device type"):
            lab.load_topology(name, lab_root)


# ---- access-info ----


def test_load_access_info_valid(lab_root):
    access = lab.load_access_info("sample_lab", lab_root)
    assert access["name"] == "sample_lab"
    assert access["devices"]["R1"]["address"] == "192.0.2.11"


def test_access_info_allows_full_field_set(lab_root):
    # access-info is exactly where address/transport/port/username/password
    # belong -- unlike topology, none of these are rejected.
    data = {
        "name": "full",
        "devices": {
            "R1": {
                "type": "iosxr",
                "address": "192.0.2.1",
                "transport": "ssh",
                "port": 22,
                "username": "u",
                "password": "p",
            }
        },
    }
    lab.write_access_info("full", data, lab_root)
    assert lab.load_access_info("full", lab_root) == data


def test_access_info_rejects_unsupported_device_type(lab_root):
    data = {"name": "bad", "devices": {"R1": {"type": "junos"}}}
    with pytest.raises(lab.LabConfigError, match="Invalid device type"):
        lab.write_access_info("bad", data, lab_root)


@pytest.mark.parametrize("transport", ["ssh", "telnet"])
def test_access_info_accepts_type_ios_over_ssh_or_telnet(lab_root, transport):
    """Classic Cisco IOS is a first-class type, with the same
    structural rules (credential/transport/port schema unchanged) as
    every other network-device type -- sanitized fake credentials only,
    never real PAGENT access-info content."""
    data = {
        "name": "with_ios",
        "devices": {
            "TEST-IOS": {
                "type": "ios",
                "address": "192.0.2.99",
                "transport": transport,
                "username": "fake-user",
                "password": "fake-password",
            }
        },
    }
    lab.write_access_info("with_ios", data, lab_root)
    assert lab.load_access_info("with_ios", lab_root)["devices"]["TEST-IOS"]["type"] == "ios"
    assert not (lab_root / "access-info" / "bad.yaml").exists()


def test_list_and_exists_access_info(lab_root):
    assert lab.list_access_info_names(lab_root) == ["sample_lab"]
    assert lab.access_info_exists("sample_lab", lab_root)
    assert not lab.access_info_exists("does-not-exist", lab_root)


# ---- MCP-facing reads ----


def test_get_active_topology_and_execution_instructions(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    active = lab.get_active_topology()
    assert active["active_topology"] == "sample_lab"
    assert "R1" in active["topology"]["devices"]
    # Topology is safe logical data: never address/username/password.
    assert "address" not in active["topology"]["devices"]["R1"]
    assert "password" not in active["topology"]["devices"]["R1"]

    instructions = lab.get_execution_instructions()
    assert instructions["scenario"]["name"] == "sample"
    assert [r["name"] for r in instructions["references"]] == ["sample"]


def test_get_device_resolves_selected_access_info(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    topology_name, access = lab.get_device("R1")
    assert topology_name == "sample_lab"
    assert access["address"] == "192.0.2.11"

    with pytest.raises(lab.LabConfigError):
        lab.get_device("does-not-exist")


def test_get_device_fails_closed_when_device_absent_from_selected_access_info(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    (lab_root / "topologies" / "sample_lab.yaml").write_text(
        "name: sample_lab\ndevices:\n  R1: {}\n  NOACCESS: {}\nlinks: []\n",
        encoding="utf-8",
    )
    with pytest.raises(lab.LabConfigError, match="not present in access-info"):
        lab.get_device("NOACCESS")


def test_get_device_fails_closed_when_no_access_info_selected(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    settings = lab.read_settings(lab_root)
    del settings["active_access_info"]
    lab.write_settings(settings, lab_root)
    with pytest.raises(lab.LabConfigError, match="No access-info is selected"):
        lab.get_device("R1")


def test_get_device_fails_closed_when_selected_access_info_missing(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    settings = lab.read_settings(lab_root)
    settings["active_access_info"] = "does-not-exist"
    lab.write_settings(settings, lab_root)
    with pytest.raises(lab.LabConfigError, match="does not exist"):
        lab.get_device("R1")


def test_get_device_resolves_topology_and_access_info_from_one_settings_snapshot(lab_root, monkeypatch):
    """get_device() must read settings.yaml exactly once and derive both
    the active topology name and the active access-info name from that
    single snapshot -- never two independent reads that could straddle a
    concurrent human-CLI commit and resolve topology from one running-
    config selection and access-info from a different, later one."""
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    real_read_settings = lab.read_settings
    call_count = 0

    def counting_read_settings(root=None):
        nonlocal call_count
        call_count += 1
        return real_read_settings(root)

    monkeypatch.setattr(lab, "read_settings", counting_read_settings)

    lab.get_device("R1")

    assert call_count == 1


def test_old_global_ambiguity_search_no_longer_exists():
    # The old temporary global device-ID uniqueness limitation (a
    # cross-file search over every committed access-info definition) is
    # removed entirely, not merely bypassed -- resolution now only ever
    # reads the one access-info definition selected in running-config.
    assert not hasattr(lab, "resolve_device_access")


def test_duplicate_device_ids_across_access_info_files_are_now_allowed(lab_root, monkeypatch):
    # Two committed access-info definitions both containing "R1" is no
    # longer ambiguous: only the one *selected* in running-config is ever
    # read, so there is no cross-file search to be ambiguous about.
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    lab.write_access_info("lab_a", {"name": "lab_a", "devices": {"R1": {"address": "10.0.0.1"}}}, lab_root)
    lab.write_access_info("lab_b", {"name": "lab_b", "devices": {"R1": {"address": "10.0.0.2"}}}, lab_root)

    settings = lab.read_settings(lab_root)
    settings["active_access_info"] = "lab_a"
    lab.write_settings(settings, lab_root)
    _, access = lab.get_device("R1")
    assert access["address"] == "10.0.0.1"

    settings["active_access_info"] = "lab_b"
    lab.write_settings(settings, lab_root)
    _, access = lab.get_device("R1")
    assert access["address"] == "10.0.0.2"


def test_missing_active_access_info_is_a_valid_legacy_settings_state(lab_root):
    # A settings.yaml written before this field existed must still load.
    settings = lab.read_settings(lab_root)
    del settings["active_access_info"]
    assert lab.get_active_access_info_name(settings) is None


# ---- topology / access-info device.type consistency ----


def test_get_device_rejects_type_mismatch(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    (lab_root / "topologies" / "sample_lab.yaml").write_text(
        "name: sample_lab\ndevices:\n  R1:\n    type: iosxr\nlinks: []\n",
        encoding="utf-8",
    )
    lab.write_access_info("sample_lab", {"name": "sample_lab", "devices": {"R1": {"type": "nxos", "password": "secret"}}}, lab_root)
    with pytest.raises(lab.LabConfigError) as exc_info:
        lab.get_device("R1")
    message = str(exc_info.value)
    assert "mismatch" in message
    assert "secret" not in message


def test_get_device_allows_matching_type_case_insensitive(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    (lab_root / "topologies" / "sample_lab.yaml").write_text(
        "name: sample_lab\ndevices:\n  R1:\n    type: IOSXR\nlinks: []\n",
        encoding="utf-8",
    )
    lab.write_access_info("sample_lab", {"name": "sample_lab", "devices": {"R1": {"type": "iosxr"}}}, lab_root)
    topology_name, access = lab.get_device("R1")
    assert topology_name == "sample_lab"


def test_get_device_allows_type_missing_on_either_side(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    (lab_root / "topologies" / "sample_lab.yaml").write_text(
        "name: sample_lab\ndevices:\n  R1: {}\nlinks: []\n",
        encoding="utf-8",
    )
    lab.write_access_info("sample_lab", {"name": "sample_lab", "devices": {"R1": {"type": "nxos"}}}, lab_root)
    # Topology has no type at all -- no mismatch to detect, existing policy
    # (missing type is allowed) is not changed by this check.
    topology_name, access = lab.get_device("R1")
    assert access["type"] == "nxos"
