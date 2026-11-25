# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""Interface Type Registry and Interface Identity normalization (SSOT).

One place decides whether two interface spellings are the same interface.
Discovery (link reconciliation, merge, L3 merge) and topology validation all
use `normalize()`; nobody keeps a private alias table.

Concepts kept apart on purpose:

- Raw name: exactly what a device printed ("TF1/1/3", "Twe 1/1/3").
- Identity: `InterfaceIdentity(type_id, identifier)`, used only to compare.
  Never shown, never sent to a device, never persisted.
- Display name: the spelling stored in a topology (see `compact_display()`).
- Recognition status: RECOGNIZED / UNKNOWN_TYPE / INVALID_FORMAT / AMBIGUOUS.
  Only RECOGNIZED yields an identity; everything else is never guessed.

Resolution is syntactic: split the name into a type token and an identifier
with a strict grammar, then look the token up by exact (case-insensitive)
match. There is no prefix matching, no fuzzy matching and no runtime
learning. The registry is a typed constant, validated at import time; an
inconsistent registry is an implementation error (RegistryError), never a
silent UNKNOWN_TYPE.

Alias evidence (see `Alias.evidence`):
  DOCUMENTED  Cisco documentation confirms type and applicability.
  OBSERVED    seen in real device output; valid only where `os` says.
  LEGACY      carried over from the original table for compatibility.
UNVERIFIED aliases are deliberately not part of the active registry.

