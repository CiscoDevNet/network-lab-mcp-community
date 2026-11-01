# SPDX-License-Identifier: Apache-2.0
"""Definition names are case-sensitive and exact, on every filesystem.

Two layers are kept apart on purpose:

1. Simulated case folding (`fold_case` fixture). Patches only the *name
   resolution* boundary (`os.path.lexists`) so a case-sensitive host (Linux
   ext4) behaves like a case-insensitive one for lookups. It proves the
   name-resolution logic; it cannot prove real on-disk side effects.
2. Real-filesystem tests, which assert the right behavior for whatever the
   pytest temp directory actually is (case-folding on default macOS APFS,
   case-preserving-and-sensitive on typical Linux). They are not skipped.

Nothing here writes into the repository's own lab/ directory.
"""

from __future__ import annotations

import errno
import os
import unicodedata
from pathlib import Path

import pytest
import yaml

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import editor

KINDS = [
    # kind, subdirectory, stored name in the lab_root fixture, load, write, exists, delete, is_deletable
    ("topology", "topologies", "sample_lab", lab.load_topology, lab.write_topology, lab.topology_exists,
     lab.delete_topology, lab.topology_is_deletable),
    ("access_info", "access-info", "sample_lab", lab.load_access_info, lab.write_access_info,
     lab.access_info_exists, lab.delete_access_info, lab.access_info_is_deletable),
    ("scenario", "scenarios", "sample", lab.load_scenario, lab.write_scenario, lab.scenario_exists,
     lab.delete_scenario, lab.scenario_is_deletable),
    ("reference", "references", "sample", lab.load_reference, lab.write_reference, lab.reference_exists,
     lab.delete_reference, lab.reference_is_deletable),
]
KIND_IDS = [k[0] for k in KINDS]


@pytest.fixture()
def fold_case(monkeypatch):
    """Make path *lookups* case-insensitive, like default macOS APFS, while
    directory listings (os.scandir) keep reporting the real stored names."""
    real_lexists = os.path.lexists
    real_samefile = os.path.samefile

    def _match(path):
        path = Path(path)
        if not path.parent.is_dir():
            return None
        for entry in os.listdir(path.parent):
            if entry.casefold() == path.name.casefold():
                return path.parent / entry
        return None

    def lexists(path):
        return real_lexists(path) or _match(path) is not None

    def samefile(a, b):
        resolved = [_match(p) or Path(p) for p in (a, b)]
        return real_samefile(*resolved)

    monkeypatch.setattr(os.path, "lexists", lexists)
    monkeypatch.setattr(os.path, "samefile", samefile)


def _alias(stored: str) -> str:
    return stored.upper()


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------- name resolution (simulated)


@pytest.mark.parametrize("kind,sub,stored,load,write,exists,delete,deletable", KINDS, ids=KIND_IDS)
def test_exact_name_resolves_and_alias_does_not(fold_case, lab_root, kind, sub, stored, load, write, exists, delete, deletable):
    assert lab.definition_file_state(kind, stored, lab_root) == "exact"
    assert exists(stored, lab_root) is True
    assert load(stored, lab_root)["name"] == stored

    alias = _alias(stored)
    assert lab.definition_file_state(kind, alias, lab_root) == "alias"
    assert exists(alias, lab_root) is False
    with pytest.raises(lab.LabConfigError, match=f"resolves to the stored definition '{stored}'"):
        load(alias, lab_root)


@pytest.mark.parametrize("kind,sub,stored,load,write,exists,delete,deletable", KINDS, ids=KIND_IDS)
def test_absent_and_invalid_names(lab_root, kind, sub, stored, load, write, exists, delete, deletable):
    assert lab.definition_file_state(kind, "nope", lab_root) == "absent"
    for bad in ("", ".", "..", "../x", "a/b", "a\0b"):
        assert lab.definition_file_state(kind, bad, lab_root) == "invalid"
        assert exists(bad, lab_root) is False
        with pytest.raises(lab.LabConfigError):
            load(bad, lab_root)


