# SPDX-License-Identifier: Apache-2.0
"""reconcile_links() with mixed LLDP + CDP observations.

The same physical link observed via both protocols (or reciprocally from
both ends, mixing protocols) becomes exactly one topology link -- keyed by
the unordered (device, interface) endpoint pair, generalizing the existing
reciprocal-dedup logic (tests/test_discovery_reconciliation.py) rather than
replacing it. A local interface where LLDP and CDP disagree about the
neighbor is reported as a conflict instead of silently picking one
protocol; unrelated links elsewhere are unaffected. The same rule also
catches two same-protocol observations disagreeing about one local
interface, not just cross-protocol disagreement."""

from __future__ import annotations

from network_lab_mcp.discovery import NeighborObservation


def _lldp(local_dev, local_intf, remote_port_id):
    return NeighborObservation(local_dev, local_intf, "irrelevant-raw-id", remote_port_id, source="lldp")


def _cdp(local_dev, local_intf, remote_port_id):
    return NeighborObservation(local_dev, local_intf, "irrelevant-raw-id", remote_port_id, source="cdp")


def test_same_link_seen_via_both_protocols_dedupes_to_one_link():
    from network_lab_mcp.discovery import reconcile_links

    resolved = [
        (_lldp("R1", "Gi0/0/0/10", "Gig0/0"), "PAGENT"),
        (_cdp("R1", "Gi0/0/0/10", "Gig0/0"), "PAGENT"),
    ]
    links, conflicts = reconcile_links(resolved)
    assert conflicts == []
    assert len(links) == 1
    assert links[0].a_device == "R1" and links[0].a_interface == "Gi0/0/0/10"
    assert links[0].b_device == "PAGENT" and links[0].b_interface == "Gig0/0"


def test_reciprocal_cdp_only_observations_collapse_into_one_link():
    from network_lab_mcp.discovery import reconcile_links

    resolved = [
        (_cdp("R1", "Gi0/0/0/10", "Gig0/0"), "PAGENT"),
        (_cdp("PAGENT", "Gig0/0", "Gi0/0/0/10"), "R1"),
    ]
    links, conflicts = reconcile_links(resolved)
    assert conflicts == []
    assert len(links) == 1


def test_lldp_and_cdp_disagree_about_the_same_local_interface_is_a_conflict():
    from network_lab_mcp.discovery import reconcile_links

    resolved = [
        (_lldp("R1", "Gi0/0/0/10", "port-x"), "SW1"),
        (_cdp("R1", "Gi0/0/0/10", "Gig0/0"), "PAGENT"),
    ]
    links, conflicts = reconcile_links(resolved)
    assert links == []
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.endpoint_a == ("R1", "Gi0/0/0/10")
    assert conflict.endpoint_b == ("R1", "Gi0/0/0/10")
    sources = {conflict.observation_a.source, conflict.observation_b.source}
    assert sources == {"lldp", "cdp"}


def test_two_cdp_rows_disagreeing_about_one_local_interface_is_also_a_conflict():
    """Same-protocol disagreement uses the exact same rule as
    cross-protocol disagreement -- not a separate special case."""
    from network_lab_mcp.discovery import reconcile_links

    resolved = [
        (_cdp("R1", "Gi0/0/0/10", "Gig0/0"), "PAGENT"),
        (_cdp("R1", "Gi0/0/0/10", "Gig0/1"), "PAGENT"),
    ]
    links, conflicts = reconcile_links(resolved)
    assert links == []
    assert len(conflicts) == 1


def test_unrelated_link_is_unaffected_by_a_conflict_elsewhere():
    from network_lab_mcp.discovery import reconcile_links

    resolved = [
        (_lldp("R1", "Gi0/0/0/10", "port-x"), "SW1"),
        (_cdp("R1", "Gi0/0/0/10", "Gig0/0"), "PAGENT"),
        (_lldp("R1", "Gi0/0/0/2", "Gi0/0/0/2"), "R2"),
        (_lldp("R2", "Gi0/0/0/2", "Gi0/0/0/2"), "R1"),
    ]
    links, conflicts = reconcile_links(resolved)
    assert len(conflicts) == 1
    assert len(links) == 1
    assert {(links[0].a_device, links[0].b_device)} == {("R1", "R2")}


def test_full_discover_topology_pipeline_merges_lldp_and_cdp_for_one_pair(lab_root, monkeypatch):
    """End-to-end (discover_topology()) proof that a link visible via both
    protocols across both directions still collapses to exactly one
    ManagedLink, using the real parse_lldp_neighbors/parse_cdp_neighbors +
    reconcile_links pipeline, not hand-built observations."""
    from network_lab_mcp import discovery, lab as labmod

    monkeypatch.setattr(labmod, "find_lab_root", lambda: lab_root)

    devices = {
        "R1": {"type": "iosxr", "address": "192.0.2.11", "transport": "ssh"},
        "R2": {"type": "iosxr", "address": "192.0.2.12", "transport": "ssh"},
    }
    labmod.write_access_info("sample_lab", {"name": "sample_lab", "devices": devices}, lab_root)

    lldp_header = "Device ID       Local Intf                      Hold-time  Capability      Port ID"
    cdp_header = "Device ID        Local Intrfce     Holdtme    Capability  Platform    Port ID"

    def _lldp_text(remote, port):
        return f"{lldp_header}\n{remote:<32}{'Gi0/0/0/2':<32}{'120':<11}{'R':<16}{port}\nTotal entries displayed: 1\n"

    def _cdp_row(device_id, local_intf, port_id):
        return (
            f"{device_id:<17}{local_intf:<18}{'120':<11}{'R':<12}{'ASR9K Ser':<12}{port_id}"
        )

    def fake_collect(device_id, cfg):
        if device_id == "R1":
            return {
                "hostname": "ASR9001-R1",
                "show_version": "",
                "show_running_config": "",
                "show_lldp_neighbors": _lldp_text("ASR9001-R2", "Gi0/0/0/2"),
                "show_cdp_neighbors": f"{cdp_header}\n{_cdp_row('ASR9001-R2', 'Gi0/0/0/2', 'Gi0/0/0/2')}\n",
            }
        return {
            "hostname": "ASR9001-R2",
            "show_version": "",
            "show_running_config": "",
            "show_lldp_neighbors": _lldp_text("ASR9001-R1", "Gi0/0/0/2"),
            "show_cdp_neighbors": f"{cdp_header}\n{_cdp_row('ASR9001-R1', 'Gi0/0/0/2', 'Gi0/0/0/2')}\n",
        }

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_collect)

    from network_lab_mcp import terminal

    monkeypatch.setattr(terminal, "close_bootstrap_terminal", lambda device_id: None)

    result = discovery.discover_topology(lab_root)

    assert result.observation_count == 2  # one LLDP row per device
    assert result.cdp_observation_count == 2  # one CDP row per device
    assert result.conflicts == []
    assert len(result.managed_links) == 1
    link = result.managed_links[0]
    assert {link.a_device, link.b_device} == {"R1", "R2"}
