"""IOS XR LLDP/CDP "not enabled" handling during Discovery.

Only the two confirmed responses ("% LLDP is not enabled" /
"% CDP is not enabled") are classified as disabled, and only when they are
the entire meaningful output for their own protocol. Disabled is an
observation-less state (a warning), never link evidence. Collection is
monkeypatched (no terminal/tmux/ssh); isolated lab_root; fictional data."""

from __future__ import annotations

import pytest

from network_lab_mcp import discovery, lab, terminal
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain

LLDP_OFF = "Fri Oct 9 14:59:01.185 JST\n% LLDP is not enabled\n"
CDP_OFF = "Fri Oct 9 14:59:45.758 JST\n% CDP is not enabled\n"
LLDP_HDR = "Device ID       Local Intf                      Hold-time  Capability      Port ID"
CDP_HDR = "Device ID        Local Intrfce     Holdtme    Capability  Platform  Port ID"
SENTINEL = "SENTINEL-pw-do-not-leak"


def lldp_table(*rows):
    body = "\n".join(f"{r[0]}  {r[1]}  120  R  {r[2]}" for r in rows)
    return f"{LLDP_HDR}\n{body}\n\nTotal entries displayed: {len(rows)}\n"


def cdp_table(*rows):
    col = CDP_HDR.index("Port ID")
    body = "\n".join((f"{r[0]}  {r[1]}  144  R  ASR9K  ").ljust(col) + r[2] for r in rows)
    return f"{CDP_HDR}\n{body}\n"


LLDP_EMPTY = f"{LLDP_HDR}\nTotal entries displayed: 0\n"
CDP_EMPTY = f"{CDP_HDR}\n"  # recognized CDP table, zero neighbors (valid success)


# ---- parser-level classification (T01-T03, T07, T09-T14) ------------------


@pytest.mark.parametrize("proto,text", [("lldp", LLDP_OFF), ("cdp", CDP_OFF)])
def test_t01_t02_exact_disabled(proto, text):
    assert discovery.iosxr_protocol_disabled(text, proto, "R1") is True


def test_t03_disabled_with_echo_timestamp_prompt():
    text = (
        "RP/0/RP0/CPU0:R1#show lldp neighbors\nFri Oct 9 14:59:01.185 JST\n% LLDP is not enabled\n\nRP/0/RP0/CPU0:R1#\n"
    )
    assert discovery.iosxr_protocol_disabled(text, "lldp", "R1") is True


@pytest.mark.parametrize(
    "proto,text",
    [
        ("lldp", "% CDP is not enabled\n"),  # T11 wrong protocol
        ("cdp", "% LLDP is not enabled\n"),  # T12
        ("lldp", "% LLDP is not enabled\n" + LLDP_HDR + "\n"),  # T10 mixed
        ("lldp", "% LLDP is not enabled\n% something else\n"),  # T13 extra content
        ("lldp", "% lldp is not enabled\n"),  # exact casing only
        ("lldp", "% LLDP is not enabled.\n"),  # not the confirmed text
        ("cdp", "% CDP is not enabled\n% CDP is not enabled\n"),
    ],
)
def test_disabled_like_but_not_exact_fails_closed(proto, text):
    with pytest.raises(discovery.DiscoveryError):
        discovery.iosxr_protocol_disabled(text, proto, "R1")


@pytest.mark.parametrize("text", ["", "% Invalid input detected at '^' marker.\n", "% LLDP is not running\n", LLDP_EMPTY])
def test_non_disabled_outputs_are_not_classified_disabled(text):
    assert discovery.iosxr_protocol_disabled(text, "lldp", "R1") is False


@pytest.mark.parametrize("text", ["", "% Invalid input detected at '^' marker.\n", "% LLDP is not running\n", LLDP_OFF])
def test_t07_t09_t14_lldp_parser_stays_fail_closed(text):
    """The strict parser itself is unchanged: unknown/empty/disabled-looking
    text is never an empty success (disabled is handled one layer above)."""
    with pytest.raises(discovery.LldpParseError):
        discovery.parse_lldp_neighbors(text, "R1")


