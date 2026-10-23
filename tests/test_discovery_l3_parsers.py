# SPDX-License-Identifier: Apache-2.0
"""L3 interface enrichment parsers -- parse_ipv4_interface_brief()
(IOS XR's `show ipv4 interface brief`), parse_ip_interface_brief() +
parse_show_vrf() (classic IOS / IOS XE's `show ip interface brief` +
`show vrf`), _canonicalize_interface_name(), and
_build_device_interface_fields() (the management-address exclusion /
unassigned-removal rule). Only ipv4_address + vrf are ever produced --
never a prefix length, never operational status."""

from __future__ import annotations

import pytest

from network_lab_mcp.discovery import (
    L3ParseError,
    _build_device_interface_fields,
    _canonicalize_interface_name,
    _combine_ios_style_l3,
    parse_ip_interface_brief,
    parse_ipv4_interface_brief,
    parse_show_vrf,
)

# ---- interface-name canonicalization ----


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Gi0/0", "GigabitEthernet0/0"),
        ("Gig0/0", "GigabitEthernet0/0"),
        ("GigabitEthernet0/0", "GigabitEthernet0/0"),
        ("Fa0/0", "FastEthernet0/0"),
        ("Fas 0/0", "FastEthernet0/0"),
        ("Gi0/0.2000", "GigabitEthernet0/0.2000"),
        ("Vlan100", "Vlan100"),
        ("Vl100", "Vlan100"),
        ("BVI2000", "BVI2000"),
    ],
)
def test_canonicalize_known_abbreviations(raw, expected):
    assert _canonicalize_interface_name(raw) == expected


def test_canonicalize_unknown_prefix_is_left_unchanged():
    assert _canonicalize_interface_name("WeirdIntf0/0") == "WeirdIntf0/0"


# ---- IOS XR: show ipv4 interface brief ----

_IOSXR_L3_HEADER = "Interface                     IP-Address   Status Protocol Vrf-Name"


def test_iosxr_l3_parses_interface_ip_and_vrf_ignoring_status_protocol():
    text = "\n".join(
        [
            _IOSXR_L3_HEADER,
            "GigabitEthernet0/0/0/2        10.0.12.1    Up     Up       default",
            "BVI2000                       10.20.0.253  Up     Up       VRF_ELAN_2000",
            "",
        ]
    )
    result = parse_ipv4_interface_brief(text)
    assert result == {
        "GigabitEthernet0/0/0/2": ("10.0.12.1", "default"),
        "BVI2000": ("10.20.0.253", "VRF_ELAN_2000"),
    }


def test_iosxr_l3_unassigned_becomes_none_not_a_string():
    text = "\n".join(
        [
            _IOSXR_L3_HEADER,
            "GigabitEthernet0/0/0/9        unassigned   Shutdown Down     default",
            "",
        ]
    )
    result = parse_ipv4_interface_brief(text)
    assert result["GigabitEthernet0/0/0/9"] == (None, "default")


def test_iosxr_l3_unrecognized_output_raises():
    with pytest.raises(L3ParseError):
        parse_ipv4_interface_brief("% Invalid input detected at '^' marker.\n")


def test_iosxr_l3_header_found_zero_rows_is_empty_not_an_error():
    assert parse_ipv4_interface_brief(_IOSXR_L3_HEADER + "\n") == {}


def test_iosxr_l3_malformed_row_is_skipped():
    text = "\n".join([_IOSXR_L3_HEADER, "TooFewTokens 10.0.0.1", ""])
    assert parse_ipv4_interface_brief(text) == {}


# ---- classic IOS / IOS XE: show ip interface brief ----

_IP_BRIEF_HEADER = "Interface              IP-Address      OK? Method Status                Protocol"


def test_ip_interface_brief_parses_interface_and_address_ignoring_rest():
    text = "\n".join(
        [
            _IP_BRIEF_HEADER,
            "GigabitEthernet0/0     192.0.2.10      YES NVRAM  up                    up",
            "Vlan100                10.10.100.1     YES NVRAM  up                    up",
            "GigabitEthernet1/0/1   unassigned      YES NVRAM  administratively down down",
            "",
        ]
    )
    result = parse_ip_interface_brief(text)
    assert result == {
        "GigabitEthernet0/0": "192.0.2.10",
        "Vlan100": "10.10.100.1",
        "GigabitEthernet1/0/1": None,
    }


def test_ip_interface_brief_canonicalizes_abbreviated_interface_names():
    text = "\n".join([_IP_BRIEF_HEADER, "Gi0/0                  192.0.2.10      YES NVRAM  up  up", ""])
    result = parse_ip_interface_brief(text)
    assert result == {"GigabitEthernet0/0": "192.0.2.10"}


def test_ip_interface_brief_unrecognized_output_raises():
    with pytest.raises(L3ParseError):
        parse_ip_interface_brief("% Invalid input detected at '^' marker.\n")


# ---- classic IOS / IOS XE: show vrf ----