@pytest.mark.parametrize("kind,sub,stored,load,write,exists,delete,deletable", KINDS, ids=KIND_IDS)
def test_listing_reports_real_names_only(fold_case, lab_root, kind, sub, stored, load, write, exists, delete, deletable):
    names = lab._list_yaml_stems(lab_root / sub)
    assert stored in names
    assert _alias(stored) not in names


# ---------------------------------------------------------------- save (simulated + real)


def _valid_data(kind: str, name: str) -> dict:
    return cfgmod._DEFINITION_LOADERS[kind][2](name)


@pytest.mark.parametrize("kind,sub,stored,load,write,exists,delete,deletable", KINDS, ids=KIND_IDS)
def test_save_through_alias_is_refused_and_changes_nothing(fold_case, lab_root, kind, sub, stored, load, write, exists, delete, deletable):
    before = _snapshot(lab_root)
    for create_only in (False, True):
        with pytest.raises(lab.LabConfigError, match="case-sensitive"):
            write(_alias(stored), _valid_data(kind, _alias(stored)), lab_root, create_only=create_only)
    assert _snapshot(lab_root) == before  # no overwrite, no stray temp file


@pytest.mark.parametrize("kind,sub,stored,load,write,exists,delete,deletable", KINDS, ids=KIND_IDS)
def test_create_only_never_overwrites_existing(lab_root, kind, sub, stored, load, write, exists, delete, deletable):
    before = _snapshot(lab_root)
    with pytest.raises(lab.LabConfigError, match="already exists"):
        write(stored, _valid_data(kind, stored), lab_root, create_only=True)
    assert _snapshot(lab_root) == before


@pytest.mark.parametrize("kind,sub,stored,load,write,exists,delete,deletable", KINDS, ids=KIND_IDS)
def test_create_then_update(lab_root, kind, sub, stored, load, write, exists, delete, deletable):
    data = _valid_data(kind, "brand_new")
    write("brand_new", data, lab_root, create_only=True)
    assert load("brand_new", lab_root) == data
    assert [p.name for p in (lab_root / sub).iterdir() if p.name.endswith(".tmp")] == []
    data["description"] = "updated"
    write("brand_new", data, lab_root)  # exact existing file: atomic replace
    assert load("brand_new", lab_root)["description"] == "updated"
    assert [p.name for p in (lab_root / sub).iterdir() if p.name.endswith(".tmp")] == []


def test_exclusive_create_loses_race_without_overwriting(lab_root, monkeypatch):
    """The target appears between the state check and publication (a
    concurrent writer). os.link() must refuse rather than replace it."""
    target = lab_root / "scenarios" / "raced.yaml"
    real_link = os.link

    def racing_link(src, dst, *a, **k):
        target.write_text("name: winner\n", encoding="utf-8")
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(lab.LabConfigError, match="already exists"):
        lab.write_scenario("raced", {"name": "loser"}, lab_root, create_only=True)
    assert target.read_text(encoding="utf-8") == "name: winner\n"
    assert [p.name for p in (lab_root / "scenarios").iterdir() if p.name.endswith(".tmp")] == []


