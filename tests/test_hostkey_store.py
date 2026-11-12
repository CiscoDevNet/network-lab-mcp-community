# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""known_hosts classification / removal with the REAL ssh-keygen,
operating only on files inside the isolated sandbox via the forced wrapper."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from network_lab_mcp import hostkey_store as hs
from network_lab_mcp import ssh_isolation as iso
from hostkey_support import keygen, make_host_key, make_real_runtime, real_binaries_available

pytestmark = pytest.mark.skipif(not real_binaries_available(), reason="OpenSSH binaries unavailable")


@pytest.fixture
def rt(tmp_path):
    before = iso.snapshot_real_ssh_area()
    runtime = make_real_runtime(tmp_path)
    yield runtime
    iso.uninstall_for_tests()
    assert iso.snapshot_real_ssh_area() == before


def put(rt, lines):
    Path(rt.known_hosts).write_text("".join(l + "\n" for l in lines))
    os.chmod(rt.known_hosts, 0o600)


def entry(host, key):
    return f"{host} {key['type']} {key['blob']}"


def test_missing_file_is_unknown(rt):
    assert hs.classify("192.0.2.1", 22, rt.known_hosts).state == hs.UNKNOWN_CANDIDATE


def test_not_found_is_unknown_and_found_is_registered(rt):
    k = make_host_key(rt, "a")
    put(rt, [entry("192.0.2.1", k)])
    assert hs.classify("192.0.2.2", 22, rt.known_hosts).state == hs.UNKNOWN_CANDIDATE
    st = hs.classify("192.0.2.1", 22, rt.known_hosts)
    assert st.state == hs.REGISTERED_UNVERIFIED
    assert st.keys[0].fingerprint == k["fingerprint"] and st.keys[0].key_type == "ssh-ed25519"


def test_nonstandard_port_identifier_and_ipv6(rt):
    k = make_host_key(rt, "a")
    put(rt, [entry("[192.0.2.1]:2222", k), entry("2001:db8::1", k), entry("[2001:db8::2]:830", k)])
    assert hs.classify("192.0.2.1", 2222, rt.known_hosts).state == hs.REGISTERED_UNVERIFIED
    assert hs.classify("192.0.2.1", 22, rt.known_hosts).state == hs.UNKNOWN_CANDIDATE
    assert hs.classify("2001:db8::1", 22, rt.known_hosts).state == hs.REGISTERED_UNVERIFIED
    assert hs.classify("2001:db8::2", 830, rt.known_hosts).state == hs.REGISTERED_UNVERIFIED


def test_hashed_entry_recognised(rt):
    k = make_host_key(rt, "a")
    put(rt, [entry("192.0.2.1", k)])
    keygen(rt, "-H", "-f", rt.known_hosts)
    assert "192.0.2.1" not in Path(rt.known_hosts).read_text()
    st = hs.classify("192.0.2.1", 22, rt.known_hosts)
    assert st.state == hs.REGISTERED_UNVERIFIED and st.keys[0].hashed


def test_multiple_key_types_listed(rt):
    a, b = make_host_key(rt, "a"), make_host_key(rt, "b", "ecdsa")
    put(rt, [entry("192.0.2.1", a), entry("192.0.2.1", b)])
    st = hs.classify("192.0.2.1", 22, rt.known_hosts)
    assert {k.key_type for k in st.keys} == {"ssh-ed25519", "ecdsa-sha2-nistp256"}


def test_unreadable_file_is_undetermined(rt):
    put(rt, ["x"])
    os.chmod(rt.known_hosts, 0)
    try:
        assert hs.classify("192.0.2.1", 22, rt.known_hosts).state == hs.UNDETERMINED
    finally:
        os.chmod(rt.known_hosts, 0o600)


def test_global_file_entry_means_not_unknown_and_unreadable_global_fails_closed(rt):
    k = make_host_key(rt, "a")
    Path(rt.global_known_hosts).write_text(entry("192.0.2.1", k) + "\n")
    os.chmod(rt.global_known_hosts, 0o600)
    assert hs.classify("192.0.2.1", 22, rt.known_hosts, rt.global_known_hosts).state == hs.REGISTERED_UNVERIFIED
    os.chmod(rt.global_known_hosts, 0)
    try:
        assert hs.classify("192.0.2.9", 22, rt.known_hosts, rt.global_known_hosts).state == hs.UNDETERMINED
    finally:
        os.chmod(rt.global_known_hosts, 0o600)
    os.unlink(rt.global_known_hosts)  # missing global file is fine
    assert hs.classify("192.0.2.9", 22, rt.known_hosts, rt.global_known_hosts).state == hs.UNKNOWN_CANDIDATE


def test_marker_entry_is_undetermined(rt):
    k = make_host_key(rt, "a")
    put(rt, [f"@cert-authority 192.0.2.1 {k['type']} {k['blob']}"])
    assert hs.classify("192.0.2.1", 22, rt.known_hosts).state in (hs.UNDETERMINED, hs.UNKNOWN_CANDIDATE)