_VRF_HEADER = "  Name                             Default RD            Protocols   Interfaces"


def test_show_vrf_maps_interfaces_to_named_vrf():
    text = "\n".join(
        [
            _VRF_HEADER,
            "  Mgmt-vrf                         <not set>              ipv4        Gi0/0",
            "  CUSTOMER_A                       <not set>              ipv4        Vlan100",
            "",
        ]
    )
    result = parse_show_vrf(text)
    assert result == {"GigabitEthernet0/0": "Mgmt-vrf", "Vlan100": "CUSTOMER_A"}


def test_show_vrf_wrapped_continuation_line_associates_with_preceding_vrf():
    text = "\n".join(
        [
            _VRF_HEADER,
            "  CUSTOMER_A                       <not set>              ipv4        Vl100",
            "                                                                       Vl200",
            "",
        ]
    )
    result = parse_show_vrf(text)
    assert result == {"Vlan100": "CUSTOMER_A", "Vlan200": "CUSTOMER_A"}


def test_show_vrf_header_found_zero_rows_is_empty_not_an_error():
    assert parse_show_vrf(_VRF_HEADER + "\n") == {}


def test_show_vrf_unrecognized_output_raises():
    with pytest.raises(L3ParseError):
        parse_show_vrf("% Invalid input detected at '^' marker.\n")


# ---- PAGENT-style combined example ----


def test_pagent_style_combination_resolves_vrf_per_interface_default_otherwise():
    ip_brief = parse_ip_interface_brief(
        "\n".join(
            [
                _IP_BRIEF_HEADER,
                "FastEthernet0/0         192.0.2.20      YES NVRAM  up up",
                "GigabitEthernet0/0      unassigned      YES NVRAM  up up",
                "GigabitEthernet0/0.2000 10.20.0.10      YES NVRAM  up up",
                "GigabitEthernet0/1.2000 10.20.0.20      YES NVRAM  up up",
                "",
            ]
        )
    )
    vrf_map = parse_show_vrf(
        "\n".join(
            [
                _VRF_HEADER,
                "  mgmt                             <not set>              ipv4        Fa0/0",
                "  tgn1                             <not set>              ipv4        Gi0/0.2000",
                "  tgn2                             <not set>              ipv4        Gi0/1.2000",
                "",
            ]
        )
    )
    combined = _combine_ios_style_l3(ip_brief, vrf_map)
    assert combined["FastEthernet0/0"] == ("192.0.2.20", "mgmt")
    assert combined["GigabitEthernet0/0.2000"] == ("10.20.0.10", "tgn1")
    assert combined["GigabitEthernet0/1.2000"] == ("10.20.0.20", "tgn2")
    # Unassigned and not otherwise VRF-mapped -- default is never guessed
    # in place of a real observation for an address-bearing interface, but
    # it's harmless here since this interface has no address at all.
    assert combined["GigabitEthernet0/0"] == (None, "default")


# ---- _build_device_interface_fields(): management exclusion + removal ----


def test_build_device_interface_fields_creates_entries_only_for_assigned_addresses():
    raw_l3 = {"Gi0/0": ("10.0.12.1", "default"), "Gi0/1": (None, "default")}
    fields = _build_device_interface_fields(raw_l3, management_address=None)
    assert fields == {
        "GigabitEthernet0/0": {"ipv4_address": "10.0.12.1", "vrf": "default"},
        "GigabitEthernet0/1": None,
    }


def test_build_device_interface_fields_excludes_management_address():
    raw_l3 = {
        "FastEthernet0/0": ("192.0.2.20", "mgmt"),
        "GigabitEthernet0/0.2000": ("10.20.0.10", "tgn1"),
    }
    fields = _build_device_interface_fields(raw_l3, management_address="192.0.2.20")
    assert fields["FastEthernet0/0"] is None
    assert fields["GigabitEthernet0/0.2000"] == {"ipv4_address": "10.20.0.10", "vrf": "tgn1"}


def test_build_device_interface_fields_no_operational_state_no_prefix():
    raw_l3 = {"Gi0/0": ("10.0.12.1", "default")}
    fields = _build_device_interface_fields(raw_l3, management_address=None)
    value = fields["GigabitEthernet0/0"]
    assert set(value) == {"ipv4_address", "vrf"}


def test_duplicate_ipv4_address_across_different_vrfs_is_not_a_conflict():
    """The same address may legitimately appear in
    different VRFs or isolated lab contexts -- no global uniqueness check
    exists anywhere in this pipeline."""
    raw_l3 = {"Gi0/0": ("10.20.0.10", "tgn1"), "Gi0/1": ("10.20.0.10", "tgn2")}
    fields = _build_device_interface_fields(raw_l3, management_address=None)
    assert fields["GigabitEthernet0/0"] == {"ipv4_address": "10.20.0.10", "vrf": "tgn1"}
    assert fields["GigabitEthernet0/1"] == {"ipv4_address": "10.20.0.10", "vrf": "tgn2"}
