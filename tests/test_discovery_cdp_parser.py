# SPDX-License-Identifier: Apache-2.0
"""`parse_cdp_neighbors()` -- the CDP counterpart of
parse_lldp_neighbors(). Handles both real-world row shapes: an IOS XR-style
one-line row, and an IOS/IOS XE-style row where a long/FQDN Device ID wraps
onto its own line with the remaining fields on the *next* physical line.
Never assumes fixed column byte offsets (only the header's own "Port ID"
column start is used, plus whitespace/holdtime-token-based splitting for
everything before it -- see the function's own docstring).

All device/hostname values below are sanitized fixture-only fake values,
never real lab access-info content."""

from __future__ import annotations

from network_lab_mcp.discovery import NeighborObservation, parse_cdp_neighbors

_HEADER = "Device ID        Local Intrfce     Holdtme    Capability  Platform    Port ID"
# Column widths derived from _HEADER itself (Device ID=17, Local Intrfce=18,
# Holdtme=11, Capability=12, Platform=12, Port ID=rest of the line) --
# fixture rows below are hand-padded to these same widths so the parser's
# single positional anchor (the header's own "Port ID" column start) lines
# up correctly, exactly like real Cisco CDP table rendering does.
_W_DEVICE_ID, _W_LOCAL_INTF, _W_HOLDTIME, _W_CAPABILITY, _W_PLATFORM = 17, 18, 11, 12, 12


def _one_line_row(device_id: str, local_intf: str, holdtime: str, capability: str, platform: str, port_id: str) -> str:
    assert len(device_id) < _W_DEVICE_ID, "use a wrapped row for a Device ID this long"
    return (
        f"{device_id:<{_W_DEVICE_ID}}{local_intf:<{_W_LOCAL_INTF}}{holdtime:<{_W_HOLDTIME}}"
        f"{capability:<{_W_CAPABILITY}}{platform:<{_W_PLATFORM}}{port_id}"
    )


def _continuation_row(local_intf: str, holdtime: str, capability: str, platform: str, port_id: str) -> str:
    prefix = " " * _W_DEVICE_ID
    return (
        f"{prefix}{local_intf:<{_W_LOCAL_INTF}}{holdtime:<{_W_HOLDTIME}}"
        f"{capability:<{_W_CAPABILITY}}{platform:<{_W_PLATFORM}}{port_id}"
    )


def test_one_line_iosxr_style_row():
    text = "\n".join(
        [
            "Capability Codes: R - Router, T - Trans Bridge, B - Source Route Bridge",
            "                  S - Switch, H - Host, I - IGMP, r - Repeater",
            "",
            _HEADER,
            _one_line_row("PAGENT", "Gi0/0/0/10", "144", "R", "Cisco 720", "Gig0/0"),
            "",
        ]
    )
    observations = parse_cdp_neighbors(text, "R1")

    assert observations == [
        NeighborObservation(
            local_device_id="R1",
            local_interface="Gi0/0/0/10",
            remote_device_id_raw="PAGENT",
            remote_port_id="Gig0/0",
            capabilities=("router",),
            source="cdp",
        )
    ]


def test_wrapped_fqdn_device_id_row():
    text = "\n".join(
        [
            _HEADER,
            "labsw-a07.example.com",
            _continuation_row("Fas 0/0", "159", "S I", "WS-C2960X", "Gig 1/0/16"),
            "",
        ]
    )
    observations = parse_cdp_neighbors(text, "PAGENT")

    assert len(observations) == 1
    obs = observations[0]
    assert obs.remote_device_id_raw == "labsw-a07.example.com"
    assert obs.local_interface == "Fas 0/0"
    assert obs.remote_port_id == "Gig 1/0/16"
    assert obs.capabilities == ("switch", "igmp")
    assert obs.source == "cdp"


def test_mixed_one_line_and_wrapped_rows_in_the_same_table():
    text = "\n".join(
        [
            _HEADER,
            _one_line_row("PAGENT", "Gi0/0/0/10", "144", "R", "Cisco 720", "Gig0/0"),
            "labsw-a07.example.com",
            _continuation_row("Fas 0/0", "159", "S I", "WS-C2960X", "Gig 1/0/16"),
            "",
        ]
    )
    observations = parse_cdp_neighbors(text, "R1")

    assert len(observations) == 2
    assert {o.remote_device_id_raw for o in observations} == {"PAGENT", "labsw-a07.example.com"}


def test_multi_word_port_id_and_platform_on_one_line():
    text = "\n".join(
        [
            _HEADER,
            _one_line_row("R1", "Gig 0/0", "137", "R", "ASR9K Ser", "Gig 0/0/0/10"),
            "",
        ]
    )
    observations = parse_cdp_neighbors(text, "PAGENT")

    assert len(observations) == 1
    obs = observations[0]
    assert obs.remote_device_id_raw == "R1"
    assert obs.local_interface == "Gig 0/0"
    assert obs.remote_port_id == "Gig 0/0/0/10"


def test_zero_neighbors_returns_empty_list():
    text = "\n".join([_HEADER, ""])
    assert parse_cdp_neighbors(text, "R1") == []


def test_cdp_disabled_or_unsupported_returns_empty_list_not_an_error():
    """Deliberately more lenient than LLDP's parser: a missing/unrecognized
    table header must never fail the whole
    device's collection just because CDP is off."""
    text = "% CDP is not enabled\n"
    assert parse_cdp_neighbors(text, "R1") == []


def test_completely_unrelated_output_returns_empty_list():
    text = "% Invalid input detected at '^' marker.\n"
    assert parse_cdp_neighbors(text, "R1") == []


def test_malformed_row_is_skipped_without_crashing():
    text = "\n".join(
        [
            _HEADER,
            "this row has no holdtime number at all",
            _one_line_row("PAGENT", "Gi0/0/0/10", "144", "R", "Cisco 720", "Gig0/0"),
            "",
        ]
    )
    observations = parse_cdp_neighbors(text, "R1")
    assert len(observations) == 1
    assert observations[0].remote_device_id_raw == "PAGENT"


def test_multiple_neighbors_deterministic_order():
    text = "\n".join(
        [
            _HEADER,
            _one_line_row("PAGENT", "Gi0/0/0/10", "144", "R", "Cisco 720", "Gig0/0"),
            _one_line_row("R2", "Gi0/0/0/2", "120", "R", "ASR9K Ser", "Gi0/0/0/2"),
            "",
        ]
    )
    observations = parse_cdp_neighbors(text, "R1")
    assert [o.remote_device_id_raw for o in observations] == ["PAGENT", "R2"]
