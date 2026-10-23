# SPDX-License-Identifier: Apache-2.0
"""When `tmux` genuinely is not installed, most of this suite fails at the
same single, well-defined boundary: `terminal._tmux_base()` raising
TerminalError("The 'tmux' binary is not available on this system."). This
project's `conftest.py` narrowly reclassifies exactly that one sentinel
exception as a skip (via a `pytest_runtest_makereport` hookwrapper) rather
than letting every tmux-dependent test surface as a raw failure/error --
confirmed by directly hiding `tmux` from PATH and running a mixed pure/
tmux-dependent slice of the real suite.

This file proves the hook mechanism itself works, using pytest's own
`pytester` plugin to run a tiny, fully isolated nested pytest session
(never touching the real tmux socket, never depending on tmux actually
being absent on the host running THIS test) -- it imports the real hook
function straight out of tests/conftest.py, so it always exercises the
actual implementation, never a duplicated copy that could drift out of
sync."""

from __future__ import annotations

import importlib.util
from pathlib import Path

pytest_plugins = ["pytester"]

_CONFTEST_PATH = Path(__file__).parent / "conftest.py"


def _load_real_conftest_source() -> str:
    """Return tests/conftest.py's exact source, for the nested pytest run
    to import as its own conftest.py -- confirms this test always tracks
    the real implementation rather than a hand-copied duplicate."""
    return _CONFTEST_PATH.read_text(encoding="utf-8")


def test_tmux_missing_sentinel_is_reclassified_as_skipped(pytester):
    spec = importlib.util.spec_from_file_location("_real_conftest_check", _CONFTEST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "pytest_runtest_makereport"), (
        "tests/conftest.py must define pytest_runtest_makereport for this test to be meaningful"
    )

    pytester.makeconftest(_load_real_conftest_source())
    pytester.makepyfile(
        test_raises_tmux_missing="""
        def test_raises_the_exact_sentinel():
            raise Exception("The 'tmux' binary is not available on this system.")

        def test_raises_something_else():
            raise Exception("a completely unrelated failure")
        """
    )

    result = pytester.runpytest_subprocess("-q", "-rs", "test_raises_tmux_missing.py")

    result.assert_outcomes(skipped=1, failed=1)
    result.stdout.fnmatch_lines(["*tmux is not installed*"])


def test_tmux_missing_message_is_the_exact_string_terminal_raises():
    """The hook's sentinel string must stay byte-identical to what
    terminal._tmux_base() actually raises -- if either side drifts, the
    hook silently stops matching and every tmux-dependent test goes back
    to raw failures when tmux is absent."""
    from network_lab_mcp import terminal

    spec = importlib.util.spec_from_file_location("_real_conftest_check2", _CONFTEST_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    try:
        terminal._tmux_base()
    except terminal.TerminalError as exc:
        # Only meaningful if tmux is actually absent in this environment;
        # skip rather than assert nothing when tmux is present (the normal
        # case for this project's own validated dev environment).
        assert module._TMUX_MISSING_MESSAGE in str(exc)
    else:
        import shutil

        assert shutil.which("tmux") is not None
