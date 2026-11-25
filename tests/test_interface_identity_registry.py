# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Interface Type Registry and Identity normalization (Test groups A, B).

Expected values are written out by hand; none is produced by the code under
test. Synthetic data only."""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from network_lab_mcp import interface_identity as ii
from network_lab_mcp.interface_identity import (
    Alias,
    InterfaceIdentity,
    ObservationContext,
    RecognitionStatus as S,
    RegistryError,
    TypeEntry,
)


def ctx(owner_os=None, **kw):
    return ObservationContext(owner_os=owner_os, **kw)


def ident(raw, os=None):
    result = ii.normalize(raw, ctx(os))
    assert result.recognized, (raw, result)
    return (result.identity.type_id, result.identity.identifier)


# ---- A: registry resolution ---------------------------------------------------------

# Every alias of the original 29-entry table, with the identity it must keep resolving to
# (hand-written; "legacy long name" is what the L3 naming wrapper must still produce).
LEGACY = [
    ("Gi0/0", "1ge", "GigabitEthernet0/0"), ("Gig0/0", "1ge", "GigabitEthernet0/0"),
    ("GigabitEthernet0/0", "1ge", "GigabitEthernet0/0"), ("Fa0/0", "fast-ethernet", "FastEthernet0/0"),
    ("Fas 0/0", "fast-ethernet", "FastEthernet0/0"), ("FastEthernet0/0", "fast-ethernet", "FastEthernet0/0"),
    ("Te0/0/0/3", "10ge", "TenGigabitEthernet0/0/0/3"), ("TenGigabitEthernet1/0/1", "10ge", "TenGigabitEthernet1/0/1"),
    ("Fo1/0/1", "40ge", "FortyGigabitEthernet1/0/1"), ("FortyGigabitEthernet1/0/1", "40ge", "FortyGigabitEthernet1/0/1"),
    ("Hu0/0/0/1", "100ge", "HundredGigE0/0/0/1"), ("HundredGigE0/0/0/1", "100ge", "HundredGigE0/0/0/1"),
    ("Tw1/0/1", "twogigabitethernet", "TwoGigabitEthernet1/0/1"),
    ("TwoGigabitEthernet1/0/1", "twogigabitethernet", "TwoGigabitEthernet1/0/1"),
    ("Et0/1", "ethernet", "Ethernet0/1"), ("Eth0/1", "ethernet", "Ethernet0/1"), ("Ethernet0/1", "ethernet", "Ethernet0/1"),
    ("Vl100", "vlan", "Vlan100"), ("Vlan100", "vlan", "Vlan100"),
    ("Lo0", "loopback", "Loopback0"), ("Loopback0", "loopback", "Loopback0"),
    ("Po10", "port-channel", "Port-channel10"), ("Port-channel10", "port-channel", "Port-channel10"),
    ("Se0/0", "serial", "Serial0/0"), ("Serial0/0", "serial", "Serial0/0"),
    ("Bv2000", "bvi", "BVI2000"), ("BVI2000", "bvi", "BVI2000"),
    ("Tu1", "tunnel", "Tunnel1"), ("Tunnel1", "tunnel", "Tunnel1"),
    ("Gi0/0.2000", "1ge", "GigabitEthernet0/0.2000"),
]


@pytest.mark.parametrize("raw,type_id,long_name", LEGACY)
def test_a01_a02_legacy_aliases_keep_their_identity_and_l3_name(raw, type_id, long_name):
    result = ii.normalize(raw)
    assert result.recognized and result.identity.type_id == type_id
    assert ii.legacy_long_name(raw) == long_name


def test_a01_every_legacy_alias_is_in_the_registry():
    legacy = {a.name for e in ii.REGISTRY for a in e.aliases if a.evidence == "LEGACY"}
    assert len(legacy) == 29
    assert legacy == {"gi", "gig", "gigabitethernet", "fa", "fas", "fastethernet", "te", "tengigabitethernet", "fo",
                      "fortygigabitethernet", "hu", "hundredgige", "tw", "twogigabitethernet", "et", "eth", "ethernet",
                      "vl", "vlan", "lo", "loopback", "po", "port-channel", "se", "serial", "bv", "bvi", "tu", "tunnel"}


def test_a03_tf_and_twe_are_one_25ge_interface_for_an_iosxe_owner():
    for raw in ("TF1/1/3", "Twe 1/1/3", "Twe1/1/3", "tf1/1/3", "TwentyFiveGigE1/1/3"):
        assert ident(raw, "iosxe") == ("25ge", "1/1/3")


def test_a04_te_and_tengige_are_one_10ge_interface():
    for raw in ("Te0/0/0/3", "TenGigE0/0/0/3", "TenGigabitEthernet0/0/0/3"):
        assert ident(raw, "iosxr") == ("10ge", "0/0/0/3")
    assert ident("Te0/0/0/3.100", "iosxr") == ident("TenGigE0/0/0/3.100", "iosxr") == ("10ge", "0/0/0/3.100")


def test_a05_a06_owner_os_not_observer_decides_and_field_is_not_needed():
    # IOS XE-owned port seen as a remote port id by an IOS XR device, and as a local interface by itself.
    remote = ii.normalize("TF1/1/3", ObservationContext(owner_os="iosxe", observing_os="iosxr", field="remote"))
    local = ii.normalize("Twe 1/1/3", ObservationContext(owner_os="iosxe", observing_os="iosxe", field="local"))
    assert remote.identity == local.identity == InterfaceIdentity("25ge", "1/1/3")
    # IOS XR-owned port seen by IOS XE, and by itself.
    assert ii.normalize("TenGigE0/0/0/3", ObservationContext(owner_os="iosxr", observing_os="iosxe", field="remote")).identity == \
        ii.normalize("Te0/0/0/3", ObservationContext(owner_os="iosxr", observing_os="iosxr", field="local")).identity


def test_a07_common_alias_needs_no_context():
    assert ident("Gig 0/0") == ("1ge", "0/0") and ident("TenGigE0/0/0/3") == ("10ge", "0/0/0/3")


def test_a08_owner_dependent_alias_without_context_is_never_guessed():
    assert ii.normalize("TF1/1/3").status is S.AMBIGUOUS
    assert ii.normalize("Twe 1/1/3", ctx(None)).status is S.AMBIGUOUS
    # owner known, alias not defined for that OS: not silently converted
    assert ii.normalize("Twe1/1/3", ctx("iosxr")).status is S.UNKNOWN_TYPE
    assert ii.normalize("BE10", ctx("iosxe")).status is S.UNKNOWN_TYPE
    assert ident("BE10", "iosxr") == ("bundle-ether", "10")


def test_a09_case_insensitive_type_case_preserved_identifier():
    assert ident("GIG0/0") == ident("gig0/0") == ("1ge", "0/0")


def test_a10_a11_space_between_type_and_number_only():
    assert ident("Twe 1/1/3", "iosxe") == ident("Twe  1/1/3", "iosxe") == ("25ge", "1/1/3")
    for bad in ("Te 0/0 /1", "Te0 /0/0", "T e0/0/0/3", "Te0/0/0/ 3"):
        assert ii.normalize(bad, ctx("iosxr")).status in (S.INVALID_FORMAT, S.UNKNOWN_TYPE), bad


@pytest.mark.parametrize("a,b", [("Tw1/0/1", "Twe1/0/1"), ("Te0/0/0/3", "Hu0/0/0/3"), ("Fo1/0/1", "FourHundredGigE1/0/1"),
                                  ("Gi0/0", "Te0/0"), ("Fi1/0/1", "Fo1/0/1")])
def test_a12_a16_prefix_overlapping_aliases_never_merge(a, b):
    ra, rb = ii.normalize(a, ctx("iosxe")), ii.normalize(b, ctx("iosxe"))
    assert not (ra.recognized and rb.recognized and ra.identity == rb.identity)


@pytest.mark.parametrize("raw", ["Fi1/0/1", "FiveGigabitEthernet1/0/1", "FiftyGigE0/0/0/1", "Tx1/1/1", "TwentyFive1/1/1",
                                  "TFx1/1/1", "Teen0/0/0/1", "GigabitEther0/0", "MgmtEth0/RP0/CPU0/0", "Null0"])
def test_a17_a24_a33_unknown_or_unverified_names_are_not_converted(raw):
    result = ii.normalize(raw, ctx("iosxe"))
    assert result.status is S.UNKNOWN_TYPE and result.identity is None


@pytest.mark.parametrize("raw,status", [("Te0/0/0/3x", S.INVALID_FORMAT), ("Te", S.INVALID_FORMAT), ("Te0/", S.INVALID_FORMAT),
                                         ("Te0//1", S.INVALID_FORMAT), ("0/0/0", S.INVALID_FORMAT), ("", S.INVALID_FORMAT),
                                         ("Te0/0\x1b[2J", S.INVALID_FORMAT), ("Te0/0\n1", S.INVALID_FORMAT),
                                         ("Gi0/0 junk", S.INVALID_FORMAT), ("XX!!", S.UNKNOWN_TYPE)])
def test_a18_a20_b12_b13_b18_invalid_formats(raw, status):
    assert ii.normalize(raw, ctx("iosxr")).status is status


def test_b09_identifier_text_is_kept_verbatim_leading_zeros_not_stripped():
    assert ident("Te0/0/0/03", "iosxr") != ident("Te0/0/0/3", "iosxr")
    assert ident("Gi01/0", None)[1] == "01/0"


@pytest.mark.parametrize("a,b", [("TF1/1/3", "TF1/1/4"), ("Te0/0/0/3", "Hu0/0/0/3"), ("Te0/0/0/3", "Te0/0/0/3.100"),
                                  ("Te0/0/0/3.100", "Te0/0/0/3.200"), ("Bundle-Ether10", "Port-channel10"),
                                  ("Gi0/0/0/0", "Fa0/0/0/0"), ("Eth0/1", "Gi0/1")])
def test_b03_b08_different_interfaces_stay_different(a, b):
    assert ii.normalize(a, ctx("iosxr")).key != ii.normalize(b, ctx("iosxr")).key


def test_b10_b11_breakout_and_channelized_forms():
    assert ident("Te0/0/0/0/1", "iosxr") == ("10ge", "0/0/0/0/1")
    assert ident("Serial0/1/0:0") == ("serial", "0/1/0:0")


def test_b14_b15_b16_identity_equality_hash_and_raw_fallback_never_collides():
    a, b = InterfaceIdentity("10ge", "0/0/0/3"), InterfaceIdentity("10ge", "0/0/0/3")
    assert a == b and hash(a) == hash(b) and len({a, b}) == 1
    raw = ii.normalize("XX0/0/0/3").key
    assert isinstance(raw, ii.RawIdentity) and raw != a and raw.raw == "XX0/0/0/3"
    assert ii.RawIdentity("10ge") != InterfaceIdentity("10ge", "")


def test_b17_raw_name_is_preserved_in_result_and_b19_deterministic():
    r1 = ii.normalize("  Twe 1/1/3 ", ctx("iosxe"))
    assert r1.raw_name == "Twe 1/1/3" and r1 == ii.normalize("  Twe 1/1/3 ", ctx("iosxe"))


def test_b20_identity_is_not_a_cli_name():
    assert not hasattr(InterfaceIdentity("25ge", "1/1/3"), "display")
    assert str(InterfaceIdentity("25ge", "1/1/3")) != "Twe1/1/3"


# ---- A22/A23/A28/A29/A30: registry validation -------------------------------------------

def entry(type_id, *aliases):
    return TypeEntry(type_id, "ethernet", tuple(aliases))


def test_a28_invalid_entries_are_detected():
    ok = Alias("zz", "LEGACY")
    for bad in (
        (entry("a", Alias("", "LEGACY")),),
        (entry("a", Alias("Upper", "LEGACY")),),
        (entry("a", Alias("x1", "LEGACY")),),
        (entry("a", Alias("zz", "UNVERIFIED")),),
        (entry("a", Alias("zz", "LEGACY", frozenset({"nxos"}))),),
        (entry("a", Alias("zz", "LEGACY", frozenset())),),
        (entry("a", ok), entry("a")),
        (entry("", ok),),
    ):
        with pytest.raises(RegistryError):
            ii.validate_registry(bad)


def test_a22_alias_collisions_are_detected_for_any_overlap():
    both = Alias("zz", "LEGACY")
    xr = Alias("zz", "LEGACY", frozenset({"iosxr"}))
    xr_xe = Alias("zz", "LEGACY", frozenset({"iosxr", "iosxe"}))
    xe = Alias("zz", "LEGACY", frozenset({"iosxe"}))
    with pytest.raises(RegistryError):
        ii.validate_registry((entry("a", both), entry("b", both)))        # identical conditions
    with pytest.raises(RegistryError):
        ii.validate_registry((entry("a", both), entry("b", xr)))          # unrestricted overlaps everything
    with pytest.raises(RegistryError):
        ii.validate_registry((entry("a", xr_xe), entry("b", xe)))         # partial overlap
    with pytest.raises(RegistryError):
        ii.validate_registry((entry("a", xr), entry("a", xr)))            # same type twice
    ii.validate_registry((entry("a", xr), entry("b", xe)))                # disjoint contexts are fine


def test_a23_a30_registry_order_does_not_change_the_result_or_validity():
    for perm in itertools.permutations(ii.REGISTRY[:5]):
        ii.validate_registry(perm)
    reversed_index = {}
    for e in reversed(ii.REGISTRY):
        for a in e.aliases:
            reversed_index[a.name] = reversed_index.get(a.name, ()) + ((e.type_id, a),)
    assert {k: {t for t, _ in v} for k, v in reversed_index.items()} == {k: {t for t, _ in v} for k, v in ii._BY_ALIAS.items()}


def test_a31_a32_every_active_alias_has_an_evidence_class_and_restricted_os_is_explicit():
    for e in ii.REGISTRY:
        for a in e.aliases:
            assert a.evidence and set(a.evidence.split("+")) <= {"DOCUMENTED", "OBSERVED", "LEGACY"}
    twe = next(a for e in ii.REGISTRY for a in e.aliases if a.name == "twe")
    assert twe.os == frozenset({"iosxe"})  # observed on IOS XE; not generalized to other OSes


def test_a26_new_type_can_be_added_without_touching_resolution_code():
    extended = ii.REGISTRY + (entry("800ge", Alias("eightgige", "DOCUMENTED")),)
    ii.validate_registry(extended)


def test_a34_type_tokens_with_hyphen_are_parsed():
    assert ident("Port-channel10") == ("port-channel", "10") and ident("Bundle-Ether10", "iosxr")[0] == "bundle-ether"


# ---- display helpers -----------------------------------------------------------------

def test_compact_display_removes_only_the_type_number_space_and_re_resolves():
    result = ii.normalize("Twe 1/1/3", ctx("iosxe"))
    assert ii.compact_display(result) == "Twe1/1/3"
    assert ii.re_resolves("Twe1/1/3", result.identity, "iosxe")
    assert not ii.re_resolves("Twe1/1/3", result.identity, None)  # owner type is required for Twe
    assert ii.compact_display(ii.normalize("XX1/1/1")) is None


def test_safe_display_escapes_controls_and_truncates():
    out = ii.safe_display("a\x1b[2Jb\nc" + "z" * 100, 20)
    assert "\x1b" not in out and "\n" not in out and out.endswith("...(truncated)")


def test_result_is_immutable():
    result = ii.normalize("Te0/0/0/3", ctx("iosxr"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.raw_name = "x"