def test_remove_only_target_preserves_everything_else(rt, tmp_path):
    a, b = make_host_key(rt, "a"), make_host_key(rt, "b")
    lines = ["# comment", entry("192.0.2.1", a), entry("192.0.2.2", b), entry("[192.0.2.1]:2222", b),
             entry("jump1", a), ""]
    put(rt, lines[:-1])
    backup_dir = str(tmp_path / "backups")
    backup = hs.remove_host("192.0.2.1", rt.known_hosts, backup_dir)
    left = Path(rt.known_hosts).read_text().splitlines()
    assert left == ["# comment", entry("192.0.2.2", b), entry("[192.0.2.1]:2222", b), entry("jump1", a)]
    assert Path(backup).read_text().splitlines() == lines[:-1]
    assert oct(os.stat(backup).st_mode & 0o777) == "0o600"
    assert oct(os.stat(backup_dir).st_mode & 0o777) == "0o700"


def test_remove_hashed_entry_only_target(rt, tmp_path):
    a, b = make_host_key(rt, "a"), make_host_key(rt, "b")
    put(rt, [entry("192.0.2.1", a), entry("192.0.2.2", b)])
    keygen(rt, "-H", "-f", rt.known_hosts)
    os.unlink(rt.known_hosts + ".old")
    hs.remove_host("192.0.2.1", rt.known_hosts, str(tmp_path / "bk"))
    assert hs.classify("192.0.2.1", 22, rt.known_hosts).state == hs.UNKNOWN_CANDIDATE
    assert hs.classify("192.0.2.2", 22, rt.known_hosts).state == hs.REGISTERED_UNVERIFIED


def test_shared_host_field_refused_nothing_modified(rt, tmp_path):
    a = make_host_key(rt, "a")
    put(rt, [f"192.0.2.1,jump1 {a['type']} {a['blob']}"])
    before = Path(rt.known_hosts).read_bytes()
    with pytest.raises(hs.HostKeyStoreError):
        hs.remove_host("192.0.2.1", rt.known_hosts, str(tmp_path / "bk"))
    assert Path(rt.known_hosts).read_bytes() == before
    assert not (tmp_path / "bk").exists()


def test_symlink_and_permissions_refused(rt, tmp_path):
    a = make_host_key(rt, "a")
    put(rt, [entry("192.0.2.1", a)])
    real = Path(rt.sandbox) / "real_kh"
    os.rename(rt.known_hosts, real)
    os.symlink(real, rt.known_hosts)
    with pytest.raises(hs.HostKeyStoreError):
        hs.remove_host("192.0.2.1", rt.known_hosts, str(tmp_path / "bk"))
    os.unlink(rt.known_hosts)
    os.rename(real, rt.known_hosts)
    os.chmod(rt.known_hosts, 0o666)
    with pytest.raises(hs.HostKeyStoreError):
        hs.remove_host("192.0.2.1", rt.known_hosts, str(tmp_path / "bk"))
    assert "192.0.2.1" in Path(rt.known_hosts).read_text()


def test_concurrent_modification_detected(rt, tmp_path):
    a = make_host_key(rt, "a")
    put(rt, [entry("192.0.2.1", a)])
    sig = hs.file_signature(rt.known_hosts)
    with open(rt.known_hosts, "a") as fh:
        fh.write(entry("192.0.2.9", a) + "\n")
    with pytest.raises(hs.HostKeyStoreError):
        hs.remove_host("192.0.2.1", rt.known_hosts, str(tmp_path / "bk"), expect_signature=sig)
    assert "192.0.2.1" in Path(rt.known_hosts).read_text()


def test_missing_entry_removal_fails_without_backup(rt, tmp_path):
    a = make_host_key(rt, "a")
    put(rt, [entry("192.0.2.1", a)])
    with pytest.raises(hs.HostKeyStoreError):
        hs.remove_host("192.0.2.77", rt.known_hosts, str(tmp_path / "bk"))
    assert not (tmp_path / "bk").exists()


def test_backups_never_overwrite(rt, tmp_path):
    a = make_host_key(rt, "a")
    put(rt, [entry("192.0.2.1", a)])
    names = {os.path.basename(hs.make_backup(rt.known_hosts, str(tmp_path / "bk"))) for _ in range(5)}
    assert len(names) == 5


def test_lock_excludes_second_holder(rt, tmp_path, monkeypatch):
    put(rt, [])
    monkeypatch.setattr(hs.KnownHostsLock, "wait_seconds", 0.3)
    with hs.KnownHostsLock(rt.known_hosts, str(tmp_path / "locks")):
        import threading

        result = []

        def other():
            try:
                with hs.KnownHostsLock(rt.known_hosts, str(tmp_path / "locks")):
                    result.append("got")
            except hs.HostKeyStoreError:
                result.append("refused")

        # shorten the wait for the test
        t = threading.Thread(target=other)
        orig = hs.threading.Lock
        t.start()
        t.join(timeout=10)
    assert result == ["refused"]
