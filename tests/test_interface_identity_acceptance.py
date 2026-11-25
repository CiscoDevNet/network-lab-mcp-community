# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Interface identity: the 9-links-instead-of-5 problem and everything around it
(Test groups D, E, F, G, H, I, J).

Fixture provenance: the CDP rows are anonymized from a real mixed IOS XR / IOS XE
observation (device names replaced, columns re-aligned to the parser's fixed
widths); R1's CDP table is a SYNTHETIC completion (only a summary of it was
available); R2/R4 and the whole topology/access-info are fully synthetic.
Expected values below are literals, never computed by the code under test."""

from __future__ import annotations

import itertools

import pytest

from network_lab_mcp import discovery, terminal
from network_lab_mcp import interface_identity as ii
from network_lab_mcp import lab as labmod
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain
from network_lab_mcp.discovery import NeighborObservation, reconcile_links_detailed

# ---- fixture text -------------------------------------------------------------------------------

CDP_HEADER = f"{'Device ID':<25}{'Local Intrfce':<18}{'Holdtme':<9}{'Capability':<12}{'Platform':<12}Port ID"


def _row(dev, local, hold, cap, plat, port):
    return f"{dev:<25}{local:<18}{hold:<9}{cap:<12}{plat:<12}{port}"


def _table(rows, footer=""):
    return "\n".join([CDP_HEADER, *rows]) + "\n" + footer


R3_CDP = _table([  # anonymized real
    _row("lab-switch-a", "Te0/0/0/4", "179", "R S I", "C9300X-24", "TF1/1/5"),
    _row("lab-switch-a", "Te0/0/0/3", "147", "R S I", "C9300X-24", "TF1/1/3"),
    _row("router1", "Te0/0/0/0", "145", "R", "NCS-540", "Te0/0/0/1"),
])
R1_CDP = _table([  # SYNTHETIC completion
    _row("lab-switch-a", "Te0/0/0/3", "150", "R S I", "C9300X-24", "TF1/1/4"),
    _row("lab-switch-a", "Te0/0/0/5", "151", "R S I", "C9300X-24", "TF1/1/2"),
    _row("router3", "Te0/0/0/1", "152", "R", "NCS-5500", "Te0/0/0/0"),
])
SW_CDP = _table([  # anonymized real
    _row("unmanaged-sw", "Gig 0/0", "131", "S I", "C9200L-48", "Gig 1/0/19"),
    _row("router3", "Twe 1/1/3", "161", "R", "NCS-5500", "TenGigE0/0/0/3"),
    _row("router3", "Twe 1/1/5", "157", "R", "NCS-5500", "TenGigE0/0/0/4"),
    _row("router1", "Twe 1/1/2", "169", "R", "NCS-540", "TenGigE0/0/0/5"),
    _row("router1", "Twe 1/1/4", "166", "R", "NCS-540", "TenGigE0/0/0/3"),
], "\nTotal cdp entries displayed : 5\n")
OFF_XR = "% CDP is not enabled\n"

# Independent fixed expectation: the five physical links as (device, type id, identifier) pairs.
EXPECTED_FIVE = {
    frozenset({("R1", "10ge", "0/0/0/1"), ("R3", "10ge", "0/0/0/0")}),
    frozenset({("L3SW1", "25ge", "1/1/2"), ("R1", "10ge", "0/0/0/5")}),
    frozenset({("L3SW1", "25ge", "1/1/3"), ("R3", "10ge", "0/0/0/3")}),
    frozenset({("L3SW1", "25ge", "1/1/4"), ("R1", "10ge", "0/0/0/3")}),
    frozenset({("L3SW1", "25ge", "1/1/5"), ("R3", "10ge", "0/0/0/4")}),
}
TYPES = {"R1": "iosxr", "R2": "iosxr", "R3": "iosxr", "R4": "iosxr", "L3SW1": "iosxe"}


def link_identities(links):
    """(device, type id, identifier) pairs of ManagedLinks / topology link dicts (via the real normalizer)."""
    out = set()
    for link in links:
        if isinstance(link, dict):
            ends = ((link["a"], link["a_interface"]), (link["b"], link["b_interface"]))
        else:
            ends = ((link.a_device, link.a_interface), (link.b_device, link.b_interface))
        pair = set()
        for device, raw in ends:
            res = ii.normalize(raw, ii.ObservationContext(owner_os=TYPES[device]))
            assert res.recognized, (device, raw)
            pair.add((device, res.identity.type_id, res.identity.identifier))
        out.add(frozenset(pair))
    return out


@pytest.fixture()
def pipeline(lab_root, monkeypatch):
    monkeypatch.setattr(labmod, "find_lab_root", lambda: lab_root)
    labmod.write_access_info(
        "sample_lab",
        {
            "name": "sample_lab",
            "devices": {
                **{d: {"type": "iosxr", "address": f"192.0.2.{i}", "transport": "ssh"} for i, d in enumerate(["R1", "R2", "R3", "R4"], 11)},
                "L3SW1": {"type": "iosxe", "address": "192.0.2.21", "transport": "ssh"},
            },
        },
        lab_root,
    )
    monkeypatch.setattr(terminal, "close_bootstrap_terminal", lambda device_id: None)
    state = {"order": [], "xe_cdp": SW_CDP}
    cdp_by_device = {"R1": R1_CDP, "R3": R3_CDP, "R2": OFF_XR, "R4": OFF_XR}

    def fake_xr(device_id, cfg, include_l3=False):
        state["order"].append(device_id)
        return {"hostname": device_id.lower().replace("r", "router"), "show_version": "",
                "show_lldp_neighbors": "% LLDP is not enabled\n", "show_cdp_neighbors": cdp_by_device[device_id]}

    def fake_xe(device_id, cfg, include_l3=False):
        state["order"].append(device_id)
        return {"hostname": "lab-switch-a", "show_version": "",
                "show_lldp_neighbors": "% LLDP is not enabled\n", "show_cdp_neighbors": state["xe_cdp"]}

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake_xr)
    monkeypatch.setattr(discovery, "_bootstrap_collect_iosxe", fake_xe)
    return lab_root, state


def _candidate(result, base=None):
    return discovery.build_discovery_candidate(result, base or {"name": "sample_lab", "description": "", "devices": {}, "links": []})


# ---- E: the 5-link acceptance ---------------------------------------------------------------------

def test_e01_xr_side_observations_alone_give_the_five_links(pipeline):
    root, state = pipeline
    state["xe_cdp"] = OFF_XR  # IOS XE side contributes nothing
    result = discovery.discover_topology(root)
    assert len(result.managed_links) == 5
    assert link_identities(result.managed_links) == EXPECTED_FIVE


def test_e02_e03_e12_xr_plus_xe_observations_still_five_links_no_duplicates(pipeline):
    root, _ = pipeline
    result = discovery.discover_topology(root)
    assert len(result.managed_links) == 5  # the old implementation produced 9 here
    assert link_identities(result.managed_links) == EXPECTED_FIVE
    assert result.conflicts == [] and result.interface_diagnostics == [] and result.deferred_observation_count == 0


def test_e04_e05_e06_parallel_links_are_kept_apart(pipeline):
    result = discovery.discover_topology(pipeline[0])
    links = link_identities(result.managed_links)
    pair = lambda a, b: {l for l in links if {d for d, _t, _i in l} == {a, b}}
    assert len(pair("R1", "R3")) == 1 and len(pair("L3SW1", "R1")) == 2 and len(pair("L3SW1", "R3")) == 2


def test_e07_e08_e09_partial_success_and_unresolved_neighbor_are_kept(pipeline):
    result = discovery.discover_topology(pipeline[0])
    status = {o.device_id: (o.connection, o.status) for o in result.outcomes}
    assert status["R2"] == ("connected", "Partial") and status["R4"] == ("connected", "Partial")
    assert status["L3SW1"] == ("connected", "Complete")
    assert sorted(result.devices) == ["L3SW1", "R1", "R2", "R3", "R4"]
    assert [(u.remote_device_id_raw, u.local_device_id) for u in result.unresolved] == [("unmanaged-sw", "L3SW1")]


def test_e10_e11_e19_e20_merge_counts_and_rediscovery_are_stable(pipeline):
    root, _ = pipeline
    result = discovery.discover_topology(root)
    existing = {"name": "sample_lab", "description": "", "devices": {d: {"type": t} for d, t in TYPES.items()},
                "links": [{"a": "R1", "a_interface": "Te0/0/0/1", "b": "R3", "b_interface": "Te0/0/0/0"}]}
    cand = _candidate(result, existing)
    changes = discovery.topology_changes(existing, cand)
    assert (changes.links_added, changes.links_retained) == (4, 1) and len(result.managed_links) == 5
    assert cand["links"][0] == existing["links"][0]  # untouched, same position
    assert link_identities(cand["links"]) == EXPECTED_FIVE
    again = discovery.build_discovery_candidate(discovery.discover_topology(root), cand)
    rechanges = discovery.topology_changes(cand, again)
    assert again == cand and (rechanges.links_added, rechanges.links_retained, rechanges.changed) == (0, 5, False)
    assert "Discovered links         : 5" in climain.render_discovery_summary(result, changes)


def test_e13_a_wrong_but_five_link_set_is_detected_by_the_identity_comparison():
    wrong = [{"a": "R1", "a_interface": "Te0/0/0/1", "b": "R3", "b_interface": "Te0/0/0/0"},
             {"a": "L3SW1", "a_interface": "TF1/1/2", "b": "R1", "b_interface": "Te0/0/0/5"},
             {"a": "L3SW1", "a_interface": "TF1/1/3", "b": "R3", "b_interface": "Te0/0/0/3"},
             {"a": "L3SW1", "a_interface": "TF1/1/4", "b": "R1", "b_interface": "Te0/0/0/3"},
             {"a": "L3SW1", "a_interface": "TF1/1/5", "b": "R3", "b_interface": "Te0/0/0/9"}]  # wrong port
    assert len(wrong) == 5 and link_identities(wrong) != EXPECTED_FIVE


def test_e14_e15_e16_display_names_new_ones_have_no_space_and_reload(pipeline, lab_root):
    root, _ = pipeline
    cand = _candidate(discovery.discover_topology(root))
    for link in cand["links"]:
        assert " " not in link["a_interface"] and " " not in link["b_interface"]
    labmod.write_topology("sample_lab", cand, root)
    reloaded = labmod.load_topology("sample_lab", root)
    assert link_identities(reloaded["links"]) == EXPECTED_FIVE  # re-resolved from stored device types only


def test_e17_e18_observation_and_worker_order_do_not_change_the_candidate(pipeline, monkeypatch):
    root, state = pipeline
    baseline = _candidate(discovery.discover_topology(root))
    monkeypatch.setattr(discovery, "DISCOVERY_MAX_WORKERS", 1)
    assert _candidate(discovery.discover_topology(root)) == baseline
    # reverse every neighbor table's row order
    for name in ("R1_CDP", "R3_CDP", "SW_CDP"):
        lines = globals()[name].splitlines()
        rows = [l for l in lines[1:] if l.strip() and "Total" not in l]
        monkeypatch.setitem(globals(), name, "\n".join([lines[0], *reversed(rows)]) + "\n")
    state["xe_cdp"] = SW_CDP
    monkeypatch.setattr(discovery.terminal, "close_bootstrap_terminal", lambda d: None)
    assert _candidate(discovery.discover_topology(root)) == baseline


@pytest.mark.parametrize("order", list(itertools.permutations(range(4))))
def test_e17_d11_d32_reconcile_is_independent_of_order_and_duplicates(order):
    obs = [
        (NeighborObservation("L3SW1", "Twe 1/1/3", "router3", "TenGigE0/0/0/3", source="cdp"), "R3"),
        (NeighborObservation("R3", "Te0/0/0/3", "lab-switch-a", "TF1/1/3", source="cdp"), "L3SW1"),
        (NeighborObservation("R3", "Te0/0/0/3", "lab-switch-a", "TF1/1/3", source="lldp"), "L3SW1"),
        (NeighborObservation("L3SW1", "Twe1/1/3", "router3", "Te0/0/0/3", source="lldp"), "R3"),
    ]
    links, conflicts, diags, deferred = reconcile_links_detailed([obs[i] for i in order], TYPES)
    assert conflicts == [] and diags == [] and deferred == 0
    assert [(l.a_device, l.a_interface, l.b_device, l.b_interface) for l in links] == [("L3SW1", "Twe1/1/3", "R3", "Te0/0/0/3")]
    doubled, _, _, _ = reconcile_links_detailed([obs[i] for i in order] * 2, TYPES)
    assert doubled == links


# ---- D: reconciliation edge cases ------------------------------------------------------------------

def _o(src, dev, local, remote_id, port):
    return NeighborObservation(dev, local, "raw", port, source=src), remote_id


def test_d14_alias_normalization_reveals_a_conflict():
    obs = [_o("cdp", "R1", "Te0/0/0/3", "R2", "Te0/0/0/1"), _o("cdp", "R1", "TenGigE0/0/0/3", "R3", "Te0/0/0/2")]
    links, conflicts, _d, _n = reconcile_links_detailed(obs, TYPES)
    assert len(conflicts) == 1 and links == []


def test_d17_d18_d19_d20_deferral_by_status_and_other_links_continue():
    good = _o("cdp", "R1", "Te0/0/0/5", "R2", "Te0/0/0/5")
    for raw, status in (("XX0/0/0/3", "UNKNOWN_TYPE"), ("Te0/0/0/3x", "INVALID_FORMAT"), ("TF1/1/3", "AMBIGUOUS")):
        # AMBIGUOUS case: owner R3 has no type here
        links, conflicts, diags, deferred = reconcile_links_detailed([_o("cdp", "R1", "Te0/0/0/3", "R3", raw), good], {"R1": "iosxr", "R2": "iosxr"})
        assert [d.status for d in diags] == [status] and deferred == 1 and len(links) == 1 and conflicts == []
        assert diags[0].field == "remote" and diags[0].owner_device == "R3" and diags[0].observing_device == "R1"
        assert diags[0].action == "Observation deferred"


def test_d21_d22_d23_independent_observation_registers_and_unknown_is_not_merged():
    obs = [_o("cdp", "SW", "Gig 0/1", "R1", "XX0/0/1"),  # unknown remote
           _o("cdp", "R1", "Te0/0/0/1", "SW", "Gi0/1")]  # independent, fully known (different spelling)
    links, conflicts, diags, deferred = reconcile_links_detailed(obs, {"SW": "ios", "R1": "iosxr"})
    assert len(links) == 1 and deferred == 1 and diags[0].raw_name == "XX0/0/1"  # warning is kept


def test_d30_conflict_provable_without_the_unknown_type():
    obs = [_o("lldp", "R1", "Gi0/0/0/10", "SW1", "port-x"), _o("cdp", "R1", "Gi0/0/0/10", "LegacyRouter", "Gig0/0")]
    links, conflicts, diags, deferred = reconcile_links_detailed(obs)
    assert len(conflicts) == 1 and links == [] and deferred == 1


# ---- G / H: merge with unknown interfaces, baseline-aware validation ---------------------------------

def _result(links, devices=None, **kw):
    managed = [discovery.ManagedLink(*l) for l in links]
    return discovery.DiscoveryResult("a", "a", 0, 0, 0, devices=devices or {d: {"type": t} for d, t in TYPES.items()},
                                     managed_links=managed, **kw)


def _topo(*links, **extra):
    return {"name": "t", "devices": {d: {"type": t} for d, t in TYPES.items()},
            "links": [{"a": a, "a_interface": ai, "b": b, "b_interface": bi} for a, ai, b, bi in links], **extra}


def test_g01_g03_g04_alias_spelling_of_an_existing_link_is_retained_not_added():
    base = _topo(("L3SW1", "TF1/1/3", "R3", "Te0/0/0/3"))
    cand = discovery.build_discovery_candidate(_result([("L3SW1", "Twe1/1/3", "R3", "TenGigE0/0/0/3")]), base)
    assert cand["links"] == base["links"]


def test_g07_g08_g09_existing_information_is_preserved():
    base = _topo(("R1", "Te0/0/0/1", "R3", "Te0/0/0/0"), future_field={"x": 1})
    base["links"][0]["note"] = "keep"
    base["devices"]["R9"] = {"type": "iosxr", "note": "stale"}
    cand = discovery.build_discovery_candidate(_result([]), base)
    assert cand["links"] == base["links"] and cand["devices"]["R9"] == base["devices"]["R9"] and cand["future_field"] == {"x": 1}


def test_g10_g11_g12_existing_unknown_interface_is_kept_and_never_a_registration_key():
    base = _topo(("R1", "XX0/0/0/9", "R2", "Te0/0/0/9"))
    notes = []
    cand = discovery.build_discovery_candidate(_result([("R1", "Te0/0/0/1", "R3", "Te0/0/0/0")]), base, notes)
    assert cand["links"][0] == base["links"][0] and len(cand["links"]) == 2 and notes == []  # d31: different number


def test_d27_d28_d29_same_number_as_unknown_stored_interface_is_unresolved_not_a_conflict():
    base = _topo(("R1", "XX0/0/0/3", "R2", "Te0/0/0/1"))
    notes = []
    cand = discovery.build_discovery_candidate(_result([("R1", "TenGigE0/0/0/3", "R3", "Te0/0/0/2")]), base, notes)
    assert cand["links"] == base["links"]  # new link deferred, nothing removed, no exception (not a confirmed conflict)
    assert [n.action.split(" (")[0] for n in notes] == ["Unresolved relationship with existing topology"]


def test_t1_confirmed_conflict_on_known_endpoint_fails_and_leaves_base_untouched():
    base = _topo(("R1", "Te0/0/0/3", "R2", "Te0/0/0/1"))
    snapshot = repr(base)
    with pytest.raises(discovery.DiscoveryError, match="conflicts with existing link"):
        discovery.build_discovery_candidate(_result([("R1", "TenGigE0/0/0/3", "R3", "Te0/0/0/2")]), base)
    assert repr(base) == snapshot


def test_t1b_conflict_provable_from_the_other_endpoint_despite_unknown_type():
    base = _topo(("R1", "XX0/0/0/3", "R2", "Te0/0/0/1"))
    with pytest.raises(discovery.DiscoveryError):
        discovery.build_discovery_candidate(_result([("R3", "Te0/0/0/7", "R2", "TenGigE0/0/0/1")]), base)


def test_g13_g14_g15_existing_alias_duplicate_is_kept_reported_and_not_worsened():
    base = _topo(("L3SW1", "TF1/1/3", "R3", "Te0/0/0/3"), ("L3SW1", "Twe 1/1/3", "R3", "TenGigE0/0/0/3"))
    notes = []
    cand = discovery.build_discovery_candidate(_result([("R1", "Te0/0/0/1", "R3", "Te0/0/0/0")]), base, notes)
    assert cand["links"][:2] == base["links"] and len(cand["links"]) == 3
    assert any(n.action.startswith("Existing duplicate link detected") for n in notes)


def test_g16_existing_endpoint_conflict_blocks_automatic_merge():
    base = _topo(("R1", "Te0/0/0/3", "R2", "Te0/0/0/1"), ("R1", "TenGigE0/0/0/3", "R3", "Te0/0/0/2"))
    with pytest.raises(discovery.DiscoveryError, match="several different neighbors"):
        discovery.build_discovery_candidate(_result([]), base)


# Baseline-aware cases A-F of the specification ----------------------------------------------------

def _dups(n, key=("R1", "Te0/0/0/3", "R2", "Te0/0/0/1")):
    spellings = ["Te0/0/0/3", "TenGigE0/0/0/3", "TenGigabitEthernet0/0/0/3"]
    return [(key[0], spellings[i], key[2], key[3]) for i in range(n)]


def _check(before, after):
    discovery.check_link_integrity_change(_topo(*before) if before is not None else None, _topo(*after))


def test_h_case_a_same_duplicate_count_is_accepted():
    _check(_dups(2), _dups(2))


def test_h_case_b_multiplicity_increase_is_rejected():
    with pytest.raises(labmod.LabConfigError):
        _check(_dups(2), _dups(3))


def test_h_case_c_multiplicity_decrease_is_accepted():
    _check(_dups(3), _dups(2))


def test_h_case_d_different_duplicate_replacing_the_old_is_rejected():
    other = _dups(2, ("R1", "Te0/0/0/5", "R2", "Te0/0/0/2"))
    with pytest.raises(labmod.LabConfigError):
        _check(_dups(2), other)


def test_h_case_e_new_neighbor_for_a_conflicted_endpoint_is_rejected_same_count():
    before = [("R1", "Te0/0/0/3", "R2", "Te0/0/0/1"), ("R1", "Te0/0/0/3", "R3", "Te0/0/0/2")]
    after = [("R1", "Te0/0/0/3", "R2", "Te0/0/0/1"), ("R1", "Te0/0/0/3", "R4", "Te0/0/0/4")]
    with pytest.raises(labmod.LabConfigError, match="several different neighbors"):
        _check(before, after)


def test_h_case_f_resolving_a_conflict_is_accepted():
    _check([("R1", "Te0/0/0/3", "R2", "Te0/0/0/1"), ("R1", "Te0/0/0/3", "R3", "Te0/0/0/2")],
           [("R1", "Te0/0/0/3", "R2", "Te0/0/0/1")])


def test_h_new_topology_has_an_empty_baseline_and_direction_or_spelling_alone_is_not_a_change():
    with pytest.raises(labmod.LabConfigError):
        _check(None, _dups(2))
    _check(_dups(2), [("R2", "Te0/0/0/1", "R1", "Te0/0/0/3"), ("R1", "TenGigE0/0/0/3", "R2", "Te0/0/0/1")])  # h29


def test_h02_h03_h04_h05_load_stays_lenient_for_legacy_alias_duplicates_and_strict_for_raw_ones(lab_root):
    legacy = _topo(("L3SW1", "TF1/1/3", "R3", "Te0/0/0/3"), ("L3SW1", "Twe 1/1/3", "R3", "TenGigE0/0/0/3"))
    legacy["name"] = "legacy"
    labmod.validate_topology_data("legacy", legacy)  # loads fine (diagnosed elsewhere)
    raw_dup = _topo(("R1", "Te0/0/0/3", "R2", "Te0/0/0/1"), ("R1", "Te0/0/0/3", "R2", "Te0/0/0/1"))
    with pytest.raises(labmod.LabConfigError, match="duplicates an earlier link"):
        labmod.validate_topology_data("t", raw_dup)


def _enter(lab_root, name):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition(name))
    return session


def _topo_named(name, *links):
    data = _topo(*links)
    data["name"] = name
    return data


def test_h_candidate_replacement_and_commit_use_the_committed_file_as_baseline(lab_root):
    labmod.write_topology("legacy", _topo_named("legacy", *_dups(2)), lab_root)  # legacy duplicate is committed
    session = _enter(lab_root, "legacy")
    with pytest.raises(labmod.LabConfigError):
        session.replace_definition_candidate(_topo_named("legacy", *_dups(3)))
    session.replace_definition_candidate(_topo_named("legacy", *_dups(2)))
    # a worse candidate smuggled in without going through replace_* is still refused at commit,
    # and being the current candidate does not make it a baseline
    session.definition_candidate = _topo_named("legacy", *_dups(3))
    session._mutation_rev += 1
    with pytest.raises(cfgmod.CommitValidationError, match="inconsistency"):
        session.commit()
    assert len(labmod.load_topology("legacy", lab_root)["links"]) == 2  # nothing written


def test_h27_new_topology_baseline_is_empty(lab_root):
    session = _enter(lab_root, "fresh")
    with pytest.raises(labmod.LabConfigError):
        session.replace_definition_candidate(_topo_named("fresh", *_dups(2)))


def test_h_discovery_baseline_is_the_target_topology_not_the_active_one(pipeline, lab_root):
    root, _ = pipeline
    result = discovery.discover_topology(root)
    # the active topology (settings) is a different, committed one carrying a legacy duplicate
    labmod.write_topology("other", _topo_named("other", *_dups(2)), root)
    session = cfgmod.CliSession(root)
    session.enter_configure()
    changes = session.apply_discovery_result(result)  # target = access-info name "sample_lab", new => empty baseline
    # (the stock sample_lab topology is the committed base here: its interface-less link stays)
    interface_links = [l for l in session.definition_candidate["links"] if l.get("a_interface")]
    assert changes.links_added == 5 and link_identities(interface_links) == EXPECTED_FIVE


def test_h28_a_failed_discovery_merge_leaves_the_candidate_and_dirty_state_untouched(pipeline):
    root, _ = pipeline
    result = discovery.discover_topology(root)
    labmod.write_topology("sample_lab", {**_topo(("R1", "Te0/0/0/3", "R2", "Te0/0/0/1"), ("R1", "TenGigE0/0/0/3", "R3", "Te0/0/0/2")),
                                         "name": "sample_lab"}, root)
    session = cfgmod.CliSession(root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    before = (repr(session.definition_candidate), session.definition_dirty(), session._mutation_rev)
    with pytest.raises(discovery.DiscoveryError):
        session.apply_discovery_result(result)
    assert (repr(session.definition_candidate), session.definition_dirty(), session._mutation_rev) == before


# ---- I: L3 merge -------------------------------------------------------------------------------------

def _l3(device, **ifaces):
    return {device: {"type": TYPES[device], "interfaces": ifaces}}


def test_i01_i02_i03_i04_alias_spelling_matches_stored_name_values_preserved():
    base = _topo()
    base["devices"]["R1"]["interfaces"] = {"TenGigE0/0/0/3": {"ipv4_address": "192.0.2.1", "vrf": "default"}}
    res = _result([], devices=_l3("R1", **{"Te0/0/0/3": {"ipv4_address": "192.0.2.1", "vrf": "default"}}))
    cand = discovery.build_discovery_candidate(res, base)
    assert list(cand["devices"]["R1"]["interfaces"]) == ["TenGigE0/0/0/3"]
    res2 = _result([], devices=_l3("R1", TenGigE0_0_0_3=None))  # unassigned never blanks
    assert discovery.build_discovery_candidate(res2, cand)["devices"]["R1"]["interfaces"] == cand["devices"]["R1"]["interfaces"]


def test_i06_i07_i08_legit_update_and_subinterface_distinction():
    base = _topo()
    base["devices"]["R1"]["interfaces"] = {"TenGigE0/0/0/3": {"ipv4_address": "192.0.2.1", "vrf": "default"}}
    res = _result([], devices=_l3("R1", **{"Te0/0/0/3": {"ipv4_address": "192.0.2.9", "vrf": "red"},
                                           "TenGigE0/0/0/3.100": {"ipv4_address": "198.51.100.1", "vrf": "default"}}))
    ifs = discovery.build_discovery_candidate(res, base)["devices"]["R1"]["interfaces"]
    assert ifs["TenGigE0/0/0/3"] == {"ipv4_address": "192.0.2.9", "vrf": "red"} and "TenGigE0/0/0/3.100" in ifs and len(ifs) == 2


def test_i11_i14_i15_i16_unknown_new_l3_interface_deferred_existing_kept():
    base = _topo()
    base["devices"]["R1"]["interfaces"] = {"XX1": {"ipv4_address": "192.0.2.1", "vrf": "default"},
                                           "Loopback0": {"ipv4_address": "192.0.2.2", "vrf": "default"}}
    notes = []
    res = _result([], devices=_l3("R1", XX2={"ipv4_address": "192.0.2.5", "vrf": "default"},
                                  XX1={"ipv4_address": "192.0.2.6", "vrf": "default"}))
    ifs = discovery.build_discovery_candidate(res, base, notes)["devices"]["R1"]["interfaces"]
    assert "XX2" not in ifs and ifs["XX1"]["ipv4_address"] == "192.0.2.6" and "Loopback0" in ifs  # exact name only
    assert [(n.raw_name, n.status, n.field, n.action) for n in notes] == [("XX2", "UNKNOWN_TYPE", "l3", "New L3 registration deferred")]


def test_i19_i25_i26_conflicting_or_ambiguous_l3_data_is_held_not_chosen_by_order():
    base = _topo()
    base["devices"]["R1"]["interfaces"] = {"Te0/0/0/3": {"ipv4_address": "192.0.2.1", "vrf": "default"},
                                           "TenGigE0/0/0/3": {"ipv4_address": "192.0.2.2", "vrf": "default"}}
    notes = []
    res = _result([], devices=_l3("R1", **{"TenGigE0/0/0/3": {"ipv4_address": "192.0.2.9", "vrf": "default"}}))
    ifs = discovery.build_discovery_candidate(res, base, notes)["devices"]["R1"]["interfaces"]
    assert ifs == base["devices"]["R1"]["interfaces"] and "held" in notes[0].action
    notes2 = []
    res2 = _result([], devices=_l3("R1", **{"Te0/0/0/5": {"ipv4_address": "192.0.2.3", "vrf": "default"},
                                            "TenGigE0/0/0/5": {"ipv4_address": "192.0.2.4", "vrf": "default"}}))
    assert "interfaces" in discovery.build_discovery_candidate(res2, _topo())["devices"]["R1"]
    assert "Te0/0/0/5" not in discovery.build_discovery_candidate(res2, _topo(), notes2)["devices"]["R1"]["interfaces"]
    assert len(notes2) == 2


# ---- F: diagnostics rendering ---------------------------------------------------------------------------

def _diag(**kw):
    base = dict(owner_device="L3SW1", owner_os="iosxe", raw_name="XX1/1/2", status="UNKNOWN_TYPE", type_token="XX",
                reason="interface type is not in the registry", observing_device="R1", observing_os="iosxr",
                protocol="cdp", field="remote", action="Observation deferred")
    base.update(kw)
    return discovery.InterfaceDiagnostic(**base)


def test_f01_f07_f14_f17_group_count_and_order_are_deterministic():
    diags = [_diag(), _diag(raw_name="XX1/1/3", observing_device="R3"), _diag(), _diag(owner_device="A", raw_name="XX9")]
    text = "\n".join(climain.render_interface_normalization_warnings(diags))
    assert text == "\n".join(climain.render_interface_normalization_warnings(list(reversed(diags))))
    assert text.index("Device: A ") < text.index("Device: L3SW1")
    block = text[text.index("Device: L3SW1"):]
    assert "XX1/1/2" in block and "XX1/1/3" in block and "Observed by: R1, R3 (iosxr)" in block and "Observations: 3" in block
    assert "Unknown interface type: interface type is not in the registry" in block and "Action: Observation deferred" in block


def test_f02_f03_f10_titles_and_unknown_owner():
    text = "\n".join(climain.render_interface_normalization_warnings(
        [_diag(status="INVALID_FORMAT", owner_device=None, owner_os=None), _diag(status="AMBIGUOUS", reason="alias depends on the owner OS")]))
    assert "Invalid interface format" in text and "Ambiguous interface alias" in text and "Device: Unknown (type unknown)" in text


def test_f19_f20_f21_f36_untrusted_text_is_escaped_single_line_and_truncated():
    text = "\n".join(climain.render_interface_normalization_warnings([_diag(raw_name="Te0/0\x1b[2J\nfake line" + "x" * 200)]))
    assert "\x1b" not in text and "\nfake line" not in text and "...(truncated)" in text


def test_f28_no_section_when_nothing_to_report():
    assert climain.render_interface_normalization_warnings([]) == []


def test_f29_f30_summary_shows_warnings_even_when_candidate_unchanged_and_marks_limitations():
    result = _result([], interface_diagnostics=[_diag()])
    out = climain.render_discovery_summary(result, discovery.TopologyChanges())
    assert "Interface Normalization Warnings:" in out and "Topology candidate unchanged." in out
    changed = climain.render_discovery_summary(result, discovery.TopologyChanges(devices_added=1))
    assert "updated with limitations" in changed


def test_f23_unmanaged_neighbor_does_not_hide_a_local_interface_problem(pipeline):
    root, state = pipeline
    state["xe_cdp"] = _table([_row("unmanaged-sw", "XX 0/0", "131", "S I", "C9200L-48", "Gig 1/0/19")])
    result = discovery.discover_topology(root)
    assert [(d.owner_device, d.raw_name, d.status, d.field) for d in result.interface_diagnostics] == [("L3SW1", "XX 0/0", "UNKNOWN_TYPE", "local")]
    assert result.unresolved and result.deferred_observation_count == 0


# ---- J: contexts, display round trips ----------------------------------------------------------------------

@pytest.mark.parametrize("owner_os,remote_raw,local_raw,identity", [
    ("iosxe", "TF1/1/3", "Twe 1/1/3", ("25ge", "1/1/3")),
    ("iosxr", "TenGigE0/0/0/3", "Te0/0/0/3", ("10ge", "0/0/0/3")),
])
def test_j24_j25_j26_j27_j28_display_from_either_observation_alone_survives_a_reload(owner_os, remote_raw, local_raw, identity):
    for raw in (remote_raw, local_raw):
        shown = ii.compact_display(ii.normalize(raw, ii.ObservationContext(owner_os=owner_os, observing_os="iosxr", field="remote")))
        assert " " not in shown
        # forget every observation-time context; only the stored device type is left
        assert ii.normalize(shown, ii.ObservationContext(owner_os=owner_os)).identity == ii.InterfaceIdentity(*identity)
        assert ii.normalize(shown).status in (ii.RecognitionStatus.AMBIGUOUS, ii.RecognitionStatus.RECOGNIZED)  # never a wrong identity


def test_j01_j04_existing_display_names_including_spaces_are_never_rewritten():
    base = _topo(("L3SW1", "Twe 1/1/3", "R3", "TenGigE0/0/0/3"))
    cand = discovery.build_discovery_candidate(_result([("L3SW1", "Twe1/1/3", "R3", "Te0/0/0/3")]), base)
    assert cand["links"] == base["links"]


def test_j07_j08_j09_j10_j11_yaml_round_trip_keeps_links_l3_and_unknown_fields(lab_root):
    topo = _topo(("R1", "Te0/0/0/1", "R3", "Te0/0/0/0"), future={"k": [1, 2]})
    topo["name"] = "rt"
    topo["devices"]["R1"]["interfaces"] = {"Te0/0/0/1": {"ipv4_address": "192.0.2.1", "vrf": "default"}}
    labmod.write_topology("rt", topo, lab_root)
    assert labmod.load_topology("rt", lab_root) == topo


def test_j14_show_configuration_renders_the_five_links_and_they_resolve_back(pipeline):
    root, _ = pipeline
    cand = _candidate(discovery.discover_topology(root))
    cand["name"] = "sample_lab"
    lines = [l.strip() for l in climain.render_topology_block(cand) if "<->" in l]
    assert len(lines) == 5
    parsed = []
    for line in lines:
        left, right = line.split(" <-> ")
        (a, ai), (b, bi) = left.split(" ", 1), right.split(" ", 1)  # new names contain no space
        parsed.append({"a": a, "a_interface": ai, "b": b, "b_interface": bi})
    assert link_identities(parsed) == EXPECTED_FIVE


def test_k_mcp_toolset_unchanged():
    from network_lab_mcp import mcp_server

    names = {t.name for t in __import__("asyncio").run(mcp_server.mcp.list_tools())} if hasattr(mcp_server, "mcp") else None
    if names is None:  # pragma: no cover
        pytest.skip("mcp object name differs")
    assert names == {"get_active_topology", "get_execution_instructions", "terminal_open", "terminal_send",
                     "terminal_read", "terminal_list", "terminal_close"}
