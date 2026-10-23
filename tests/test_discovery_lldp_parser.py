# SPDX-License-Identifier: Apache-2.0
"""IOS XR `show lldp neighbors` parser (parse_lldp_neighbors).

Uses the sanitized real-lab-derived fixtures under tests/fixtures/lldp/ --
no access-info, credentials, or private addresses. The parser only ever
produces raw normalized observations; it never resolves a remote Device ID
to a logical managed device (see test_discovery_identity.py for that
separate stage).

Fail-closed contract: the parser must never represent invalid/
unrecognized command output (e.g. "% Invalid input detected...") as an
empty, "zero neighbors" result -- see the LldpParseError tests below."""

from __future__ import annotations

from pathlib import Path

import pytest

from network_lab_mcp.discovery import LldpParseError, parse_lldp_neighbors

FIXTURES = Path(__file__).parent / "fixtures" / "lldp"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_r1_fixture_parses_five_observations():
    text = _read("r1_show_lldp_neighbors.txt")
    observations = parse_lldp_neighbors(text, "R1")
    assert len(observations) == 5
    assert observations[0].local_device_id == "R1"
    assert observations[0].local_interface == "GigabitEthernet0/0/0/2"
    assert observations[0].remote_device_id_raw == "LAB_DC_R2.example"
    assert observations[0].remote_port_id == "GigabitEthernet0/0/0/2"
    assert observations[0].capabilities == ("router",)
    assert observations[0].source == "lldp"


def test_r2_fixture_parses_five_observations():
    text = _read("r2_show_lldp_neighbors.txt")
    observations = parse_lldp_neighbors(text, "R2")
    assert len(observations) == 5
    remote_ids = {o.remote_device_id_raw for o in observations}
    assert remote_ids == {"LAB_DC_R1.example", "LAB_DC_R4.example", "ASR9001_R1.example.com"}


def test_asr9001_observation_preserves_raw_evidence():
    text = _read("r1_show_lldp_neighbors.txt")
    observations = parse_lldp_neighbors(text, "R1")
    asr = next(o for o in observations if o.remote_device_id_raw == "ASR9001_R1.example.com")
    assert asr.local_device_id == "R1"
    assert asr.local_interface == "GigabitEthernet0/0/0/10"
    assert asr.remote_port_id == "GigabitEthernet0/0/0/0"
    assert asr.capabilities == ("router",)


def test_r3_fixture_parses_two_observations():
    text = _read("r3_show_lldp_neighbors.txt")
    observations = parse_lldp_neighbors(text, "R3")
    assert len(observations) == 2
    assert {o.remote_device_id_raw for o in observations} == {"LAB_DC_R1.example"}


def test_r4_fixture_parses_two_observations():
    text = _read("r4_show_lldp_neighbors.txt")
    observations = parse_lldp_neighbors(text, "R4")
    assert len(observations) == 2
    assert {o.remote_device_id_raw for o in observations} == {"LAB_DC_R2.example"}


# ---- robustness ----


_HEADER = "Device ID       Local Intf                      Hold-time  Capability      Port ID"


def test_parser_works_without_timestamp_line():
    text = f"{_HEADER}\nNEIGH.example Gi0/0/0/1 120 R Gi0/0/0/1\n\nTotal entries displayed: 1\n"
    observations = parse_lldp_neighbors(text, "R1")
    assert len(observations) == 1
    assert observations[0].remote_device_id_raw == "NEIGH.example"


def test_parser_works_with_timestamp_and_legend():
    text = (
        "RP/0/RP0/CPU0:R1#show lldp neighbors\n"
        "Sun Sep 20 19:07:12.895 JST\n"
        "Capability codes:\n"
        "        (R) Router, (B) Bridge, (T) Telephone, (C) DOCSIS Cable Device\n"
        "        (W) WLAN Access Point, (P) Repeater, (S) Station, (O) Other\n\n"
        f"{_HEADER}\n"
        "NEIGH.example Gi0/0/0/1 120 R Gi0/0/0/1\n\n"
        "Total entries displayed: 1\n\n"
        "RP/0/RP0/CPU0:R1#"
    )
    observations = parse_lldp_neighbors(text, "R1")
    assert len(observations) == 1


