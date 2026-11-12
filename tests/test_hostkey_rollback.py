# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""known_hosts removal under concurrent modification and failures (real
ssh-keygen, isolated sandbox). The backup is never written back wholesale."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from network_lab_mcp import hostkey_store as hs
from network_lab_mcp import ssh_isolation as iso
from hostkey_support import make_host_key, make_real_runtime, real_binaries_available

pytestmark = pytest.mark.skipif(not real_binaries_available(), reason="OpenSSH binaries unavailable")


@pytest.fixture
def box(tmp_path):
    before = iso.snapshot_real_ssh_area()
    rt = make_real_runtime(tmp_path)
    a, b, c = (make_host_key(rt, n) for n in "abc")
    lines = [f"192.0.2.1 {a['type']} {a['blob']}", f"192.0.2.2 {b['type']} {b['blob']}",
             f"[192.0.2.1]:2222 {c['type']} {c['blob']}"]
    Path(rt.known_hosts).write_text("\n".join(lines) + "\n")
    os.chmod(rt.known_hosts, 0o600)
    other = f"192.0.2.99 {c['type']} {c['blob']}"
    yield rt, lines, other, str(tmp_path / "bk"), tmp_path
    iso.uninstall_for_tests()
    assert iso.snapshot_real_ssh_area() == before


def text(rt):
    return Path(rt.known_hosts).read_text().splitlines()


def test_A_conflict_before_update_is_detected(box):
    rt, lines, other, bk, tmp = box
    sig = hs.file_signature(rt.known_hosts)
    with open(rt.known_hosts, "a") as fh:
        fh.write(other + "\n")
    with pytest.raises(hs.HostKeyStoreError, match="changed since"):
        hs.remove_host("192.0.2.1", rt.known_hosts, bk, expect_signature=sig)
    assert text(rt) == lines + [other]
    assert not os.path.exists(bk)


def test_B_conflict_right_before_removal_modifies_nothing(box, monkeypatch):
    rt, lines, other, bk, tmp = box
    real_backup = hs.make_backup

    def backup_then_other_process_appends(*a, **k):
        path = real_backup(*a, **k)
        with open(rt.known_hosts, "a") as fh:
            fh.write(other + "\n")
        return path

    monkeypatch.setattr(hs, "make_backup", backup_then_other_process_appends)
    with pytest.raises(hs.HostKeyStoreError, match="changed before removal"):
        hs.remove_host("192.0.2.1", rt.known_hosts, bk)
    assert text(rt) == lines + [other]  # their line AND the target entry are untouched
    assert len(os.listdir(bk)) == 1


def test_C_other_process_appends_right_after_removal_keeps_both(box, monkeypatch):
    rt, lines, other, bk, tmp = box
    real = hs.run_keygen

    def after(args):
        r = real(args)
        if args and args[0] == "-R":
            with open(rt.known_hosts, "a") as fh:
                fh.write(other + "\n")
        return r

    monkeypatch.setattr(hs, "run_keygen", after)
    backup = hs.remove_host("192.0.2.1", rt.known_hosts, bk)
    assert text(rt) == [lines[1], lines[2], other]
    assert Path(backup).read_text().splitlines() == lines  # backup is untouched, never written back


def test_C2_unrelated_line_missing_after_removal_stops_without_writing_back(box, monkeypatch):
    rt, lines, other, bk, tmp = box
    real = hs.run_keygen

    def after(args):
        r = real(args)
        if args and args[0] == "-R":
            Path(rt.known_hosts).write_text("")  # another process emptied the file
        return r

    monkeypatch.setattr(hs, "run_keygen", after)
    with pytest.raises(hs.HostKeyStoreError, match="Review it manually"):
        hs.remove_host("192.0.2.1", rt.known_hosts, bk)
    assert Path(rt.known_hosts).read_text() == ""  # no automatic rewrite of the backup over their change


def test_F_backup_failure_modifies_nothing(box):
    rt, lines, other, bk, tmp = box
    blocker = tmp / "blocker"
    blocker.write_text("a file, not a directory")
    with pytest.raises(hs.HostKeyStoreError, match="backup could not be created"):
        hs.remove_host("192.0.2.1", rt.known_hosts, str(blocker / "bk"))
    assert text(rt) == lines


def test_G_keygen_R_failure_unchanged_and_changed_variants(box, monkeypatch):
    rt, lines, other, bk, tmp = box
    real = hs.run_keygen

    class R:
        returncode, stdout, stderr = 1, "", "boom"

    monkeypatch.setattr(hs, "run_keygen", lambda args: R() if args[0] == "-R" else real(args))
    with pytest.raises(hs.HostKeyStoreError, match="unchanged"):
        hs.remove_host("192.0.2.1", rt.known_hosts, bk)
    assert text(rt) == lines

    def fail_after_other_write(args):
        if args[0] == "-R":
            with open(rt.known_hosts, "a") as fh:
                fh.write(other + "\n")
            return R()
        return real(args)

    monkeypatch.setattr(hs, "run_keygen", fail_after_other_write)
    with pytest.raises(hs.HostKeyStoreError, match="NOT restored"):
        hs.remove_host("192.0.2.1", rt.known_hosts, str(tmp / "bk2"))
    assert text(rt) == lines + [other]


def test_symlink_swap_between_backup_and_removal_is_refused(box, monkeypatch):
    rt, lines, other, bk, tmp = box
    real_backup = hs.make_backup

    def swap(*a, **k):
        path = real_backup(*a, **k)
        evil = tmp / "evil_known_hosts"
        os.rename(rt.known_hosts, evil)
        os.symlink(evil, rt.known_hosts)
        return path

    monkeypatch.setattr(hs, "make_backup", swap)
    with pytest.raises(hs.HostKeyStoreError, match="symlink"):
        hs.remove_host("192.0.2.1", rt.known_hosts, bk)
    assert (tmp / "evil_known_hosts").read_text().splitlines() == lines


def test_other_entries_hashed_and_multi_key_survive(box):
    rt, lines, other, bk, tmp = box
    d = make_host_key(rt, "d", "ecdsa")
    with open(rt.known_hosts, "a") as fh:
        fh.write(f"192.0.2.1 {d['type']} {d['blob']}\n")  # second key type for the same host
    from hostkey_support import keygen
    keygen(rt, "-H", "-f", rt.known_hosts)
    os.unlink(rt.known_hosts + ".old")
    hs.remove_host("192.0.2.1", rt.known_hosts, bk)
    assert hs.classify("192.0.2.1", 22, rt.known_hosts).state == hs.UNKNOWN_CANDIDATE
    assert hs.classify("192.0.2.2", 22, rt.known_hosts).state == hs.REGISTERED_UNVERIFIED
    assert hs.classify("192.0.2.1", 2222, rt.known_hosts).state == hs.REGISTERED_UNVERIFIED
