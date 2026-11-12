# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: Apache-2.0
"""UTF-8 Safe Editor Launch: verify (and if needed repair) the locale of the
environment handed to the external editor.

Only the *editor child's* environment is ever inspected or changed -- the
parent's `os.environ` is never modified and `locale.setlocale()` is never
called in this process. The verdict comes from the C library, not from the
locale's name: an isolated validation interpreter runs
`setlocale(LC_ALL, "")` (every category, honoring LC_ALL > LC_* > LANG and
LOCPATH) and reads `nl_langinfo(CODESET)`. LC_CTYPE alone is not enough: an
invalid LANG makes the all-category call fail even when LC_CTYPE is valid.

When the verdict is "not UTF-8", the first libc-verified UTF-8 locale is
selected and applied as `LC_ALL` in the editor environment only, then the
final environment is validated again with the same function.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from typing import Callable, Mapping, NamedTuple

_log = logging.getLogger(__name__)

PER_CHECK_TIMEOUT = 3.0  # seconds for one validation subprocess
TOTAL_TIMEOUT = 10.0  # seconds for the whole probe + search + final re-check

_SUCCESS_TOKEN = b"NLM_CODESET=UTF-8\n"

# Fixed validation program: no locale name is ever interpolated into it.
# Exit codes: 2 = setlocale failed, 3 = CODESET is not UTF-8.
_PROBE_CODE = (
    "import locale, sys\n"
    "try:\n"
    "    locale.setlocale(locale.LC_ALL, '')\n"
    "except locale.Error:\n"
    "    sys.exit(2)\n"
    "codeset = locale.nl_langinfo(locale.CODESET)\n"
    "if codeset.replace('-', '').replace('_', '').lower() != 'utf8':\n"
    "    sys.exit(3)\n"
    "sys.stdout.write('NLM_CODESET=UTF-8\\n')\n"
)

# Variables kept for the validation child besides LANG / LC_*: locale lookup
# paths (LOCPATH for glibc, PATH_LOCALE for Apple libc) and what a Python
# build may need to start at all.
_PATH_VARS = ("LOCPATH", "PATH_LOCALE")
_RUNTIME_VARS = ("PATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH")

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.@-]+$")
_LANG_TERRITORY = re.compile(r"^([A-Za-z]{2,3}(?:_[A-Za-z0-9]{2,3})?)(?:\.[A-Za-z0-9_-]+)?$")
_FIXED_CANDIDATES = ("en_US.UTF-8", "C.UTF-8", "en_US.utf8", "C.utf8", "UTF-8")

OK = "ok"
INVALID_LOCALE = "invalid_locale"
NON_UTF8_CODESET = "non_utf8_codeset"
TIMEOUT = "timeout"
PROCESS_LAUNCH_FAILED = "process_launch_failed"
INVALID_OUTPUT = "invalid_output"
VALIDATION_ERROR = "validation_error"


class LocaleCheck(NamedTuple):
    ok: bool
    reason: str


Validator = Callable[[Mapping[str, str], float], LocaleCheck]


def _validation_env(environment: Mapping[str, str]) -> dict[str, str] | None:
    """Minimal env for the validation child: same locale resolution inputs as
    the editor, none of the Python-specific settings. None if a locale search
    path cannot be made absolute safely."""
    env: dict[str, str] = {}
    for key, value in environment.items():
        if key in ("LANG", "LC_ALL") or key.startswith("LC_") or key in _RUNTIME_VARS:
            env[key] = value
    for key in _PATH_VARS:
        value = environment.get(key)
        if value is None:
            continue
        # The child runs in "/", so a relative path must be anchored where the
        # editor (which keeps the caller's cwd) would resolve it.
        parts = value.split(":")
        if any(not part for part in parts):
            return None
        try:
            env[key] = ":".join(os.path.abspath(part) for part in parts)
        except OSError:
            return None
    env["PYTHONUTF8"] = "0"
    env["PYTHONCOERCECLOCALE"] = "0"
    return env


def validate_utf8_locale(environment: Mapping[str, str], timeout: float = PER_CHECK_TIMEOUT) -> LocaleCheck:
    """Ask the C library whether `environment` yields a UTF-8 locale in every
    category. Any doubt (launch failure, timeout, unexpected output) is a
    failure."""
    exe = sys.executable
    if not exe or not os.path.isfile(exe) or not os.access(exe, os.X_OK):
        return LocaleCheck(False, PROCESS_LAUNCH_FAILED)
    env = _validation_env(environment)
    if env is None:
        return LocaleCheck(False, VALIDATION_ERROR)
    command = [exe, "-X", "utf8=0", "-S"]
    if sys.version_info >= (3, 11):
        command.append("-P")  # no implicit script/cwd entry on sys.path
    command += ["-c", _PROBE_CODE]
    try:
        result = subprocess.run(
            command,
            env=env,
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return LocaleCheck(False, TIMEOUT)
    except (OSError, ValueError):
        return LocaleCheck(False, PROCESS_LAUNCH_FAILED)
    if result.returncode == 2:
        return LocaleCheck(False, INVALID_LOCALE)
    if result.returncode == 3:
        return LocaleCheck(False, NON_UTF8_CODESET)
    if result.returncode != 0:
        return LocaleCheck(False, VALIDATION_ERROR)
    try:
        result.stdout.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        return LocaleCheck(False, INVALID_OUTPUT)
    if result.stdout != _SUCCESS_TOKEN:
        return LocaleCheck(False, INVALID_OUTPUT)
    return LocaleCheck(True, OK)


def candidate_locales(environment: Mapping[str, str]) -> list[str]:
    """UTF-8 locale names to try, best first. The first name derived from
    LC_ALL / LC_CTYPE / LANG (language_TERRITORY + .UTF-8) comes first, then
    fixed fallbacks. Unsafe names are never used to build candidates, and
    @modifier locales fall through to the fixed list."""
    candidates: list[str] = []
    for key in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = environment.get(key, "")
        if not value or value in ("C", "POSIX") or not _SAFE_NAME.match(value):
            continue
        match = _LANG_TERRITORY.match(value)
        if match:  # "@modifier" values do not match and are not rewritten
            candidates.append(match.group(1) + ".UTF-8")
        break
    candidates.extend(_FIXED_CANDIDATES)
    return list(dict.fromkeys(candidates))


def prepare_editor_env(
    environment: Mapping[str, str],
    validator: Validator | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, str]:
    """Return a copy of `environment` whose locale is verified UTF-8 -- with
    `LC_ALL` set to a verified UTF-8 locale only when needed. Raises
    `UnavailableUTF8Locale` when that cannot be guaranteed. `environment`
    itself is never modified."""
    validate = validator or validate_utf8_locale
    deadline = clock() + TOTAL_TIMEOUT

    def check(env: Mapping[str, str]) -> LocaleCheck:
        remaining = deadline - clock()
        if remaining <= 0:
            return LocaleCheck(False, TIMEOUT)
        return validate(env, min(PER_CHECK_TIMEOUT, remaining))

    editor_env = dict(environment)
    initial = check(editor_env)
    if initial.ok:
        return editor_env
    _log.debug("editor locale is not verified UTF-8 (%s); searching", initial.reason)

    for name in candidate_locales(editor_env):
        candidate_env = dict(editor_env)
        candidate_env["LC_ALL"] = name
        verdict = check(candidate_env)
        if verdict.reason == TIMEOUT and deadline - clock() <= 0:
            break
        if not verdict.ok:
            continue
        final = check(candidate_env)  # same function, final environment
        _log.debug("selected locale %s; final check: %s", name, final.reason)
        if final.ok:
            return candidate_env
        break
    raise UnavailableUTF8Locale(initial.reason)


class UnavailableUTF8Locale(Exception):
    """No UTF-8 locale could be verified for the editor environment."""

    def __init__(self, reason: str):
        super().__init__(
            "Error: Unable to launch editor with a UTF-8 locale.\n"
            "\n"
            "No usable UTF-8 locale was found.\n"
            "Please install or configure a UTF-8 locale and retry."
        )
        self.reason = reason
