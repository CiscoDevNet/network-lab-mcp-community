# SPDX-License-Identifier: Apache-2.0
"""Gated real-lab acceptance tests for IOS XR + IOS XE + classic IOS
Discovery (LLDP for IOS XR/IOS XE, CDP for all three).

Skipped by default -- `pytest` alone never touches real hardware. Run
explicitly with:

    NETWORK_LAB_REAL_TESTS=1 pytest tests/test_real_lab_iosxr.py -v

These tests use the real, committed `lab/settings.yaml` /
`lab/access-info/<selected>.yaml` exactly as-is (never write to either) and
open real bootstrap sessions against whatever devices the currently
selected access-info defines. They never print full `show running-config`
output or any credential value."""

from __future__ import annotations

import os

import pytest

from network_lab_mcp import discovery, lab, terminal

pytestmark = pytest.mark.real_lab

if os.environ.get("NETWORK_LAB_REAL_TESTS") != "1":
    pytest.skip(
        "set NETWORK_LAB_REAL_TESTS=1 to run real IOS XR lab acceptance tests",
        allow_module_level=True,
    )


def _real_access_data() -> dict:
    settings = lab.read_settings()
    access_info_name = lab.get_active_access_info_name(settings)
    if not access_info_name:
        pytest.fail("Real lab prerequisite: no access-info is selected in running-config.")
    if not lab.access_info_exists(access_info_name):
        pytest.fail(f"Real lab prerequisite: selected access-info '{access_info_name}' does not exist.")
    return lab.load_access_info(access_info_name)


def _real_iosxr_targets() -> dict[str, dict]:
    return discovery._select_iosxr_targets(_real_access_data())


def _real_iosxe_targets() -> dict[str, dict]:
    return discovery._select_iosxe_targets(_real_access_data())


def _real_ios_targets() -> dict[str, dict]:
    return discovery._select_ios_targets(_real_access_data())


@pytest.fixture(scope="module")
def iosxr_targets() -> dict[str, dict]:
    targets = _real_iosxr_targets()
    if not targets:
        pytest.fail("Real lab prerequisite: no IOS XR targets in the selected access-info.")
    return targets


@pytest.mark.parametrize("device_id", ["R1", "R2", "R3", "R4"])
def test_real_device_login_and_required_commands(device_id, iosxr_targets):
    if device_id not in iosxr_targets:
        pytest.fail(f"Real lab prerequisite: '{device_id}' is missing from the selected access-info.")
    device_config = iosxr_targets[device_id]

    try:
        info = discovery._bootstrap_collect(device_id, device_config)
    finally:
        terminal.close_bootstrap_terminal(device_id)

    assert info["hostname"], f"{device_id}: no hostname resolved from the IOS XR prompt"
    assert info["show_version"].strip(), f"{device_id}: 'show version' returned empty output"
    # 'show running-config' is deliberately not collected (unused
    # downstream; avoids persisting the device's full configuration into
    # the terminal log for no benefit). 'show lldp neighbors' may
    # legitimately report zero neighbors; the command having executed
    # without raising (above) is what's asserted.

    logs = terminal.list_device_logs(device_id)
    assert logs, f"{device_id}: no terminal log file was created"


def test_real_end_to_end_discovery_run():
    result = discovery.discover_topology()

    assert result.connected_count == result.iosxr_target_count + result.iosxe_target_count + result.ios_target_count
    assert result.observation_count >= 0
    assert result.cdp_observation_count >= 0
    assert all(device["type"] in ("iosxr", "iosxe", "ios") for device in result.devices.values())
    for device_id in result.devices:
        assert terminal.list_device_logs(device_id), f"{device_id}: no terminal log after discovery"

    # Never commits or otherwise touches the real running-config/committed
    # topology -- this call only ever returns an in-memory DiscoveryResult.


@pytest.mark.parametrize("device_id", ["SW3"])
def test_real_iosxe_device_login_and_cdp_collection(device_id):
    """Gated, optional: only meaningful if the selected access-info defines
    an IOS XE target. Skips (does not fail) if absent, since IOS XE
    targets are not a hard real-lab prerequisite the way IOS XR ones are."""
    targets = _real_iosxe_targets()
    if device_id not in targets:
        pytest.skip(f"Real lab: no IOS XE target '{device_id}' in the selected access-info.")
    device_config = targets[device_id]

    try:
        info = discovery._bootstrap_collect_iosxe(device_id, device_config)
    finally:
        terminal.close_bootstrap_terminal(device_id)

    assert info["hostname"], f"{device_id}: no hostname resolved from the IOS XE prompt"
    assert info["show_version"].strip(), f"{device_id}: 'show version' returned empty output"
    # 'show cdp neighbors' may legitimately report zero/disabled; the
    # command having executed without raising (above) is what's asserted.


@pytest.mark.parametrize("device_id", ["PAGENT"])
def test_real_ios_device_login_and_cdp_collection(device_id):
    """Gated, optional: only meaningful if the selected access-info defines
    a classic IOS target (e.g. PAGENT). Skips (does not fail) if absent,
    since classic IOS targets are not a hard real-lab prerequisite the way
    IOS XR ones are. Classic IOS has no LLDP support -- CDP only."""
    targets = _real_ios_targets()
    if device_id not in targets:
        pytest.skip(f"Real lab: no classic IOS target '{device_id}' in the selected access-info.")
    device_config = targets[device_id]

    try:
        info = discovery._bootstrap_collect_ios(device_id, device_config)
    finally:
        terminal.close_bootstrap_terminal(device_id)

    assert info["hostname"], f"{device_id}: no hostname resolved from the classic IOS prompt"
    assert info["show_version"].strip(), f"{device_id}: 'show version' returned empty output"
    # 'show cdp neighbors' may legitimately report zero/disabled; the
    # command having executed without raising (above) is what's asserted.