def test_parser_handles_multiple_neighbors():
    text = f"{_HEADER}\n" + "\n".join(f"N{i}.example Gi0/0/0/{i} 120 R Gi0/0/0/{i}" for i in range(1, 6))
    observations = parse_lldp_neighbors(text, "R1")
    assert len(observations) == 5


def test_parser_stops_at_trailing_prompt_with_no_blank_line_before_it():
    text = f"{_HEADER}\nNEIGH.example Gi0/0/0/1 120 R Gi0/0/0/1\nRP/0/RP0/CPU0:R1#"
    observations = parse_lldp_neighbors(text, "R1")
    assert len(observations) == 1


def test_parser_stops_at_total_entries_displayed():
    text = f"{_HEADER}\nNEIGH.example Gi0/0/0/1 120 R Gi0/0/0/1\nTotal entries displayed: 1\nNOTALINE.example Gi0/0/0/2 120 R Gi0/0/0/2"
    observations = parse_lldp_neighbors(text, "R1")
    assert len(observations) == 1


def test_parser_handles_fqdn_like_device_ids():
    text = f"{_HEADER}\nASR9001_R1.example.com Gi0/0/0/10 120 R Gi0/0/0/0\n"
    observations = parse_lldp_neighbors(text, "R1")
    assert observations[0].remote_device_id_raw == "ASR9001_R1.example.com"


def test_parser_handles_zero_neighbors():
    text = f"{_HEADER}\n\nTotal entries displayed: 0\n"
    observations = parse_lldp_neighbors(text, "R1")
    assert observations == []


def test_parser_tolerates_spacing_variation():
    text = f"{_HEADER}\nNEIGH.example    Gi0/0/0/1     120   R   Gi0/0/0/1\n"
    observations = parse_lldp_neighbors(text, "R1")
    assert len(observations) == 1
    assert observations[0].local_interface == "Gi0/0/0/1"


# ---- fail-closed: invalid/unrecognized output must never become [] ----


def test_invalid_input_error_raises_instead_of_empty_list():
    text = "% Invalid input detected at '^' marker.\n"
    with pytest.raises(LldpParseError):
        parse_lldp_neighbors(text, "R1")


def test_missing_table_header_raises():
    text = "RP/0/RP0/CPU0:R1#show lldp neighbors\nSun Sep 20 19:07:12.895 JST\n\nRP/0/RP0/CPU0:R1#"
    with pytest.raises(LldpParseError):
        parse_lldp_neighbors(text, "R1")


def test_completely_empty_output_raises():
    with pytest.raises(LldpParseError):
        parse_lldp_neighbors("", "R1")


def test_total_entries_mismatch_raises():
    # Header recognized and one row parsed, but the device's own declared
    # count disagrees -- must not silently succeed with the wrong count.
    text = f"{_HEADER}\nNEIGH.example Gi0/0/0/1 120 R Gi0/0/0/1\nTotal entries displayed: 2\n"
    with pytest.raises(LldpParseError):
        parse_lldp_neighbors(text, "R1")


def test_total_entries_match_succeeds():
    text = f"{_HEADER}\nNEIGH.example Gi0/0/0/1 120 R Gi0/0/0/1\nTotal entries displayed: 1\n"
    observations = parse_lldp_neighbors(text, "R1")
    assert len(observations) == 1


# ---- valid zero-neighbor output remains a successful parse ----


def test_valid_zero_neighbor_output_with_matching_total_succeeds():
    text = f"{_HEADER}\nTotal entries displayed: 0\n"
    observations = parse_lldp_neighbors(text, "R1")
    assert observations == []


def test_valid_zero_neighbor_output_without_total_line_succeeds():
    text = f"{_HEADER}\n\n"
    observations = parse_lldp_neighbors(text, "R1")
    assert observations == []


# ---- malformed rows are tolerated (skipped), not fatal, when the table
# header itself was genuinely recognized ----


def test_malformed_row_is_skipped_not_fatal():
    text = f"{_HEADER}\nNEIGH.example Gi0/0/0/1 120 R Gi0/0/0/1\ntruncated-row-missing-fields\n"
    observations = parse_lldp_neighbors(text, "R1")
    assert len(observations) == 1
    assert observations[0].remote_device_id_raw == "NEIGH.example"