def test_t04_t05_t06_existing_parsers_unchanged():
    assert len(discovery.parse_lldp_neighbors(lldp_table(("HOST-R2", "Gi0/0/0/2", "Gi0/0/0/2")), "R1")) == 1
    assert discovery.parse_lldp_neighbors(LLDP_EMPTY, "R1") == []  # no-neighbors != disabled
    assert len(discovery.parse_cdp_neighbors(cdp_table(("HOST-R3", "Gi0/0/0/3", "Gi0/0/0/3")), "R1")) == 1
    with pytest.raises(discovery.LldpParseError):
        discovery.parse_lldp_neighbors(f"{LLDP_HDR}\nHOST-R2  Gi0/0/0/2  120  R  Gi0/0/0/2\nTotal entries displayed: 5\n", "R1")


# ---- discovery integration ---------------------------------------------------


@pytest.fixture(autouse=True)
def _env(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    closed: list[str] = []
    monkeypatch.setattr(terminal, "close_bootstrap_terminal", lambda d: closed.append(d))
    return closed


def _targets(lab_root, ids, types=None):
    devices = {
        d: {"type": (types or {}).get(d, "iosxr"), "address": f"192.0.2.{i + 10}", "transport": "ssh", "port": 22, "password": SENTINEL}
        for i, d in enumerate(ids)
    }
    lab.write_access_info("sample_lab", {"name": "sample_lab", "devices": devices}, lab_root)


def _run(lab_root, monkeypatch, outputs):
    """outputs: {device: (lldp_text, cdp_text)}"""
    _targets(lab_root, list(outputs))

    def fake(device_id, cfg):
        lldp, cdp = outputs[device_id]
        return {
            "hostname": f"HOST-{device_id}",
            "show_version": "x",
            "show_lldp_neighbors": lldp,
            "show_cdp_neighbors": cdp,
        }

    monkeypatch.setattr(discovery, "_bootstrap_collect", fake)
    return discovery.discover_topology(lab_root)


def _pairs(result):
    return sorted((l.a_device, l.a_interface, l.b_device, l.b_interface) for l in result.managed_links)


R1R2 = ("HOST-R2", "Gi0/0/0/2", "Gi0/0/0/2")


def test_i01_lldp_disabled_cdp_links_retained(lab_root, monkeypatch):
    r = _run(lab_root, monkeypatch, {"R1": (LLDP_OFF, cdp_table(R1R2)), "R2": (LLDP_EMPTY, CDP_EMPTY)})
    assert _pairs(r) == [("R1", "Gi0/0/0/2", "R2", "Gi0/0/0/2")]
    # Disabled beside a working protocol is a protocol status, not a warning.
    assert r.protocol_status == {"LLDP disabled": ["R1"]}
    assert r.protocol_warnings == []


def test_i02_cdp_disabled_lldp_links_retained(lab_root, monkeypatch):
    r = _run(lab_root, monkeypatch, {"R1": (lldp_table(R1R2), CDP_OFF), "R2": (LLDP_EMPTY, CDP_EMPTY)})
    assert _pairs(r) == [("R1", "Gi0/0/0/2", "R2", "Gi0/0/0/2")]
    assert r.protocol_status == {"CDP disabled": ["R1"]}
    assert r.protocol_warnings == []


def test_i03_both_disabled_is_fatal_no_usable_protocol(lab_root, monkeypatch):
    # IC-4: a device with no usable discovery protocol fails Discovery
    # (previously: success with a warning and zero links).
    with pytest.raises(discovery.DiscoveryError, match=r"Device 'R1': no usable neighbor discovery protocol \(LLDP disabled, CDP disabled\)"):
        _run(lab_root, monkeypatch, {"R1": (LLDP_OFF, CDP_OFF), "R2": (LLDP_EMPTY, CDP_EMPTY)})


def test_i04_disabled_device_is_not_a_contradiction_and_one_way_policy_kept(lab_root, monkeypatch):
    """R1 disabled, R2 observes R1: the existing one-sided-link policy
    applies (link kept, no conflict); disabled is not negative evidence."""
    r = _run(
        lab_root,
        monkeypatch,
        {"R1": (LLDP_OFF, CDP_EMPTY), "R2": (lldp_table(("HOST-R1", "Gi0/0/0/5", "Gi0/0/0/9")), CDP_EMPTY)},
    )
    assert r.conflicts == []
    assert _pairs(r) == [("R2", "Gi0/0/0/5", "R1", "Gi0/0/0/9")]


def test_i05_i12_other_devices_continue_and_no_inference(lab_root, monkeypatch):
    r = _run(
        lab_root,
        monkeypatch,
        {
            "R1": (LLDP_OFF, CDP_EMPTY),
            "R2": (lldp_table(("HOST-R3", "Gi0/0/0/2", "Gi0/0/0/2")), CDP_OFF),
            "R3": (LLDP_OFF, cdp_table(("HOST-R2", "Gi0/0/0/2", "Gi0/0/0/2"))),
            "R4": (LLDP_OFF, CDP_EMPTY),
        },
    )
    assert _pairs(r) == [("R2", "Gi0/0/0/2", "R3", "Gi0/0/0/2")]
    assert r.conflicts == []
    assert r.connected_count == 4


def test_i06_all_disabled_fails_discovery_and_leaves_candidate_untouched(lab_root, monkeypatch, capsys):
    _targets(lab_root, ["R1", "R2"])
    monkeypatch.setattr(
        discovery,
        "_bootstrap_collect",
        lambda d, c: {
            "hostname": f"HOST-{d}",
            "show_version": "x",
            "show_lldp_neighbors": LLDP_OFF,
            "show_cdp_neighbors": CDP_OFF,
        },
    )
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    capsys.readouterr()
    assert climain.execute_command_line(session, "discover topology") is False
    out = capsys.readouterr().out
    assert "no usable neighbor discovery protocol" in out
    assert "% Topology candidate not updated." in out
    assert session.definition_kind is None and session.definition_candidate is None
    assert session.mode == "global"
    assert SENTINEL not in out and "Local Intf" not in out
    assert not lab.topology_exists("sample_lab_new", lab_root)


def test_i07_cdp_command_actually_runs_after_lldp_disabled(monkeypatch):
    sent = []
    monkeypatch.setattr(discovery, "_login", lambda d, c: "HOST-R1")
    monkeypatch.setattr(discovery, "_disable_terminal_paging", lambda d, p: None)
    outputs = {
        "show version": "ver",
        "show lldp neighbors": LLDP_OFF,
        "show cdp neighbors": CDP_OFF,
    }

    def run(device_id, cmd, prompt_re=None, timeout=None):
        sent.append(cmd)
        return outputs[cmd]

    monkeypatch.setattr(discovery, "_run_command", run)
    info = discovery._bootstrap_collect("R1", {})
    # L2 mode: no L3-only command is ever sent.
    assert sent == ["show version", "show lldp neighbors", "show cdp neighbors"]
    assert "show_ipv4_interface_brief" not in info
    assert info["show_cdp_neighbors"] == CDP_OFF
    assert all(not c.startswith(("configure", "lldp", "cdp", "commit")) for c in sent)  # read-only


@pytest.mark.parametrize(
    "lldp,cdp",
    [
        ("% Invalid input detected at '^' marker.\n", CDP_OFF),  # I08 LLDP error + CDP disabled
        (LLDP_OFF, "% LLDP is not enabled\n"),  # CDP command returned wrong protocol's message
        ("% CDP is not enabled\n", CDP_OFF),
        (LLDP_OFF + LLDP_HDR + "\n", CDP_OFF),
    ],
)
def test_i08_i09_errors_are_not_masked_by_the_other_protocol(lab_root, monkeypatch, lldp, cdp):
    with pytest.raises(discovery.DiscoveryError):
        _run(lab_root, monkeypatch, {"R1": (lldp, cdp), "R2": (LLDP_EMPTY, CDP_EMPTY)})


def test_i10_conflict_detection_preserved(lab_root, monkeypatch):
    # IC-6: a reconciliation conflict is fatal (previously reported and skipped).
    with pytest.raises(discovery.DiscoveryError, match="reconciliation conflict: R1 Gi0/0/0/2: conflicting"):
        _run(
            lab_root,
            monkeypatch,
            {
                "R1": (
                    lldp_table(("HOST-R2", "Gi0/0/0/2", "Gi0/0/0/2")),
                    cdp_table(("HOST-R3", "Gi0/0/0/2", "Gi0/0/0/2")),
                ),
                "R2": (LLDP_EMPTY, CDP_EMPTY),
                "R3": (LLDP_EMPTY, CDP_EMPTY),
            },
        )


def test_i11_duplicate_lldp_cdp_dedup(lab_root, monkeypatch):
    r = _run(lab_root, monkeypatch, {"R1": (lldp_table(R1R2), cdp_table(R1R2)), "R2": (LLDP_EMPTY, CDP_EMPTY)})
    assert len(r.managed_links) == 1 and r.protocol_warnings == []


def test_i13_warnings_deterministic_and_unique(lab_root, monkeypatch):
    outputs = {f"R{i}": (LLDP_OFF, CDP_EMPTY) for i in range(1, 9)}
    results = [_run(lab_root, monkeypatch, outputs) for _ in range(5)]
    runs = [(r.protocol_status, r.protocol_warnings) for r in results]
    assert all(run == runs[0] for run in runs)
    status, warnings = runs[0]
    assert status == {"LLDP disabled": [f"R{i}" for i in range(1, 9)]}  # target order, unique
    assert len(warnings) == 1 and "No physical links were discovered" in warnings[0]


def test_i14_i15_cleanup_on_success_and_failure(lab_root, monkeypatch, _env):
    _run(lab_root, monkeypatch, {"R1": (LLDP_OFF, CDP_EMPTY), "R2": (LLDP_EMPTY, CDP_OFF)})
    assert sorted(_env) == ["R1", "R2"]
    _env.clear()
    with pytest.raises(discovery.DiscoveryError):
        _run(lab_root, monkeypatch, {"R1": ("% CDP is not enabled\n", CDP_EMPTY), "R2": (LLDP_OFF, CDP_OFF)})
    assert sorted(_env) == ["R1", "R2"]


def test_i19_iosxe_lldp_disabled_is_a_protocol_status_and_unrecognized_cdp_is_fatal(lab_root, monkeypatch):
    _targets(lab_root, ["X1"], {"X1": "iosxe"})

    def collect(cdp):
        return lambda d, c: {
            "hostname": "HOST-X1",
            "show_version": "x",
            "show_lldp_neighbors": "% LLDP is not enabled\n",
            "show_cdp_neighbors": cdp,
        }

    monkeypatch.setattr(discovery, "_bootstrap_collect_iosxe", collect(CDP_EMPTY))
    r = discovery.discover_topology(lab_root)
    assert r.managed_links == []
    assert r.protocol_status == {"LLDP disabled": ["X1"]}

    # IC-3: unrecognized CDP output is a parser failure (previously: zero neighbors).
    monkeypatch.setattr(discovery, "_bootstrap_collect_iosxe", collect("garbage\n"))
    with pytest.raises(discovery.DiscoveryError, match="not recognized as valid CDP"):
        discovery.discover_topology(lab_root)

    # Both known "not enabled" texts on IOS XE: no usable protocol.
    monkeypatch.setattr(discovery, "_bootstrap_collect_iosxe", collect("% CDP is not enabled\n"))
    with pytest.raises(discovery.DiscoveryError, match="no usable neighbor discovery protocol"):
        discovery.discover_topology(lab_root)


def test_ios_classic_cdp_only_matrix(lab_root, monkeypatch):
    """Classic IOS has no LLDP (not applicable): CDP success(0) is a
    success without warnings; CDP disabled leaves no usable protocol."""
    _targets(lab_root, ["I1"], {"I1": "ios"})

    def collect(cdp):
        return lambda d, c: {"hostname": "HOST-I1", "show_version": "x", "show_cdp_neighbors": cdp}

    monkeypatch.setattr(discovery, "_bootstrap_collect_ios", collect(CDP_EMPTY))
    assert discovery.discover_topology(lab_root).protocol_warnings == []
    monkeypatch.setattr(discovery, "_bootstrap_collect_ios", collect("% CDP is not enabled\n"))
    with pytest.raises(discovery.DiscoveryError, match=r"LLDP not applicable, CDP disabled"):
        discovery.discover_topology(lab_root)


def test_i16_i18_no_warnings_leaves_summary_unchanged(lab_root, monkeypatch, capsys):
    _run(lab_root, monkeypatch, {"R1": (lldp_table(R1R2), CDP_EMPTY), "R2": (LLDP_EMPTY, CDP_EMPTY)})
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    capsys.readouterr()
    climain.execute_command_line(session, "discover topology")
    out = capsys.readouterr().out
    assert "Neighbor discovery warnings" not in out and "completed with warnings" not in out
    added = [l for l in session.definition_candidate["links"] if "a_interface" in l]
    assert len(added) == 1


def test_i20_warnings_contain_no_credentials_or_raw_output(lab_root, monkeypatch):
    r = _run(lab_root, monkeypatch, {"R1": (LLDP_OFF, CDP_EMPTY), "R2": (LLDP_EMPTY, CDP_OFF)})
    text = "\n".join(r.protocol_warnings + [f"{k} {v}" for k, v in r.protocol_status.items()])
    assert SENTINEL not in text and "JST" not in text and "%" not in text
