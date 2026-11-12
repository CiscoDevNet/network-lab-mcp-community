# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Validation of access-info values that end up in ssh/telnet argv.

Every value is checked before it becomes an argv element: a value that
OpenSSH could read as an option, or that carries whitespace, control
characters or shell metacharacters, is rejected rather than escaped.
Error messages never echo the rejected value (it may be private)."""

from __future__ import annotations

import ipaddress
import re

_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")


class UnsafeConnectionValue(ValueError):
    """A connection value is unsafe for use in an ssh/telnet argv."""


def validate_host(value: object) -> str:
    """Return the address as text if it is a valid hostname, IPv4 or IPv6
    address; otherwise raise. Leading '-' is always rejected."""
    if not isinstance(value, str) or not value:
        raise UnsafeConnectionValue("address must be a non-empty string")
    if value.startswith("-"):
        raise UnsafeConnectionValue("address must not start with '-'")
    if ":" in value:
        try:
            ipaddress.IPv6Address(value)
        except ValueError:
            raise UnsafeConnectionValue("address is not a valid IPv6 address") from None
        return value
    try:
        ipaddress.IPv4Address(value)
        return value
    except ValueError:
        pass
    if value.replace(".", "").isdigit():
        raise UnsafeConnectionValue("address is not a valid IPv4 address")
    if not _HOSTNAME_RE.match(value):
        raise UnsafeConnectionValue("address is not a valid hostname")
    return value


def validate_port(value: object) -> int:
    if isinstance(value, bool):
        raise UnsafeConnectionValue("port must be an integer")
    if isinstance(value, str):
        if not value.isascii() or not value.isdigit():
            raise UnsafeConnectionValue("port must be an integer")
        value = int(value)
    if not isinstance(value, int) or not 1 <= value <= 65535:
        raise UnsafeConnectionValue("port must be between 1 and 65535")
    return value


def validate_username(value: object) -> str:
    if not isinstance(value, str) or not _USERNAME_RE.match(value):
        raise UnsafeConnectionValue("username contains unsupported characters")
    return value


def format_host_for_jump(address: str) -> str:
    """Host as it must appear inside `-J user@host:port` (IPv6 bracketed)."""
    return f"[{address}]" if ":" in address else address


def validate_endpoint(config: dict, label: str) -> None:
    """Validate the address/port/username present in one access-info entry."""
    try:
        if config.get("address") not in (None, ""):
            validate_host(config["address"])
        if config.get("port") not in (None, ""):
            validate_port(config["port"])
        if config.get("username") not in (None, ""):
            validate_username(config["username"])
    except UnsafeConnectionValue as exc:
        raise UnsafeConnectionValue(f"{label}: {exc}") from None
