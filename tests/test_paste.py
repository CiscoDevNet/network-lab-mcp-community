"""Multi-line configuration paste.

Pasting a multi-line configuration block (e.g. copied from a `show`
output, or typed as a block into a terminal that uses bracketed paste)
previously reached the CLI as a single string containing embedded
newlines, which was parsed as one (invalid) command. `execute_input_block()`
splits that string into physical command lines and runs each one through
the exact same `execute_command_line()` path used for normal manually
typed input -- there is no separate paste grammar or mode model; every
line is parsed by the same cli/grammar.py SSOT and dispatched through the
same HANDLERS table, so a mode-changing line (device/exit/root/end/clear/
commit/...) is reflected in `session` before the next line is evaluated.

Device names R9/R10 are used (instead of R1/R2) for "brand-new device"
scenarios, since the `lab_root` fixture's access-info/topology already
commit R1 and R2 -- R1 is used deliberately where a test wants an
*existing* committed device.

All tests here use the isolated `lab_root` fixture; nothing touches the
real repository's lab/ directory."""

from __future__ import annotations

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain


def _access_info_session(lab_root, name="sample_lab"):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition(name)
    return session


# ---- basic multi-line paste ----


def test_basic_multiline_paste_configures_a_new_device(lab_root):
    session = _access_info_session(lab_root)
    block = (
        "device R9\n"
        " type iosxr\n"
        " address 192.0.2.20\n"
        " transport ssh\n"
        " port 22\n"
        " username example-user\n"
        " password Example!Password123\n"
    )

    climain.execute_input_block(session, block)

    assert session.mode == "access_device"
    assert session.current_device_name == "R9"
    device = session.definition_candidate["devices"]["R9"]
    assert device["type"] == "iosxr"
    assert device["address"] == "192.0.2.20"
    assert device["transport"] == "ssh"
    assert device["port"] == 22
    assert device["username"] == "example-user"
    assert device["password"] == "Example!Password123"


def test_basic_multiline_paste_does_not_commit(lab_root):
    session = _access_info_session(lab_root)
    block = "device R9\n type iosxr\n"
    climain.execute_input_block(session, block)
    on_disk = lab.load_access_info("sample_lab", lab_root)
    assert "R9" not in on_disk.get("devices", {})


# ---- indentation is cosmetic ----


def test_leading_indentation_does_not_affect_parsing(lab_root):
    session = _access_info_session(lab_root)
    block = "    device R9\n      type iosxr\n      address 192.0.2.20\n"
    climain.execute_input_block(session, block)
    assert session.mode == "access_device"
    device = session.definition_candidate["devices"]["R9"]
    assert device["type"] == "iosxr"
    assert device["address"] == "192.0.2.20"


# ---- LF and CRLF ----


def test_crlf_line_endings_are_normalized(lab_root):
    session = _access_info_session(lab_root)
    block = "device R9\r\n type iosxr\r\n address 192.0.2.20\r\n"
    climain.execute_input_block(session, block)
    device = session.definition_candidate["devices"]["R9"]
    assert device["type"] == "iosxr"
    assert device["address"] == "192.0.2.20"


# ---- blank lines are ignored ----


def test_blank_lines_between_commands_are_ignored(lab_root):
    session = _access_info_session(lab_root)
    block = "device R9\n\n type iosxr\n\n address 192.0.2.20\n\n"
    climain.execute_input_block(session, block)
    device = session.definition_candidate["devices"]["R9"]
    assert device["type"] == "iosxr"
    assert device["address"] == "192.0.2.20"


# ---- special-character password ----


def test_password_with_exclamation_mark_is_preserved_literally(lab_root):
    session = _access_info_session(lab_root)
    block = "device R9\n type iosxr\n password Example!Password123\n"
    climain.execute_input_block(session, block)
    assert session.definition_candidate["devices"]["R9"]["password"] == "Example!Password123"


# ---- sequential mode changes within one paste ----


def test_sequential_devices_in_one_paste(lab_root):
    session = _access_info_session(lab_root)
    block = "device R9\n type iosxr\n exit\ndevice R10\n type iosxr\n"
    climain.execute_input_block(session, block)

    assert session.mode == "access_device"
    assert session.current_device_name == "R10"
    devices = session.definition_candidate["devices"]
    assert devices["R9"]["type"] == "iosxr"
    assert devices["R10"]["type"] == "iosxr"


# ---- root inside a paste ----


def test_root_inside_paste_returns_to_global_mode(lab_root):
    session = _access_info_session(lab_root)
    session.enter_access_info_device("R1")
    block = "type iosxr\n root\n"
    climain.execute_input_block(session, block)
    assert session.mode == "global"
    # root preserves candidate state -- it must not have cleared anything.
    assert session.definition_kind == "access_info"


# ---- end inside a paste ----


