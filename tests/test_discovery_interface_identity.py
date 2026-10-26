# SPDX-License-Identifier: Apache-2.0
"""Interface identity in link reconciliation: the same physical port seen as
"Gi0/0/0/10" (LLDP) and "Gig 0/0/0/10" (CDP summary) must be one endpoint, while
genuinely different ports stay distinct. Synthetic names/addresses only."""

from __future__ import annotations

import itertools

import pytest

from network_lab_mcp import discovery, lab as labmod, terminal
from network_lab_mcp.discovery import NeighborObservation, reconcile_links

LLDP_HEADER = "Device ID       Local Intf                      Hold-time  Capability      Port ID"
CDP_HEADER = "Device ID        Local Intrfce     Holdtme    Capability  Platform    Port ID"


def _obs(source, dev, intf, port):
    return NeighborObservation(dev, intf, "raw", port, source=source)


def _keys(links):
    return {(l.a_device, l.a_interface, l.b_device, l.b_interface) for l in links}


def test_lldp_and_cdp_reciprocal_with_different_spellings_is_one_link():
    resolved = [
        (_obs("cdp", "AGENT", "Gig 0/0", "Gig 0/0/0/10"), "SW1"),
        (_obs("lldp", "SW1", "Gi0/0/0/10", "Gi0/0"), "AGENT"),
    ]
    links, conflicts = reconcile_links(resolved)
    assert conflicts == []
    assert len(links) == 1


@pytest.mark.parametrize("order", list(itertools.permutations(range(2))))
def test_display_names_are_deterministic_and_use_each_devices_own_spelling(order):
    resolved = [
        (_obs("cdp", "AGENT", "Gig 0/0", "Gig 0/0/0/10"), "SW1"),
        (_obs("lldp", "SW1", "Gi0/0/0/10", "Gi0/0"), "AGENT"),
    ]
    links, _ = reconcile_links([resolved[i] for i in order])
    # ManagedLink orientation follows the first observation (the candidate builder
    # sorts endpoints); the endpoint pair and display spellings must not vary.
    assert {frozenset({(l.a_device, l.a_interface), (l.b_device, l.b_interface)}) for l in links} == {
        frozenset({("AGENT", "Gig 0/0"), ("SW1", "Gi0/0/0/10")})
    }


def test_same_device_two_spellings_of_one_port_is_one_link_regardless_of_order():
    a = (_obs("lldp", "SW1", "Gi0/0/0/10", "Gi0/0"), "AGENT")
    b = (_obs("cdp", "SW1", "Gig 0/0/0/10", "Gig 0/0"), "AGENT")
    first, _ = reconcile_links([a, b])
    second, _ = reconcile_links([b, a])
    assert len(first) == 1 and first == second


def test_different_ports_are_not_merged():
    resolved = [
        (_obs("lldp", "SW1", "Gi0/0/0/1", "Gi0/0"), "AGENT"),
        (_obs("lldp", "SW1", "Gi0/0/0/10", "Gi0/1"), "AGENT"),
        (_obs("lldp", "SW1", "Te0/0/0/10", "Gi0/2"), "AGENT"),
    ]
    links, conflicts = reconcile_links(resolved)
    assert conflicts == [] and len(links) == 3


def test_same_port_with_different_neighbor_ports_is_still_a_conflict():
    resolved = [
        (_obs("lldp", "SW1", "Gi0/0/0/10", "Gi0/0"), "AGENT"),
        (_obs("cdp", "SW1", "Gig 0/0/0/10", "Gig 0/1"), "AGENT"),
    ]
    links, conflicts = reconcile_links(resolved)
    assert links == [] and len(conflicts) == 1


def test_unknown_prefix_is_not_guessed():
    resolved = [
        (_obs("lldp", "SW1", "weird0/0", "Gi0/0"), "AGENT"),
        (_obs("cdp", "SW1", "weird 0/0", "Gi0/0"), "AGENT"),
    ]
    links, conflicts = reconcile_links(resolved)
    assert conflicts == [] and len(links) == 2  # kept apart, never merged
    with pytest.raises(discovery.DiscoveryError, match="more than one link"):
        discovery._fail_on_link_ambiguity(links, conflicts, [])


def test_gi_and_subinterface_and_gi_vs_gigabit_long_form():
    resolved = [
        (_obs("lldp", "SW1", "Gi0/0/0/10", "Gi0/0"), "AGENT"),
        (_obs("cdp", "AGENT", "GigabitEthernet0/0", "GigabitEthernet0/0/0/10"), "SW1"),
        (_obs("lldp", "SW1", "Gi0/0/0/10.100", "Gi0/5"), "AGENT"),
    ]
    links, conflicts = reconcile_links(resolved)
    assert conflicts == [] and len(links) == 2