Adding an alias: see docs/architecture.md ("Extending the Interface Type
Registry"). Pure module: no I/O, no environment, no device access.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

_KNOWN_EVIDENCE = frozenset({"DOCUMENTED", "OBSERVED", "LEGACY"})
_KNOWN_OS = frozenset({"iosxr", "iosxe", "ios"})


class RegistryError(Exception):
    """The built-in registry is internally inconsistent (a bug, not input)."""


class RecognitionStatus(str, enum.Enum):
    RECOGNIZED = "RECOGNIZED"
    UNKNOWN_TYPE = "UNKNOWN_TYPE"
    INVALID_FORMAT = "INVALID_FORMAT"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class Alias:
    name: str  # lower-case type token, exact match
    evidence: str  # DOCUMENTED | OBSERVED | LEGACY (combine with "+")
    os: frozenset[str] | None = None  # interface-OWNER OS restriction; None = any
    legacy_long_name: str | None = None  # legacy L3 display expansion only


@dataclass(frozen=True)
class TypeEntry:
    type_id: str
    category: str
    aliases: tuple[Alias, ...] = ()


def _a(name, evidence, os=None, legacy=None):
    return Alias(name, evidence, frozenset(os) if os else None, legacy)


# Applicability of a restricted alias is the *owner's* OS: it must be
# reproducible later from the stored topology (device `type`) alone.
REGISTRY: tuple[TypeEntry, ...] = (
    TypeEntry("ethernet", "ethernet", (
        _a("et", "LEGACY", legacy="Ethernet"), _a("eth", "LEGACY", legacy="Ethernet"),
        _a("ethernet", "LEGACY", legacy="Ethernet"))),
    TypeEntry("fast-ethernet", "ethernet", (
        _a("fa", "LEGACY", legacy="FastEthernet"), _a("fas", "LEGACY", legacy="FastEthernet"),
        _a("fastethernet", "LEGACY", legacy="FastEthernet"))),
    TypeEntry("1ge", "ethernet", (
        _a("gi", "LEGACY", legacy="GigabitEthernet"), _a("gig", "LEGACY", legacy="GigabitEthernet"),
        _a("gigabitethernet", "LEGACY", legacy="GigabitEthernet"))),
    # The original table maps Tw/TwoGigabitEthernet to one type; what speed
    # that is has no evidence here, so the type id is deliberately not "2.5ge".
    TypeEntry("twogigabitethernet", "ethernet", (
        _a("tw", "LEGACY", legacy="TwoGigabitEthernet"),
        _a("twogigabitethernet", "LEGACY", legacy="TwoGigabitEthernet"))),
    TypeEntry("10ge", "ethernet", (
        _a("te", "LEGACY", legacy="TenGigabitEthernet"),
        _a("tengigabitethernet", "LEGACY", legacy="TenGigabitEthernet"),
        _a("tengige", "DOCUMENTED"))),
    TypeEntry("25ge", "ethernet", (
        _a("twentyfivegige", "DOCUMENTED"),
        # TF: IOS XR short name (documented); observed as the Remote Port ID
        # of an IOS XE-owned port. Twe: IOS XE short name (documented, observed).
        _a("tf", "DOCUMENTED+OBSERVED", os=("iosxr", "iosxe")),
        _a("twe", "DOCUMENTED+OBSERVED", os=("iosxe",)))),
    TypeEntry("40ge", "ethernet", (
        _a("fo", "LEGACY", legacy="FortyGigabitEthernet"),
        _a("fortygigabitethernet", "LEGACY", legacy="FortyGigabitEthernet"),
        _a("fortygige", "DOCUMENTED"))),
    TypeEntry("100ge", "ethernet", (
        _a("hu", "LEGACY", legacy="HundredGigE"), _a("hundredgige", "LEGACY", legacy="HundredGigE"))),
    TypeEntry("200ge", "ethernet", (
        _a("twohundredgige", "DOCUMENTED"), _a("th", "DOCUMENTED", os=("iosxr",)))),
    TypeEntry("400ge", "ethernet", (
        _a("fourhundredgige", "DOCUMENTED"), _a("fh", "DOCUMENTED", os=("iosxr",)))),
    TypeEntry("bundle-ether", "aggregation", (
        _a("bundle-ether", "DOCUMENTED"), _a("be", "DOCUMENTED", os=("iosxr",)))),
    TypeEntry("port-channel", "aggregation", (
        _a("po", "LEGACY", legacy="Port-channel"), _a("port-channel", "LEGACY", legacy="Port-channel"))),
    TypeEntry("loopback", "logical", (
        _a("lo", "LEGACY", legacy="Loopback"), _a("loopback", "LEGACY", legacy="Loopback"))),
    TypeEntry("bvi", "logical", (_a("bv", "LEGACY", legacy="BVI"), _a("bvi", "LEGACY", legacy="BVI"))),
    TypeEntry("vlan", "logical", (_a("vl", "LEGACY", legacy="Vlan"), _a("vlan", "LEGACY", legacy="Vlan"))),
    TypeEntry("tunnel", "logical", (_a("tu", "LEGACY", legacy="Tunnel"), _a("tunnel", "LEGACY", legacy="Tunnel"))),
    TypeEntry("serial", "legacy", (_a("se", "LEGACY", legacy="Serial"), _a("serial", "LEGACY", legacy="Serial"))),
    # Catalogued but with no active alias: no evidence collected yet, so these
    # names stay UNKNOWN_TYPE (and warn) until someone adds evidence + tests.
    TypeEntry("mgmt-eth", "management"),
    TypeEntry("pos", "legacy"),
    TypeEntry("atm", "legacy"),
    TypeEntry("null", "legacy"),
)


def normalize_os(os_name: str | None) -> str | None:
    if not os_name:
        return None
    return re.sub(r"[\s_-]", "", os_name.lower()) or None


def validate_registry(entries: tuple[TypeEntry, ...]) -> None:
    """Raise RegistryError for any inconsistency. Order-independent."""
    seen_types: set[str] = set()
    by_alias: dict[str, list[tuple[str, Alias]]] = {}
    for entry in entries:
        if not entry.type_id or entry.type_id != entry.type_id.strip():
            raise RegistryError(f"invalid type id {entry.type_id!r}")
        if entry.type_id in seen_types:
            raise RegistryError(f"duplicate type id {entry.type_id!r}")
        seen_types.add(entry.type_id)
        for alias in entry.aliases:
            if not alias.name or not re.fullmatch(r"[a-z][a-z-]*", alias.name):
                raise RegistryError(f"invalid alias {alias.name!r} in {entry.type_id!r}")
            for part in alias.evidence.split("+"):
                if part not in _KNOWN_EVIDENCE:
                    raise RegistryError(f"alias {alias.name!r}: evidence {part!r} not allowed in the active registry")
            if alias.os is not None and (not alias.os or not alias.os <= _KNOWN_OS):
                raise RegistryError(f"alias {alias.name!r}: invalid os condition {alias.os!r}")
            by_alias.setdefault(alias.name, []).append((entry.type_id, alias))
    for name, defs in by_alias.items():
        for i, (type_a, alias_a) in enumerate(defs):
            for type_b, alias_b in defs[i + 1:]:
                # Same alias twice is only legal for different types whose
                # OS conditions are both restricted and disjoint.
                disjoint = alias_a.os is not None and alias_b.os is not None and not (alias_a.os & alias_b.os)
                if type_a == type_b or not disjoint:
                    raise RegistryError(f"alias {name!r} is defined ambiguously ({type_a}, {type_b})")


validate_registry(REGISTRY)

_BY_ALIAS: dict[str, tuple[tuple[str, Alias], ...]] = {}
for _entry in REGISTRY:
    for _alias in _entry.aliases:
        _BY_ALIAS[_alias.name] = _BY_ALIAS.get(_alias.name, ()) + ((_entry.type_id, _alias),)


@dataclass(frozen=True, order=True)
class InterfaceIdentity:
    type_id: str
    identifier: str


@dataclass(frozen=True, order=True)
class RawIdentity:
    """Comparison key for a name that is not RECOGNIZED. A separate type, so it
    can never equal an InterfaceIdentity; used only for exact-text matching
    of information that is already stored."""

    raw: str


@dataclass(frozen=True)
class ObservationContext:
    owner_device: str | None = None
    owner_os: str | None = None
    owner_platform: str | None = None
    observing_device: str | None = None
    observing_os: str | None = None
    observing_platform: str | None = None
    protocol: str | None = None  # "cdp" | "lldp" | "l3" | "topology"
    command: str | None = None
    field: str | None = None  # "local" | "remote" | "l3" | "topology"


@dataclass(frozen=True)
class NormalizationResult:
    raw_name: str
    status: RecognitionStatus
    identity: InterfaceIdentity | None = None
    parsed_type: str | None = None
    parsed_identifier: str | None = None
    matched_alias: Alias | None = None
    reason: str = ""

    @property
    def recognized(self) -> bool:
        return self.status is RecognitionStatus.RECOGNIZED

    @property
    def key(self) -> "InterfaceIdentity | RawIdentity":
        return self.identity if self.identity is not None else RawIdentity(self.raw_name.strip())


_TYPE_RE = re.compile(r"^([A-Za-z][A-Za-z-]*)[ \t]*(.*)$", re.DOTALL)
_SEG = r"(?:[A-Za-z]{1,4})?\d+"
_IDENT_RE = re.compile(rf"^{_SEG}(?:[/:.]{_SEG})*$")


def normalize(raw: str, context: ObservationContext | None = None) -> NormalizationResult:
    """Resolve `raw` to an identity. Never raises for bad input."""
    name = raw.strip() if isinstance(raw, str) else ""
    if not name or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return NormalizationResult(str(raw), RecognitionStatus.INVALID_FORMAT, reason="empty or contains control characters")
    match = _TYPE_RE.match(name)
    if not match:
        return NormalizationResult(name, RecognitionStatus.INVALID_FORMAT, reason="no interface type")
    token, rest = match.group(1), match.group(2)
    defs = _BY_ALIAS.get(token.lower())
    if not defs:
        return NormalizationResult(name, RecognitionStatus.UNKNOWN_TYPE, parsed_type=token, parsed_identifier=rest or None,
                                   reason="interface type is not in the registry")
    if not rest or not _IDENT_RE.match(rest):
        return NormalizationResult(name, RecognitionStatus.INVALID_FORMAT, parsed_type=token, parsed_identifier=rest or None,
                                   reason="invalid interface identifier")
    owner_os = normalize_os(context.owner_os) if context else None
    if owner_os is None:
        applicable = [(t, a) for t, a in defs if a.os is None]
        if not applicable:
            return NormalizationResult(name, RecognitionStatus.AMBIGUOUS, parsed_type=token, parsed_identifier=rest,
                                       reason="alias depends on the owner OS, which is unknown")
    else:
        applicable = [(t, a) for t, a in defs if a.os is None or owner_os in a.os]
        if not applicable:
            return NormalizationResult(name, RecognitionStatus.UNKNOWN_TYPE, parsed_type=token, parsed_identifier=rest,
                                       reason=f"alias is not defined for owner OS {owner_os}")
    if len({t for t, _ in applicable}) != 1:
        return NormalizationResult(name, RecognitionStatus.AMBIGUOUS, parsed_type=token, parsed_identifier=rest,
                                   reason="alias resolves to more than one type")
    type_id, alias = applicable[0]
    return NormalizationResult(name, RecognitionStatus.RECOGNIZED, InterfaceIdentity(type_id, rest), token, rest, alias)


def compact_display(result: NormalizationResult) -> str | None:
    """Space-free spelling (type token as printed + identifier) of a
    RECOGNIZED name, or None."""
    if not result.recognized:
        return None
    return f"{result.parsed_type}{result.parsed_identifier}"


def re_resolves(display: str, identity: InterfaceIdentity, owner_os: str | None) -> bool:
    """True if `display` resolves to `identity` using only what a stored
    topology still has: the owner's device type."""
    return normalize(display, ObservationContext(owner_os=owner_os)).identity == identity


def legacy_long_name(raw: str) -> str:
    """Compatibility expansion used for L3 interface names: exactly the old
    behaviour for a legacy alias (Gi0/0 -> GigabitEthernet0/0), the stripped
    raw name for everything else."""
    result = normalize(raw)
    if result.recognized and result.matched_alias is not None and result.matched_alias.legacy_long_name:
        return f"{result.matched_alias.legacy_long_name}{result.parsed_identifier}"
    return raw.strip()


def safe_display(text: object, limit: int = 64) -> str:
    """Printable, single-line rendering of untrusted text for warnings."""
    out = []
    for ch in str(text):
        out.append(ch if ch.isprintable() and ch not in "\r\n\t" else f"\\x{ord(ch):02x}")
    shown = "".join(out)
    return shown if len(shown) <= limit else shown[:limit] + "...(truncated)"
