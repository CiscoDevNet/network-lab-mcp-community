# SPDX-License-Identifier: Apache-2.0
"""access-info configuration render/paste round-trip.

Rendered access-info configuration (render_access_info_block()) closes
every jump-host/device block, and the whole access-info block itself,
with a standalone "!" line -- exactly the convention IOS XR-style show
output uses. Previously, a pasted standalone "!" was unconditionally
dropped (see _split_pasted_command_lines()'s old docstring), so pasting
that rendered output back failed: after "device R1"'s block, the CLI was
still inside R1's own submode when the next "device R2" line arrived, and
"device" is not a valid command there.

_apply_structural_bang() (cli/main.py) gives a pasted standalone "!" a
narrow, explicit meaning bounded to exactly five definition modes (plus the
conditional `running` section modes):

    access_device      -> exactly one level up, to access_info
    access_jump_host    -> exactly one level up, to access_info
    access_info          -> exactly one level up, to global
    device (topology)    -> exactly one level up, to topology
    topology             -> exactly one level up, to global

Everywhere else -- including global configuration mode and EXEC -- it is
an explicit, deliberate no-op: never exit/end/quit, never a candidate
change, never a CLI termination. This is bounded to multi-line paste only
(execute_input_block()'s per-physical-line loop); a single manually typed
"!" is untouched (still reaches execute_command_line()/grammar.parse()
exactly as before). Scenario/reference modes are unaffected; topology
now closes with "!" like access-info -- see
test_standalone_bang_in_topology_paste_closes_device_then_topology in
test_paste.py and the stray-"!" tests in test_show_configuration_replay.py.

All tests here use the isolated `lab_root` fixture; nothing touches the
real repository's lab/ directory."""

from __future__ import annotations

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain


def _rendered_committed_access_info(lab_root, name="sample_lab"):
    data = lab.load_access_info(name, lab_root)
    return "\n".join(climain.render_access_info_block(data))


# ==========================================================================
# Mandatory acceptance: full rendered access-info block, pasted from
# global configuration mode
# ==========================================================================