def test_exclusive_create_failure_leaves_no_target_or_temp(lab_root, monkeypatch):
    def failing_dump(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(lab.yaml, "safe_dump", failing_dump)
    before = _snapshot(lab_root)
    with pytest.raises(OSError):
        lab.write_reference("will_fail", {"name": "will_fail"}, lab_root, create_only=True)
    assert _snapshot(lab_root) == before
    assert not (lab_root / "references" / "will_fail.yaml").exists()
    assert [p.name for p in (lab_root / "references").iterdir() if p.name.endswith(".tmp")] == []


# ---------------------------------------------------------------- delete (simulated)


@pytest.mark.parametrize("kind,sub,stored,load,write,exists,delete,deletable", KINDS, ids=KIND_IDS)
def test_delete_through_alias_is_refused(fold_case, lab_root, kind, sub, stored, load, write, exists, delete, deletable):
    before = _snapshot(lab_root)
    assert deletable(_alias(stored), lab_root) is False
    with pytest.raises(lab.LabConfigError):
        delete(_alias(stored), lab_root)
    assert _snapshot(lab_root) == before

    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    with pytest.raises(cfgmod.ConfigError, match="does not exist"):
        session.remove_definition(kind, _alias(stored))
    assert session.definition_kind is None and session.definition_candidate is None
    assert _snapshot(lab_root) == before


# ---------------------------------------------------------------- CLI candidate protection


def _state(session):
    return (
        session.mode, session.definition_kind, session.definition_name, session.definition_candidate,
        session.definition_original, session._mutation_rev, session.overall_dirty(),
        dict(session.settings_candidate),
    )


@pytest.mark.parametrize("kind,sub,stored,load,write,exists,delete,deletable", KINDS, ids=KIND_IDS)
def test_entering_alias_is_rejected_before_any_state_change(fold_case, lab_root, kind, sub, stored, load, write, exists, delete, deletable):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    before_state = _state(session)
    before_files = _snapshot(lab_root)
    enter = {
        "topology": lambda n: session.apply_topology_definition_plan(cfgmod.TopologyPlan("create_new", n)),
        "access_info": session.enter_access_info_definition,
        "scenario": session.enter_scenario_definition,
        "reference": session.enter_reference_definition,
    }[kind]
    with pytest.raises(cfgmod.ConfigError, match="case-sensitive"):
        enter(_alias(stored))
    assert _state(session) == before_state
    assert _snapshot(lab_root) == before_files
    # the CLI session is still usable afterwards
    enter(stored)
    assert session.definition_name == stored and session.definition_candidate["name"] == stored


def test_topology_plan_rejects_alias_before_confirmation(fold_case, lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    before = _state(session)
    with pytest.raises(cfgmod.ConfigError, match="case-sensitive"):
        session.plan_topology_definition("SAMPLE_LAB")  # a prompt would be offered if this returned a plan
    assert _state(session) == before


def test_topology_case_collision_still_confirmable_on_case_sensitive_fs(lab_root):
    """The existing Linux flow (separate SAMPLE_LAB next to sample_lab) is
    kept wherever the filesystem can really hold both."""
    if (lab_root / "topologies" / "SAMPLE_LAB.yaml").exists():
        # Case-folding host: the pair cannot coexist, so creation is refused
        # up front (the alias-rejection tests cover this in depth).
        session = cfgmod.CliSession(lab_root)
        session.enter_configure()
        with pytest.raises(cfgmod.ConfigError):
            session.plan_topology_definition("SAMPLE_LAB")
        return
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    plan = session.plan_topology_definition("SAMPLE_LAB")
    assert (plan.kind, plan.existing) == ("case_collision", "sample_lab")
    session.apply_topology_definition_plan(cfgmod.TopologyPlan("create_new", "SAMPLE_LAB"))
    session.commit()
    assert lab.load_topology("SAMPLE_LAB", lab_root)["name"] == "SAMPLE_LAB"
    assert lab.load_topology("sample_lab", lab_root)["name"] == "sample_lab"


def test_commit_refuses_alias_and_changes_nothing(fold_case, lab_root):
    """Defense in depth: even if a candidate for an alias name existed, commit
    validates before any write."""
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_scenario_definition("fresh")
    session.definition_name = "SAMPLE"  # bypass the entry guard on purpose
    session.definition_candidate["name"] = "SAMPLE"
    before = _snapshot(lab_root)
    with pytest.raises(cfgmod.CommitValidationError):
        session.commit()
    assert _snapshot(lab_root) == before


def test_commit_refuses_to_overwrite_file_created_after_candidate(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_scenario_definition("late")
    session.definition_candidate["description"] = "mine"
    lab.write_scenario("late", {"name": "late", "description": "theirs"}, lab_root)
    before = _snapshot(lab_root)
    rev = session._mutation_rev
    with pytest.raises(cfgmod.CommitValidationError, match="already exists"):
        session.commit()
    assert _snapshot(lab_root) == before
    assert session._mutation_rev == rev and session.definition_dirty()


# ---------------------------------------------------------------- existing settings with an alias reference


def test_settings_alias_reference_is_not_adopted_but_cli_still_works(fold_case, lab_root):
    settings_path = lab_root / "settings.yaml"
    settings = yaml.safe_load(settings_path.read_text())
    settings.update(active_scenario="SAMPLE", active_references=["SAMPLE"], active_topology="SAMPLE_LAB")
    settings_path.write_text(yaml.safe_dump(settings))
    before = _snapshot(lab_root)

    # MCP side: fails closed with a name-only message, never the alias's content.
    with pytest.raises(lab.LabConfigError, match="case-sensitive") as info:
        lab.load_scenario(settings["active_scenario"], lab_root)
    assert "objectives" not in str(info.value)
    assert lab.scenario_exists("SAMPLE", lab_root) is False
    assert lab.topology_exists("SAMPLE_LAB", lab_root) is False

    # CLI side: configure mode starts, shows the committed settings, and the
    # dangling selections can be corrected to the exact names and committed.
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    assert session.settings_candidate["active_scenario"] == "SAMPLE"
    session.set_scenario("sample")
    session.select_topology("sample_lab")
    session.remove_reference("SAMPLE")
    session.add_reference("sample")
    assert session.commit() is True
    fixed = lab.read_settings(lab_root)
    assert (fixed["active_scenario"], fixed["active_topology"], fixed["active_references"]) == (
        "sample", "sample_lab", ["sample"]
    )
    # nothing but settings.yaml changed
    after = _snapshot(lab_root)
    assert {k for k in after if after[k] != before.get(k)} == {"settings.yaml"}
    assert before == {**after, "settings.yaml": before["settings.yaml"]}


def test_no_automatic_repair_of_alias_settings(fold_case, lab_root):
    settings_path = lab_root / "settings.yaml"
    settings = yaml.safe_load(settings_path.read_text())
    settings["active_scenario"] = "SAMPLE"
    settings_path.write_text(yaml.safe_dump(settings))
    before = _snapshot(lab_root)
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    assert session.commit() is False  # nothing dirty -> nothing written, no auto-fix
    session.reset_to_exec()
    assert _snapshot(lab_root) == before


# ---------------------------------------------------------------- real filesystem (host-dependent, never skipped)


def test_real_fs_case_behavior_matches_spec(lab_root):
    """Whatever this host's filesystem does, the answer for a case-only name
    is alias (folding FS) or absent (case-sensitive FS) -- never exact."""
    folds = (lab_root / "topologies" / "SAMPLE_LAB.yaml").exists()
    state = lab.definition_file_state("topology", "SAMPLE_LAB", lab_root)
    assert state == ("alias" if folds else "absent")
    assert lab.topology_exists("SAMPLE_LAB", lab_root) is False
    if folds:
        with pytest.raises(lab.LabConfigError):
            lab.load_topology("SAMPLE_LAB", lab_root)
        with pytest.raises(lab.LabConfigError):
            lab.write_topology("SAMPLE_LAB", {"name": "SAMPLE_LAB", "devices": {}, "links": []}, lab_root)
        assert lab.load_topology("sample_lab", lab_root)["name"] == "sample_lab"
    else:
        lab.write_topology("SAMPLE_LAB", {"name": "SAMPLE_LAB", "devices": {}, "links": []}, lab_root, create_only=True)
        assert lab.load_topology("SAMPLE_LAB", lab_root)["name"] == "SAMPLE_LAB"
        assert lab.load_topology("sample_lab", lab_root)["name"] == "sample_lab"


def test_unicode_normalization_variants_never_match_exactly(lab_root):
    """Names are compared exactly (no NFC folding). A filesystem that treats
    NFC/NFD as one file (APFS) yields alias; one that does not yields absent."""
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    assert nfc != nfd
    lab.write_scenario(nfc, {"name": "cafe"}, lab_root, create_only=True)
    assert lab.scenario_exists(nfc, lab_root) or lab.scenario_exists(nfd, lab_root)
    # Exactly one of the two spellings is the stored one; the other is never "exact".
    stored = [n for n in lab.list_scenario_names(lab_root) if unicodedata.normalize("NFC", n) == nfc]
    assert len(stored) == 1
    other = nfd if stored[0] == nfc else nfc
    assert lab.definition_file_state("scenario", other, lab_root) in ("alias", "absent")
    assert lab.scenario_exists(other, lab_root) is False
    if lab.definition_file_state("scenario", other, lab_root) == "alias":
        with pytest.raises(lab.LabConfigError):
            lab.write_scenario(other, {"name": "x"}, lab_root, create_only=True)
    else:  # distinct names on this filesystem: a separate definition may be created
        lab.write_scenario(other, {"name": "x"}, lab_root, create_only=True)
        assert lab.scenario_exists(other, lab_root)


# ---------------------------------------------------------------- editor launch errors


@pytest.fixture()
def isolated_editor_env(tmp_path, monkeypatch):
    """PATH and temp dir confined to tmp_path; the caller's real PATH is untouched."""
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    return tmp_path


def _no_temp_left(tmp_path):
    return list((tmp_path / "tmp").iterdir()) == []


def test_editor_enoent(isolated_editor_env, monkeypatch):
    empty = isolated_editor_env / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("EDITOR", "definitely-not-a-real-editor-binary")
    with pytest.raises(editor.EditorError, match="not found"):
        editor.edit_yaml_candidate({"name": "original"})
    assert _no_temp_left(isolated_editor_env)


def test_editor_enotdir_from_regular_file_in_path(isolated_editor_env, monkeypatch):
    """A regular file in a PATH slot makes execvp report ENOTDIR (on Linux
    and macOS alike); that is a failed lookup, not a crash."""
    plain_file = isolated_editor_env / "not-a-dir"
    plain_file.write_text("x")
    empty = isolated_editor_env / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", f"{empty}:{plain_file}")
    monkeypatch.setenv("EDITOR", "definitely-not-a-real-editor-binary")
    with pytest.raises(editor.EditorError, match="not found"):
        editor.edit_yaml_candidate({"name": "original"})
    assert _no_temp_left(isolated_editor_env)


def test_editor_explicit_path_through_a_file_is_not_found(isolated_editor_env, monkeypatch):
    plain_file = isolated_editor_env / "not-a-dir"
    plain_file.write_text("x")
    monkeypatch.setenv("EDITOR", str(plain_file / "ed"))
    with pytest.raises(editor.EditorError, match="not found"):
        editor.edit_yaml_candidate({"name": "original"})


def test_editor_eacces(isolated_editor_env, monkeypatch):
    bindir = isolated_editor_env / "bin"
    bindir.mkdir()
    script = bindir / "noexec-editor"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o644)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("EDITOR", "noexec-editor")
    with pytest.raises(editor.EditorError, match="permission denied"):
        editor.edit_yaml_candidate({"name": "original"})
    assert _no_temp_left(isolated_editor_env)


def test_editor_other_oserror_is_not_hidden(isolated_editor_env, monkeypatch):
    def boom(*a, **k):
        raise OSError(errno.E2BIG, "Argument list too long")

    monkeypatch.setattr(editor.subprocess, "run", boom)
    monkeypatch.setenv("EDITOR", "anything")
    with pytest.raises(OSError) as info:
        editor.edit_yaml_candidate({"name": "original"})
    assert not isinstance(info.value, editor.EditorError)
    assert info.value.errno == errno.E2BIG
    assert _no_temp_left(isolated_editor_env)


# ---------------------------------------------------------------- command-line level


def test_cli_rejects_alias_without_prompt_and_keeps_working(fold_case, lab_root, monkeypatch, capsys):
    from network_lab_mcp.cli import main as climain

    def no_prompt(*a, **k):
        raise AssertionError("an unrealizable creation must not be offered for confirmation")

    monkeypatch.setattr(climain, "_confirm", no_prompt)
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    before_files = _snapshot(lab_root)
    before_state = _state(session)
    for line in ("topology SAMPLE_LAB", "no topology SAMPLE_LAB", "scenario SAMPLE", "no scenario SAMPLE"):
        climain.execute_command_line(session, line)
        assert "%" in capsys.readouterr().out, line
        assert _state(session) == before_state, line
    assert _snapshot(lab_root) == before_files
    climain.execute_command_line(session, "clear")
    climain.execute_command_line(session, "topology sample_lab")
    assert session.definition_name == "sample_lab"


# ---------------------------------------------------------------- permissions and temp-file robustness


@pytest.mark.parametrize("umask", [0o022, 0o077])
def test_created_file_mode_matches_update_path_under_any_umask(tmp_path, umask):
    """Creation must never be more permissive than the existing atomic
    update path (access-info holds credentials): both honor the umask."""
    old = os.umask(umask)
    try:
        lab._atomic_write_yaml(tmp_path / "updated.yaml", {"name": "u"})
        lab._create_yaml_exclusive(tmp_path / "created.yaml", {"name": "c"}, "Scenario", "created")
    finally:
        os.umask(old)
    mode = lambda n: (tmp_path / n).stat().st_mode & 0o777  # noqa: E731
    assert mode("created.yaml") == mode("updated.yaml") == 0o666 & ~umask


def test_stale_temp_leftover_does_not_block_creation(lab_root, monkeypatch):
    """A leftover temp file from a crashed run (even one whose name a PID-
    or counter-based scheme would reuse) must not make creation fail."""
    directory = lab_root / "scenarios"
    stale = [directory / f".fresh.yaml.{i}.tmp" for i in (os.getpid(), 1, 2)]
    for p in stale:
        p.write_text("partial")
    lab.write_scenario("fresh", {"name": "fresh"}, lab_root, create_only=True)
    assert lab.load_scenario("fresh", lab_root) == {"name": "fresh"}
    assert all(p.read_text() == "partial" for p in stale)  # untouched, not adopted


def test_temp_name_collision_is_retried(lab_root, monkeypatch):
    names = iter(["aaaa", "aaaa", "bbbb"])
    monkeypatch.setattr(lab.secrets, "token_hex", lambda n: next(names))
    (lab_root / "scenarios" / ".x.yaml.aaaa.tmp").write_text("taken")
    lab.write_scenario("x", {"name": "x"}, lab_root, create_only=True)
    assert lab.load_scenario("x", lab_root) == {"name": "x"}
    assert (lab_root / "scenarios" / ".x.yaml.aaaa.tmp").read_text() == "taken"


def test_temp_cleanup_failure_does_not_fail_or_corrupt_creation(lab_root, monkeypatch):
    real_unlink = os.unlink

    def failing_unlink(path, *a, **k):
        if str(path).endswith(".tmp"):
            raise PermissionError(errno.EPERM, "cannot unlink")
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(os, "unlink", failing_unlink)
    lab.write_scenario("kept", {"name": "kept"}, lab_root, create_only=True)
    assert lab.load_scenario("kept", lab_root) == {"name": "kept"}
    # the residue is a dot-file hard link: invisible to listings, same content
    assert lab.list_scenario_names(lab_root).count("kept") == 1
    assert not [n for n in lab.list_scenario_names(lab_root) if n.startswith(".")]
