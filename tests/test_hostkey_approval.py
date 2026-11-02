# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import base64
import os
import pty
import threading
import time

import pytest

from network_lab_mcp import hostkey_approval as ha

FP = "SHA256:" + base64.b64encode(bytes(range(32))).decode().rstrip("=")


def test_fingerprint_validation():
    assert ha.parse_fingerprint("  " + FP + " ") == FP
    for bad in ("", "sha256:" + FP[7:], FP + "=", FP[:-1], FP + "A", "MD5:aa:bb", "SHA256:" + "!" * 43, "SHA256: " + "A" * 42, "SHA256:" + "A" * 20 + " " + "A" * 23):
        with pytest.raises(ha.ApprovalError):
            ha.parse_fingerprint(bad)


def test_key_types_and_rsa_never_sha1():
    assert ha.parse_key_type(" ed25519 ") == "ED25519"
    with pytest.raises(ha.ApprovalError):
        ha.parse_key_type("DSA")
    assert "ssh-rsa" not in ha.host_key_algorithms("RSA").split(",")
    assert ha.host_key_algorithms("RSA") == "rsa-sha2-512,rsa-sha2-256"
    assert ha.host_key_algorithms("ECDSA").count("nistp") == 3
    assert ha.key_label_for_known_hosts_type("ssh-rsa") == "RSA"


def test_sanitize_strips_escape_sequences():
    s = ha.sanitize_display("host\x1b[2J\x07\u009bx")
    assert "\x1b" not in s and "\x07" not in s and "\u009b" not in s


def _grant(reg, **kw):
    base = dict(host="192.0.2.20", port=22, hop="target", key_label="ED25519", fingerprint=FP, operation="register")
    base.update(kw)
    return reg.grant(**base)


def _consume(reg, a, **kw):
    base = dict(host="192.0.2.20", port=22, hop="target", key_label="ED25519", fingerprint=FP,
                operation="register", discovery_id=reg.discovery_id)
    base.update(kw)
    reg.consume(a, **base)


def test_approval_is_single_use_and_scoped():
    reg = ha.ApprovalRegistry()
    a = _grant(reg)
    _consume(reg, a)
    with pytest.raises(ha.ApprovalError):
        _consume(reg, a)
    for change in ({"host": "192.0.2.21"}, {"port": 2222}, {"hop": "jump"}, {"key_label": "RSA"},
                   {"fingerprint": "SHA256:" + "A" * 43}, {"operation": "replace"}, {"discovery_id": "other"}):
        b = _grant(reg)
        with pytest.raises(ha.ApprovalError):
            _consume(reg, b, **change)
        assert b.consumed  # burned even on mismatch


def test_approval_expires():
    now = [1000.0]
    reg = ha.ApprovalRegistry(clock=lambda: now[0])
    a = _grant(reg)
    now[0] += ha.APPROVAL_TTL_SECONDS + 1
    with pytest.raises(ha.ApprovalError):
        _consume(reg, a)


# ---- prompt behaviour over a REAL pty -------------------------------------


class PtyChannel:
    """DevTtyChannel logic against a pty pair (the test drives the master)."""

    def __init__(self):
        self.master, self.slave = pty.openpty()
        self.out = b""
        self._stop = False
        threading.Thread(target=self._drain, daemon=True).start()
        self._impl = ha.DevTtyChannel.__new__(ha.DevTtyChannel)
        self._impl._fd = self.slave

    def _drain(self):
        while not self._stop:
            try:
                import select
                r, _, _ = select.select([self.master], [], [], 0.05)
                if r:
                    self.out += os.read(self.master, 4096)
            except OSError:
                return

    def flush_input(self):
        self._impl.flush_input()

    def write(self, t):
        self._impl.write(t)

    def readline(self, timeout):
        return self._impl.readline(timeout)

    def type(self, text):
        os.write(self.master, text.encode())

    def close(self):
        self._stop = True


CTX = ha.PromptContext(device="Router1", hop="target", host="192.0.2.20", port=22, operation="register")


def _typed(channel, lines, delay=0.15):
    def run():
        for line in lines:
            time.sleep(delay)
            channel.type(line + "\n")
    threading.Thread(target=run, daemon=True).start()


def test_prompt_happy_path_and_stale_input_flushed():
    ch = PtyChannel()
    ch.type("ED25519\n" + FP + "\n3\nyes\n")  # arrives BEFORE the prompt: must be discarded
    time.sleep(0.2)
    _typed(ch, ["ECDSA", FP, "3", "yes"])
    got = ha.collect_expected_key(ch, CTX, timeout=5)
    ch.close()
    assert got == ("ECDSA", FP)


@pytest.mark.parametrize("script", [
    [""], ["ED25519", ""], ["ED25519", FP, ""], ["ED25519", FP, "1"], ["ED25519", FP, "3", ""],
    ["ED25519", FP, "3", "y"], ["ED25519", FP, "3", "YES"], ["DSA"], ["ED25519", "MD5:aa"], ["ED25519", FP, "9"],
])
def test_prompt_cancels_on_anything_but_explicit_approval(script):
    ch = PtyChannel()
    _typed(ch, script, delay=0.05)
    assert ha.collect_expected_key(ch, CTX, timeout=1.5) is None
    ch.close()


def test_prompt_timeout_cancels():
    ch = PtyChannel()
    t0 = time.monotonic()
    assert ha.collect_expected_key(ch, CTX, timeout=0.5) is None
    assert time.monotonic() - t0 < 3
    ch.close()


def test_prompt_details_option_then_approve():
    ch = PtyChannel()
    _typed(ch, ["ED25519", FP, "2", "3", "yes"], delay=0.08)
    assert ha.collect_expected_key(ch, CTX, timeout=5) == ("ED25519", FP)
    assert FP.encode() in ch.out
    ch.close()


def test_prompt_never_echoes_control_chars_from_host():
    ch = PtyChannel()
    evil = ha.PromptContext(device="R\x1b[2J", hop="target", host="h\x1b]0;x\x07", port=22, operation="register")
    _typed(ch, [""], delay=0.05)
    ha.collect_expected_key(ch, evil, timeout=1)
    assert b"\x1b[2J" not in ch.out and b"\x1b]0" not in ch.out
    ch.close()


def test_devtty_channel_refuses_non_tty_stdio(monkeypatch):
    monkeypatch.setattr(os, "isatty", lambda fd: False)
    with pytest.raises(ha.ApprovalError):
        ha.DevTtyChannel()