def test_full_rendered_access_info_block_pastes_from_global_config(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    assert session.mode == "global"

    rendered = _rendered_committed_access_info(lab_root, "sample_lab")
    # Sanity: the real renderer actually produced sibling device blocks
    # separated by standalone "!", the exact shape that used to break.
    assert rendered.count("\n !\n") >= 2 or " !\n device " in rendered

    climain.execute_input_block(session, rendered)

    assert session.mode == "global"  # final outer "!" returns here
    assert session.definition_kind == "access_info"
    assert session.definition_name == "sample_lab"
    assert set(session.definition_candidate["devices"]) == {"R1", "R2"}
    assert set(session.definition_candidate["jump_hosts"]) == {"jump1"}
    # Unchanged pasted configuration produces no net diff vs. committed.
    assert climain.render_configuration_candidate(session) == ""


def test_extra_trailing_bang_after_full_block_is_a_safe_noop_at_global(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    rendered = _rendered_committed_access_info(lab_root, "sample_lab")
    climain.execute_input_block(session, rendered + "\n!\n!\n!\n")  # imperfect extra copy/paste
    assert session.mode == "global"
    assert session.definition_kind == "access_info"
    assert climain.render_configuration_candidate(session) == ""


# ==========================================================================
# Mandatory acceptance: access-info body paste from access-info definition
# mode -- the exact style the user encountered
# ==========================================================================


def test_access_info_body_paste_from_access_info_definition_mode(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    assert session.mode == "access_info"

    block = (
        "device R1\n"
        " type iosxr\n"
        " address 192.0.2.1\n"
        "!\n"
        "device R2\n"
        " type iosxr\n"
        " address 192.0.2.2\n"
        "!\n"
        "device R3\n"
        " type iosxr\n"
        " address 192.0.2.3\n"
        "!\n"
        "device R4\n"
        " type iosxr\n"
        " address 192.0.2.4\n"
        "!\n"
        "!\n"
    )
    climain.execute_input_block(session, block)

    # No "% Unknown command: \"device\"" anywhere -- every sibling block
    # was accepted, proven by all four devices actually landing in the
    # candidate with the values from their own block (not a stale one).
    devices = session.definition_candidate["devices"]
    assert set(devices) == {"R1", "R2", "R3", "R4"}
    assert devices["R1"]["address"] == "192.0.2.1"
    assert devices["R2"]["address"] == "192.0.2.2"
    assert devices["R3"]["address"] == "192.0.2.3"
    assert devices["R4"]["address"] == "192.0.2.4"
    # The final outer "!" returned to global configuration mode.
    assert session.mode == "global"


# ==========================================================================
# Mandatory acceptance: jump-host paste round-trip
# ==========================================================================


def test_jump_host_paste_round_trip_with_device_reference(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")

    block = (
        "jump-host jump2\n"
        " type host\n"
        " address 192.0.2.30\n"
        " transport ssh\n"
        "!\n"
        "device R1\n"
        " jump-host jump2\n"
        " transport ssh\n"
        "!\n"
        "!\n"
    )
    climain.execute_input_block(session, block)

    assert session.mode == "global"
    assert session.definition_candidate["jump_hosts"]["jump2"]["address"] == "192.0.2.30"
    assert session.definition_candidate["devices"]["R1"]["jump_host"] == "jump2"
    # Sibling jump host and device untouched.
    assert "jump1" in session.definition_candidate["jump_hosts"]
    assert "R2" in session.definition_candidate["devices"]


# ==========================================================================
# Extra trailing "!" safety
# ==========================================================================


def test_exact_spec_example_extra_trailing_bang_from_global_config(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    block = "access-info test_lab\n device R1\n  type iosxr\n !\n!\n!\n"
    climain.execute_input_block(session, block)
    # 1) access-info test_lab entered; 2) R1 entered; 3) first "!" returns
    # to access-info parent; 4) second "!" returns to global config;
    # 5) third extra "!" is a no-op; 6)-8) stays at config)#, never EXEC,
    # never terminated; 9) candidate remains valid.
    assert session.mode == "global"
    assert session.definition_kind == "access_info"
    assert session.definition_name == "test_lab"
    assert session.definition_candidate["devices"]["R1"]["type"] == "iosxr"


# ==========================================================================
# Safe no-op at global configuration and EXEC
# ==========================================================================


def test_standalone_bang_at_global_config_is_a_noop(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    assert session.mode == "global"
    climain.execute_input_block(session, "!\n")
    assert session.mode == "global"
    assert session.overall_dirty() is False


def test_standalone_bang_at_exec_is_a_noop_and_never_exits(lab_root):
    session = cfgmod.CliSession(lab_root)
    assert session.mode == "exec"
    # Must not raise _ExitCli (which a real "exit"/"quit" at EXEC does).
    climain.execute_input_block(session, "!\n")
    assert session.mode == "exec"


def test_standalone_bang_at_exec_alongside_other_lines_never_terminates(lab_root):
    session = cfgmod.CliSession(lab_root)
    climain.execute_input_block(session, "!\nconfigure\n!\n")
    # The stray leading/trailing "!" never triggered exit/quit; "configure"
    # in between executed normally.
    assert session.mode == "global"


# ==========================================================================
# Explicit `exit` sibling paste syntax still works alongside "!"
# ==========================================================================


def test_explicit_exit_and_structural_bang_are_both_valid_between_siblings(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    block = (
        "device R5\n"
        " type iosxr\n"
        "exit\n"  # explicit navigation style
        "device R6\n"
        " type iosxr\n"
        "!\n"  # renderer style
    )
    climain.execute_input_block(session, block)
    assert session.mode == "access_info"
    devices = session.definition_candidate["devices"]
    assert devices["R5"]["type"] == "iosxr"
    assert devices["R6"]["type"] == "iosxr"


# ==========================================================================
# Primary round-trip acceptance test
# ==========================================================================


def test_committed_access_info_render_paste_commit_round_trip(lab_root):
    # 1) Committed access-info already contains jump-host jump1, R1, R2
    #    (the lab_root fixture's sample_lab). 2) Render it with the real
    #    renderer.
    rendered = _rendered_committed_access_info(lab_root, "sample_lab")

    # 3) Feed it through the real multi-line paste path from the
    #    appropriate parent mode (global configuration).
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    climain.execute_input_block(session, rendered)

    # 4) No parser/mode errors: candidate matches committed exactly.
    assert session.mode == "global"
    committed = lab.load_access_info("sample_lab", lab_root)
    assert session.definition_candidate == committed

    # 6) show configuration has no net diff.
    assert climain.render_configuration_candidate(session) == ""

    # 7) Commit.
    changed = session.commit()
    assert changed is False  # nothing was actually dirty -- a true no-op

    # 8) Committed definition remains semantically equivalent.
    assert lab.load_access_info("sample_lab", lab_root) == committed

    # 9) An extra trailing standalone "!" after the rendered block is a
    #    safe no-op at global configuration level.
    climain.execute_input_block(session, "!\n")
    assert session.mode == "global"


# ==========================================================================
# Mutation round-trip acceptance
# ==========================================================================


def test_add_device_then_render_paste_again_preserves_all_devices(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.enter_access_info_device("R4")
    session.set_device_field("type", "iosxr")
    session.set_device_field("address", "192.0.2.4")
    assert set(session.definition_candidate["devices"]) == {"R1", "R2", "R4"}
    session.commit()
    persisted = lab.load_access_info("sample_lab", lab_root)
    assert set(persisted["devices"]) == {"R1", "R2", "R4"}

    # Render the newly-committed running-config and paste it again.
    rendered_again = "\n".join(climain.render_access_info_block(persisted))
    session2 = cfgmod.CliSession(lab_root)
    session2.enter_configure()
    climain.execute_input_block(session2, rendered_again)

    assert session2.mode == "global"
    assert session2.definition_candidate == persisted  # no semantic change, no sibling loss
    assert climain.render_configuration_candidate(session2) == ""  # no candidate diff
