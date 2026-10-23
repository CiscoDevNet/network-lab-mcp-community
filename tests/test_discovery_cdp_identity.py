# SPDX-License-Identifier: Apache-2.0
"""CDP identity resolution reuses resolve_remote_identity() as-is
(it is protocol-agnostic pure string matching -- it never inspects
`obs.source`), including the FQDN -> short-name path a CDP Device ID
commonly needs (e.g. "asr9001-r1.example.com" -> "R1", whose observed
hostname is "ASR9001-R1"). No fuzzy/substring matching, and an ambiguous
short-name collision stays unresolved rather than guessed, exactly as for
LLDP. An unmanaged/unresolved CDP neighbor must never be treated as (or
turn into) a managed topology device."""

from __future__ import annotations

from network_lab_mcp.discovery import NeighborObservation, resolve_remote_identity


def test_cdp_fqdn_resolves_via_short_name_against_observed_hostname():
    identity_map = {"R1": "ASR9001-R1", "R2": "ASR9001-R2"}
    assert resolve_remote_identity("asr9001-r1.example.com", identity_map) == "R1"
    assert resolve_remote_identity("ASR9001-R2.example.com", identity_map) == "R2"


def test_cdp_short_hostname_ambiguous_collision_stays_unresolved():
    identity_map = {"R1": "SW1", "R2": "sw1"}
    assert resolve_remote_identity("sw1.example.com", identity_map) is None


def test_cdp_exact_hostname_match_unaffected_by_case():
    identity_map = {"PAGENT": "PAGENT"}
    assert resolve_remote_identity("PAGENT", identity_map) == "PAGENT"
    assert resolve_remote_identity("pagent", identity_map) == "PAGENT"


def test_unmanaged_cdp_neighbor_is_not_resolved_and_not_a_device():
    identity_map = {"R1": "ASR9001-R1", "PAGENT": "PAGENT"}
    remote_id = resolve_remote_identity("labsw-a07.example.com", identity_map)
    assert remote_id is None


def test_resolve_remote_identity_is_source_agnostic():
    """The exact same resolution rule applies whether the observation came
    from LLDP or CDP -- resolve_remote_identity() never looks at
    `obs.source` at all (it isn't even passed the observation)."""
    identity_map = {"PAGENT": "PAGENT"}
    lldp_obs = NeighborObservation("R1", "Gi0/0/0/10", "PAGENT", "Gig0/0", source="lldp")
    cdp_obs = NeighborObservation("R1", "Gi0/0/0/10", "PAGENT", "Gig0/0", source="cdp")
    assert resolve_remote_identity(lldp_obs.remote_device_id_raw, identity_map) == "PAGENT"
    assert resolve_remote_identity(cdp_obs.remote_device_id_raw, identity_map) == "PAGENT"