def test_end_inside_paste_returns_to_exec_when_clean(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    block = "end\n"
    climain.execute_input_block(session, block)
    assert session.mode == "exec"


def test_invalid_line_after_end_fails_at_that_line_and_stops(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    # Proves re-evaluation happens in the mode `end` actually produced:
    # the second line is invalid specifically because it now runs in EXEC.
    block = "end\nnot-a-real-command\n"
    climain.execute_input_block(session, block)
    assert session.mode == "exec"


# ---- clear inside a paste (mandatory) ----


def test_clear_inside_paste_falls_back_to_the_real_clear_handlers_parent(lab_root):
    session = _access_info_session(lab_root)
    block = "device R9\n type iosxr\n clear\ndevice R10\n type iosxr\n"

    climain.execute_input_block(session, block)

    # R9 was only ever candidate state and must be gone after `clear`.
    assert "R9" not in (session.definition_candidate or {}).get("devices", {})
    # R10 was created *after* clear, in whatever mode clear's own handler
    # left the session in -- the paste loop must not have retained a stale
    # "still in R9's device mode" assumption.
    assert session.mode == "access_device"
    assert session.current_device_name == "R10"
    assert session.definition_candidate["devices"]["R10"]["type"] == "iosxr"


def test_clear_inside_paste_with_an_existing_committed_device(lab_root):
    session = _access_info_session(lab_root)
    session.enter_access_info_device("R1")
    block = "address 192.0.2.99\n clear\n"
    climain.execute_input_block(session, block)
    # Clearing while inside an already-committed object's mode keeps that
    # mode (per existing clear semantics) rather than falling back further.
    assert session.mode == "access_device"
    assert session.current_device_name == "R1"
    assert session.definition_candidate["devices"]["R1"]["address"] != "192.0.2.99"


# ---- fail-fast, no rollback ----


def test_fail_fast_stops_at_first_invalid_line_keeping_earlier_changes(lab_root):
    session = _access_info_session(lab_root)
    block = "device R9\n type iosxr\n address 192.0.2.20\n invalid-command foo\n port 22\n"

    climain.execute_input_block(session, block)

    device = session.definition_candidate["devices"]["R9"]
    assert device["type"] == "iosxr"
    assert device["address"] == "192.0.2.20"
    assert "port" not in device  # the line after the error never executed
    assert session.mode == "access_device"  # still where the error left us


def test_interactive_clear_still_works_after_a_failed_paste(lab_root):
    session = _access_info_session(lab_root)
    block = "device R9\n type iosxr\n invalid-command foo\n"
    climain.execute_input_block(session, block)
    assert session.definition_candidate["devices"]["R9"]["type"] == "iosxr"

    # A normal, single, manually-typed `clear` afterwards must behave
    # exactly as it always has -- no special paste-recovery state.
    climain.execute_command_line(session, "clear")
    assert "R9" not in (session.definition_candidate or {}).get("devices", {})


# ---- explicit commit inside a paste ----


def test_explicit_commit_inside_paste_persists_and_stays_in_mode(lab_root):
    session = _access_info_session(lab_root)
    block = "device R9\n type iosxr\n commit\n address 192.0.2.20\n"

    climain.execute_input_block(session, block)

    assert session.mode == "access_device"
    on_disk = lab.load_access_info("sample_lab", lab_root)
    assert on_disk["devices"]["R9"]["type"] == "iosxr"
    assert "address" not in on_disk["devices"]["R9"]  # committed before this line ran
    assert session.definition_candidate["devices"]["R9"]["address"] == "192.0.2.20"  # uncommitted


# ---- jump-host paste ----


def test_jump_host_paste(lab_root):
    session = _access_info_session(lab_root)
    block = (
        "jump-host jump2\n"
        " type host\n"
        " address 192.0.2.30\n"
        " transport ssh\n"
        " port 22\n"
        " username example-user\n"
        " password Example!Jump123\n"
    )
    climain.execute_input_block(session, block)

    assert session.mode == "access_jump_host"
    assert session.current_jump_host_name == "jump2"
    jump_host = session.definition_candidate["jump_hosts"]["jump2"]
    assert jump_host["type"] == "host"
    assert jump_host["address"] == "192.0.2.30"
    assert jump_host["password"] == "Example!Jump123"


# ---- topology-device paste (mechanism must be generic, not access-info-only) ----


def test_topology_device_paste(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))

    climain.execute_input_block(session, "device R9\n type iosxr\n")

    assert session.mode == "device"
    assert session.current_device_name == "R9"
    assert session.definition_candidate["devices"]["R9"]["type"] == "iosxr"


# ---- standalone "!" separators (as produced by show-configuration output) ----
#
# access-info's three modes (access_info/access_device/access_jump_host) and
# topology's two (device/topology) give a pasted standalone "!" a narrow,
# explicit meaning (exactly one level up, mirroring the rendered blocks' own
# closing convention -- see _apply_structural_bang() in main.py).
# This test previously asserted the OLD, now-fixed behavior ("!" always
# ignored, which meant a rendered access-info block
# could not be pasted back). It has been rewritten to assert the new,
# correct behavior instead of being left encoding a bug.
# Full dedicated coverage (both access-info paste round-trip and the
# unrelated-mode/global/EXEC no-op safety) lives in
# test_structural_bang_paste.py.


def test_standalone_bang_closes_access_device_block_then_noop_at_global(lab_root):
    session = _access_info_session(lab_root)
    block = "device R9\n type iosxr\n!\n exit\n!\n"
    climain.execute_input_block(session, block)
    # First "!" (still in access_device mode) closes R9's block back to
    # access_info; explicit "exit" then goes to global; the trailing "!"
    # at global is a safe no-op (see test_structural_bang_paste.py).
    assert session.mode == "global"
    assert session.definition_candidate["devices"]["R9"]["type"] == "iosxr"


def test_bang_inside_a_password_value_is_not_treated_as_a_separator(lab_root):
    session = _access_info_session(lab_root)
    block = "device R9\n password Example!Password123\n!\n"
    climain.execute_input_block(session, block)
    assert session.definition_candidate["devices"]["R9"]["password"] == "Example!Password123"


def test_standalone_bang_in_topology_paste_closes_device_then_topology(lab_root):
    """The structural "!" meaning covers access-info's three modes and the
    topology definition's two (device -> topology -> global); scenario and
    reference have no CLI body grammar and are unchanged."""
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.enter_topology_device("R9")
    block = "type iosxr\n!\n"
    climain.execute_input_block(session, block)
    # topology device/definition modes close with "!" like access-info (so a rendered
    # topology delta returns to global); a "!" after content climbs one level
    assert session.mode == "topology"
    climain.execute_input_block(session, "!\n!")
    assert session.mode == "global"
    assert session.definition_candidate["devices"]["R9"]["type"] == "iosxr"


# ---- mandatory: object-deletion combined with clear inside a paste ----


def test_device_create_delete_clear_create_paste_sequence(lab_root):
    """R5 is created mid-paste, removed via `no device R5` (resolving
    against that same uncommitted candidate), then `clear` restores the
    committed candidate -- the paste loop must continue afterward with a
    freshly re-read authoritative mode, and R6 must land in access_device
    mode for the *new* object with no stale R5 state surviving."""
    session = _access_info_session(lab_root)
    block = (
        "device R5\n"
        " type iosxr\n"
        " exit\n"
        "no device R5\n"
        "clear\n"
        "device R6\n"
        " type iosxr\n"
        " exit\n"
    )
    climain.execute_input_block(session, block)

    assert session.mode == "access_info"
    devices = session.definition_candidate["devices"]
    assert "R5" not in devices
    assert set(devices) == {"R1", "R2", "R6"}
    assert devices["R6"]["type"] == "iosxr"
    on_disk = lab.load_access_info("sample_lab", lab_root)
    assert "R5" not in on_disk.get("devices", {})
    assert "R6" not in on_disk.get("devices", {})  # never committed


def test_jump_host_create_delete_clear_create_paste_sequence(lab_root):
    """Same interaction as above, for jump hosts, using the real jump-host
    schema (type/address/transport, no `jump-host` field of its own)."""
    session = _access_info_session(lab_root)
    block = (
        "jump-host jump_temp\n"
        " type host\n"
        " address 192.0.2.200\n"
        " transport ssh\n"
        " exit\n"
        "no jump-host jump_temp\n"
        "clear\n"
        "jump-host jump_after_clear\n"
        " type host\n"
        " exit\n"
    )
    climain.execute_input_block(session, block)

    assert session.mode == "access_info"
    jump_hosts = session.definition_candidate["jump_hosts"]
    assert "jump_temp" not in jump_hosts
    assert set(jump_hosts) == {"jump1", "jump_after_clear"}
    assert jump_hosts["jump_after_clear"]["type"] == "host"
    on_disk = lab.load_access_info("sample_lab", lab_root)
    assert "jump_temp" not in on_disk.get("jump_hosts", {})
    assert "jump_after_clear" not in on_disk.get("jump_hosts", {})  # never committed


# ---- single-line input is untouched ----


def test_single_line_input_is_not_routed_through_the_paste_splitter(lab_root):
    session = _access_info_session(lab_root)
    # A single-line device name containing no newline must reach the
    # grammar exactly as before -- no lstrip, no "!" filtering applied to
    # ordinary typed input.
    ok = climain.execute_command_line(session, "device R9")
    assert ok
    assert session.mode == "access_device"


# ---- history safety ----


def test_password_line_inside_a_pasted_block_never_enters_history():
    history = climain.MaskingHistory()
    block = "device R9\n type iosxr\n password Example!Password123\n address 192.0.2.20\n"
    history.append_string(block)
    stored = "\n".join(history.get_strings())
    assert "Example!Password123" not in stored
    assert "device R9" in stored
    assert "type iosxr" in stored
    assert "address 192.0.2.20" in stored


def test_single_line_password_command_still_excluded_from_history():
    history = climain.MaskingHistory()
    history.append_string("password Example!Password123")
    assert history.get_strings() == []


def test_single_line_non_password_command_still_recorded_in_history():
    history = climain.MaskingHistory()
    history.append_string("device R9")
    assert history.get_strings() == ["device R9"]
