# SPDX-License-Identifier: Apache-2.0
"""Identity resolution: mapping an LLDP remote Device ID to a logical
managed device ID (resolve_remote_identity()). Deliberately bounded --
exact/case-normalized/short-name-alias matching only, fails closed
(returns None) on anything ambiguous or unknown. No substring search, no
fuzzy matching, no inference from the logical device ID itself."""

from __future__ import annotations

from network_lab_mcp.discovery import resolve_remote_identity

IDENTITY_MAP = {
    "R1": "LAB_DC_R1",
    "R2": "LAB_DC_R2",
    "R3": "LAB_DC_R3",
    "R4": "LAB_DC_R4",
}


def test_exact_hostname_match():
    assert resolve_remote_identity("LAB_DC_R1", IDENTITY_MAP) == "R1"


def test_fqdn_style_alias_resolves_uniquely():
    assert resolve_remote_identity("LAB_DC_R2.example", IDENTITY_MAP) == "R2"


def test_case_insensitive_exact_match():
    assert resolve_remote_identity("lab_dc_r3", IDENTITY_MAP) == "R3"


def test_case_insensitive_alias_match():
    assert resolve_remote_identity("lab_dc_r4.EXAMPLE", IDENTITY_MAP) == "R4"


def test_unknown_hostname_is_unresolved():
    assert resolve_remote_identity("ASR9001_R1.example.com", IDENTITY_MAP) is None


def test_ambiguous_hostname_is_unresolved():
    ambiguous_map = {"R1": "SHARED", "R2": "SHARED"}
    assert resolve_remote_identity("SHARED", ambiguous_map) is None


def test_ambiguous_alias_is_unresolved():
    ambiguous_map = {"R1": "DUP", "R2": "DUP"}
    assert resolve_remote_identity("DUP.example", ambiguous_map) is None


def test_does_not_use_substring_matching():
    # "R2" appearing inside a logical ID string must never cause a match --
    # only comparisons against the *hostname* value are allowed.
    tricky_map = {"R2": "totally-unrelated-hostname"}
    assert resolve_remote_identity("something-R2-something", tricky_map) is None


def test_empty_identity_map_is_unresolved():
    assert resolve_remote_identity("LAB_DC_R1", {}) is None