@pytest.fixture()
def pipeline(lab_root, monkeypatch):
    monkeypatch.setattr(labmod, "find_lab_root", lambda: lab_root)
    labmod.write_access_info(
        "sample_lab",
        {
            "name": "sample_lab",
            "devices": {
                "AGENT": {"type": "ios", "address": "192.0.2.30", "transport": "telnet"},
                "SW1": {"type": "iosxr", "address": "192.0.2.11", "transport": "ssh"},
            },
        },
        lab_root,
    )
    monkeypatch.setattr(terminal, "close_bootstrap_terminal", lambda device_id: None)

    def fake_ios(device_id, cfg, include_l3=False):
        row = f"{'SW1':<17}{'Gig 0/0':<18}{'120':<11}{'R':<12}{'ASR9K Ser':<12}Gig 0/0/0/10"
        return {"hostname": "AGENT", "show_version": "", "show_cdp_neighbors": f"{CDP_HEADER}\n{row}\n"}

    def fake_xr(device_id, cfg, include_l3=False):
        lldp = (
            f"{LLDP_HEADER}\n{'AGENT':<32}{'Gi0/0/0/10':<32}{'120':<11}{'R':<16}Gi0/0\n"
            "Total entries displayed: 1\n"
        )
        return {
            "hostname": "SW1",
            "show_version": "",
            "show_running_config": "",
            "show_lldp_neighbors": lldp,
            "show_cdp_neighbors": f"{CDP_HEADER}\n",
        }

    monkeypatch.setattr(discovery, "_bootstrap_collect_ios", fake_ios)
    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_xr)
    return lab_root


def test_pipeline_ios_cdp_plus_xr_lldp_yields_one_link_and_stable_rediscovery(pipeline):
    first = discovery.discover_topology(pipeline)
    second = discovery.discover_topology(pipeline)
    assert len(first.managed_links) == 1 and first.conflicts == []
    base = {"name": "sample_lab", "description": "", "devices": {}, "links": []}
    cand1 = discovery.build_discovery_candidate(first, base)
    cand2 = discovery.build_discovery_candidate(second, cand1)
    assert cand1 == cand2
    assert cand1["links"] == [
        {"a": "AGENT", "a_interface": "Gig 0/0", "b": "SW1", "b_interface": "Gi0/0/0/10"}
    ]


def _candidate_links(resolved):
    links, conflicts = reconcile_links(resolved)
    assert conflicts == []
    result = discovery.DiscoveryResult("a", "a", 0, 0, 0, managed_links=links)
    return discovery.build_discovery_candidate(result, {"name": "a"})["links"]


FULL = [
    (_obs("lldp", "SW1", "Gi0/0/0/10", "Gi0/0"), "AGENT"),
    (_obs("cdp", "SW1", "Gig 0/0/0/10", "Gig 0/0"), "AGENT"),
    (_obs("cdp", "AGENT", "Gig 0/0", "Gig 0/0/0/10"), "SW1"),
]


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
def test_candidate_is_identical_for_every_observation_order(order):
    expected = [{"a": "AGENT", "a_interface": "Gig 0/0", "b": "SW1", "b_interface": "Gi0/0/0/10"}]
    assert _candidate_links([FULL[i] for i in order]) == expected


def test_one_sided_observation_uses_the_only_spellings_seen():
    links = _candidate_links([FULL[2]])
    assert links == [{"a": "AGENT", "a_interface": "Gig 0/0", "b": "SW1", "b_interface": "Gig 0/0/0/10"}]


def test_own_spelling_beats_neighbors_spelling_when_protocol_set_changes():
    # Identity (one link) is stable across protocol sets; only the display spelling may follow
    # what the device itself reported.
    for subset in ([FULL[0]], [FULL[1]], [FULL[0], FULL[2]], [FULL[1], FULL[2]], FULL):
        assert len(_candidate_links(subset)) == 1
    only_sw1_lldp = _candidate_links([FULL[0]])[0]
    assert (only_sw1_lldp["a_interface"], only_sw1_lldp["b_interface"]) == ("Gi0/0", "Gi0/0/0/10")
    both_sides = _candidate_links([FULL[0], FULL[2]])[0]
    assert (both_sides["a_interface"], both_sides["b_interface"]) == ("Gig 0/0", "Gi0/0/0/10")


def test_two_peers_claiming_one_port_under_different_spellings_is_fatal():
    resolved = [
        (_obs("lldp", "A", "Gi0/1", "Gi0/0/0/10"), "SW1"),
        (_obs("cdp", "B", "Gi0/1", "Gig 0/0/0/10"), "SW1"),
    ]
    links, conflicts = reconcile_links(resolved)
    with pytest.raises(discovery.DiscoveryError, match="SW1 .*more than one link"):
        discovery._fail_on_link_ambiguity(links, conflicts, [])


def test_unmanaged_neighbor_on_a_managed_port_under_another_spelling_is_fatal():
    links = [discovery.ManagedLink("SW1", "Gi0/0/0/10", "A", "Gi0/1")]
    unresolved = [discovery.UnresolvedNeighbor("host", "SW1", "Gig 0/0/0/10", "p", ())]
    with pytest.raises(discovery.DiscoveryError, match="both a managed and an unmanaged"):
        discovery._fail_on_link_ambiguity(links, [], unresolved)


def test_reciprocal_mismatch_across_spellings_is_a_conflict():
    resolved = [
        (_obs("lldp", "A", "Gi0/1", "Gi0/0/0/10"), "SW1"),
        (_obs("cdp", "SW1", "Gig 0/0/0/10", "Gi0/2"), "A"),
    ]
    links, conflicts = reconcile_links(resolved)
    assert links == [] and len(conflicts) == 1
