# SPDX-License-Identifier: Apache-2.0
"""Link reconciliation (reconcile_links()) and unresolved-neighbor
evidence retention -- reciprocal LLDP observations collapse into one
physical link, parallel links between the same router pair stay distinct,
conflicting reciprocal observations are reported rather than silently
resolved, and an unresolved/external neighbor (e.g. the sanitized ASR9001
fixture) is retained as evidence without ever being added as a managed
topology device."""

from __future__ import annotations

from pathlib import Path

from network_lab_mcp.discovery import (
    LldpObservation,
    parse_lldp_neighbors,
    reconcile_links,
    resolve_remote_identity,
)

FIXTURES = Path(__file__).parent / "fixtures" / "lldp"

IDENTITY_MAP = {
    "R1": "LAB_DC_R1",
    "R2": "LAB_DC_R2",
    "R3": "LAB_DC_R3",
    "R4": "LAB_DC_R4",
}


def _resolve_and_split(observations):
    resolved = []
    unresolved = []
    for obs in observations:
        remote_id = resolve_remote_identity(obs.remote_device_id_raw, IDENTITY_MAP)
        if remote_id is not None:
            resolved.append((obs, remote_id))
        else:
            unresolved.append(obs)
    return resolved, unresolved


def _link_set(links):
    return {(link.a_device, link.a_interface, link.b_device, link.b_interface) for link in links}


def test_reciprocal_observations_collapse_into_one_link():
    r1_obs = [
        o for o in parse_lldp_neighbors((FIXTURES / "r1_show_lldp_neighbors.txt").read_text(), "R1")
        if o.remote_device_id_raw.startswith("LAB_DC_R2")
    ]
    r2_obs = [
        o for o in parse_lldp_neighbors((FIXTURES / "r2_show_lldp_neighbors.txt").read_text(), "R2")
        if o.remote_device_id_raw.startswith("LAB_DC_R1")
    ]
    resolved, _ = _resolve_and_split(r1_obs + r2_obs)
    links, conflicts = reconcile_links(resolved)

    assert conflicts == []
    assert len(links) == 2  # Gi.../2 and Gi.../3 -- two parallel links, not deduplicated together
    pairs = {(min(l.a_device, l.b_device), max(l.a_device, l.b_device)) for l in links}
    assert pairs == {("R1", "R2")}


def test_parallel_links_remain_distinct():
    resolved = [
        (LldpObservation("R1", "Gi0/0/0/2", "LAB_DC_R2", "Gi0/0/0/2"), "R2"),
        (LldpObservation("R2", "Gi0/0/0/2", "LAB_DC_R1", "Gi0/0/0/2"), "R1"),
        (LldpObservation("R1", "Gi0/0/0/3", "LAB_DC_R2", "Gi0/0/0/3"), "R2"),
        (LldpObservation("R2", "Gi0/0/0/3", "LAB_DC_R1", "Gi0/0/0/3"), "R1"),
    ]
    links, conflicts = reconcile_links(resolved)
    assert conflicts == []
    assert len(links) == 2
    interfaces = {l.a_interface for l in links} | {l.b_interface for l in links}
    assert interfaces == {"Gi0/0/0/2", "Gi0/0/0/3"}


def test_one_sided_observation_still_creates_a_managed_link():
    resolved = [(LldpObservation("R1", "Gi0/0/0/6", "LAB_DC_R3", "Gi0/0/0/6"), "R3")]
    links, conflicts = reconcile_links(resolved)
    assert conflicts == []
    assert len(links) == 1
    assert links[0] == (links[0].__class__("R1", "Gi0/0/0/6", "R3", "Gi0/0/0/6"))


def test_conflicting_reciprocal_observation_is_reported_not_silently_resolved():
    resolved = [
        (LldpObservation("R1", "Gi0/0/0/2", "LAB_DC_R2", "Gi0/0/0/2"), "R2"),
        # R2 claims a *different* local interface talks to R1 on Gi0/0/0/2
        # than R1 itself claims (R1 said Gi0/0/0/2, R2 says its own
        # Gi0/0/0/2 connects to R1's Gi0/0/0/3, not Gi0/0/0/2).
        (LldpObservation("R2", "Gi0/0/0/2", "LAB_DC_R1", "Gi0/0/0/3"), "R1"),
    ]
    links, conflicts = reconcile_links(resolved)
    assert links == []
    assert len(conflicts) == 1


def test_all_four_fixtures_produce_expected_links():
    all_resolved = []
    for name, device_id in (
        ("r1_show_lldp_neighbors.txt", "R1"),
        ("r2_show_lldp_neighbors.txt", "R2"),
        ("r3_show_lldp_neighbors.txt", "R3"),
        ("r4_show_lldp_neighbors.txt", "R4"),
    ):
        observations = parse_lldp_neighbors((FIXTURES / name).read_text(), device_id)
        resolved, _ = _resolve_and_split(observations)
        all_resolved.extend(resolved)

    links, conflicts = reconcile_links(all_resolved)
    assert conflicts == []
    pairs = {tuple(sorted((l.a_device, l.b_device))) for l in links}
    assert pairs == {("R1", "R2"), ("R1", "R3"), ("R2", "R4")}
    assert len(links) == 6  # two parallel links for each of the three router pairs


# ---- unresolved-neighbor evidence ----


def test_asr9001_remains_unresolved_and_is_not_a_managed_device():
    observations = parse_lldp_neighbors((FIXTURES / "r1_show_lldp_neighbors.txt").read_text(), "R1")
    resolved, unresolved = _resolve_and_split(observations)

    assert all(remote_id != "ASR9001_R1.example.com" for _, remote_id in resolved)
    unresolved_ids = {o.remote_device_id_raw for o in unresolved}
    assert "ASR9001_R1.example.com" in unresolved_ids


def test_unresolved_evidence_retains_full_observation_detail():
    observations = parse_lldp_neighbors((FIXTURES / "r1_show_lldp_neighbors.txt").read_text(), "R1")
    _, unresolved = _resolve_and_split(observations)
    asr = next(o for o in unresolved if o.remote_device_id_raw == "ASR9001_R1.example.com")

    assert asr.local_device_id == "R1"
    assert asr.local_interface == "GigabitEthernet0/0/0/10"
    assert asr.remote_port_id == "GigabitEthernet0/0/0/0"
    assert asr.capabilities == ("router",)


def test_unresolved_neighbor_never_appears_in_reconciled_links():
    observations = parse_lldp_neighbors((FIXTURES / "r1_show_lldp_neighbors.txt").read_text(), "R1")
    resolved, _ = _resolve_and_split(observations)
    links, _ = reconcile_links(resolved)
    all_devices = {l.a_device for l in links} | {l.b_device for l in links}
    assert "ASR9001_R1.example.com" not in all_devices
    assert all(device in IDENTITY_MAP for device in all_devices)
