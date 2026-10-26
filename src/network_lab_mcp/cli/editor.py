# SPDX-License-Identifier: Apache-2.0
"""External YAML editor support for topology/scenario/reference definitions.

Resolution order: $VISUAL, then $EDITOR, then a `vim` fallback. Network Lab
MCP never adds vim-specific *decorative* options (syntax highlighting /
filetype) when the operator explicitly chose an editor via $VISUAL/$EDITOR
-- those apply only to the fallback it selects itself. Paste mode is the
one exception: whichever editor is resolved (explicit or fallback), if its
executable is positively confirmed to be real Vim (see
`_is_confirmed_vim()`), `-c "set paste"` is appended so a multi-line YAML
paste into the editor never triggers Vim's autoindent-amplification on
every newline. This is a per-invocation Vim startup command, not a change
to any vimrc -- it affects only the one editor session this module launches.

The candidate flow is: committed definition -> candidate data -> a secure,
unique `.yaml` temporary file -> external editor -> (editor exits) -> read
the temporary file -> parse -> validate -> update the in-memory candidate.
Nothing under lab/ is touched here; only `commit` writes real YAML. The
temporary file is always removed, including on every error path, and is
never written to with world/group-readable permissions.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml


class EditorError(Exception):
    """A clear, user-facing error from launching or reading the external editor."""


# Real Vim's own binary names -- never Neovim (`nvim`) and never bare `vi`
# on its own, since `vi` alone could be BusyBox vi, BSD/SysV vi, or any
# other vi-compatible editor that doesn't understand Vim's `-c` startup
# commands the same way (or at all).
_CONFIRMED_VIM_BASENAMES = frozenset({"vim", "vim.basic", "vim.tiny"})


def _is_confirmed_vim(executable: str) -> bool:
    """True only when `executable` can be positively confirmed to be real
    Vim without running it -- a fail-safe, positive-detection check, never
    a name-based guess. A direct basename match (`vim`, `vim.basic`,
    `vim.tiny` -- e.g. an explicit `VISUAL=vim` or `VISUAL=/usr/bin/vim`)
    is always confirmed. `nvim` is never treated as Vim: current Neovim
    already handles terminal paste correctly, and its own `paste` option
    is a deprecated compatibility shim -- there is nothing to fix there.
    Any other name, including plain `vi` (which many systems alias or
    symlink to Vim, e.g. Debian's `/usr/bin/vi` -> `/etc/alternatives/vi`
    -> `/usr/bin/vim.basic`, but which may just as easily be BusyBox/BSD/
    SysV vi instead), is resolved via `shutil.which()` + `os.path.realpath()`
    -- plain filesystem/PATH lookups, never a subprocess probe like
    `vi --version` -- to see whether it ultimately points at one of the
    confirmed Vim binaries. Unresolvable or non-matching names are left
    unconfirmed; the caller must leave those editors completely alone."""
    basename = os.path.basename(executable)
    if basename in _CONFIRMED_VIM_BASENAMES:
        return True
    if basename == "nvim":
        return False
    resolved = shutil.which(executable) or executable
    try:
        real = os.path.realpath(resolved)
    except OSError:
        return False
    return os.path.basename(real) in _CONFIRMED_VIM_BASENAMES


def _with_vim_paste_mode(command: list[str]) -> list[str]:
    """Append `-c "set paste"` to `command` when (and only when) its
    resolved executable is confirmed Vim (see `_is_confirmed_vim()`) --
    otherwise returns `command` unchanged, including for Neovim, an
    unconfirmed `vi`, and every non-Vim editor. Vim accepts any number of
    `-c` startup commands, so this never removes or reorders an existing
    one (whether supplied by the operator via $VISUAL/$EDITOR, e.g.
    `VISUAL="vim -c 'set number'"`, or this module's own fallback
    syntax/filetype options below) -- it is purely additive, appended
    last so it always takes effect regardless of what an earlier `-c` set."""
    if not command or not _is_confirmed_vim(command[0]):
        return command
    if command[-2:] == ["-c", "set paste"]:
        return command  # already present verbatim -- never duplicate it
    return [*command, "-c", "set paste"]


def resolve_editor_command() -> list[str]:
    """Return the argv prefix for the external editor: $VISUAL, then
    $EDITOR (each parsed with shlex.split() since it may embed arguments;
    `shell=True` is never used), then a `vim` fallback with just enough
    options to make an unfamiliar YAML temp file legible. Whichever one is
    resolved, `_with_vim_paste_mode()` additionally appends Vim's paste
    mode when (and only when) that resolved executable is confirmed Vim."""
    for var in ("VISUAL", "EDITOR"):
        value = os.environ.get(var)
        if not value:
            continue
        try:
            parts = shlex.split(value)
        except ValueError as exc:
            raise EditorError(f"Could not parse ${var}: {exc}") from exc
        if parts:
            return _with_vim_paste_mode(parts)
    return _with_vim_paste_mode(["vim", "-c", "syntax on", "-c", "set filetype=yaml"])


# PyYAML only wraps a flow mapping/plain scalar at this column; anything this
# large keeps a normal link on one physical line without any text rewriting.
_NO_WRAP_WIDTH = 1 << 30


class _FlowDict(dict):
    """Display-only marker: dumped as a one-line flow mapping. Never stored
    in a candidate -- it is built from copies inside `dump_topology_for_edit`."""


class _QuotedStr(str):
    """Display-only marker for a string containing a newline: dumped
    double-quoted (`\\n` escape) so a flow mapping stays on one line."""


class _TopologyEditDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False):
        return super().increase_indent(flow, False)  # indent sequence items under their key


_TopologyEditDumper.add_representer(
    _FlowDict, lambda d, v: d.represent_mapping("tag:yaml.org,2002:map", v, flow_style=True)
)
_TopologyEditDumper.add_representer(
    _QuotedStr, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", str(v), style='"')
)


def _display_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _display_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_display_copy(v) for v in value]
    if isinstance(value, str) and "\n" in value:
        return _QuotedStr(value)
    return value


def dump_topology_for_edit(data: Any) -> str:
    """Topology `edit` text: each top-level section is dumped on its own and
    joined by one blank line; every `links` entry is a one-line flow mapping.
    Presentation only -- `data` is not modified and the committed-file
    serializer (lab._atomic_write_yaml) is not involved."""
    if not isinstance(data, dict) or not data:
        return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)
    sections = []
    for key, value in data.items():
        width = None
        if key == "links" and isinstance(value, list):
            value = [_FlowDict(_display_copy(link)) if isinstance(link, dict) else link for link in value]
            width = _NO_WRAP_WIDTH
        sections.append(
            yaml.dump(
                {key: value},
                Dumper=_TopologyEditDumper,
                sort_keys=False,
                default_flow_style=False,
                allow_unicode=True,
                width=width if width is not None else 80,
            )
        )
    return "\n".join(sections)


def edit_yaml_candidate(data: Any, topology: bool = False) -> dict:
    """Open `data` in the resolved external editor via a secure temp file,
    then parse and minimally validate the result (valid YAML, root is a
    mapping) and return it. Raises EditorError -- leaving the caller's
    candidate untouched -- on a non-zero editor exit, an unparseable editor
    result, or a non-mapping root. The temporary file is removed on every
    path, including exceptions."""
    command = resolve_editor_command()
    fd, path_str = tempfile.mkstemp(suffix=".yaml", prefix="network-lab-mcp-")
    path = Path(path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if topology:
                handle.write(dump_topology_for_edit(data))
            else:
                yaml.safe_dump(data, handle, sort_keys=False, default_flow_style=False, allow_unicode=True)

        try:
            result = subprocess.run([*command, str(path)])
        except FileNotFoundError as exc:
            raise EditorError(f"Editor '{command[0]}' was not found.") from exc

        if result.returncode != 0:
            raise EditorError(
                f"Editor exited with a non-zero status ({result.returncode}). Candidate was not updated."
            )

        text = path.read_text(encoding="utf-8")
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            if mark is not None:
                raise EditorError(
                    f"YAML validation failed at line {mark.line + 1}, column {mark.column + 1}. "
                    "Candidate was not updated."
                ) from exc
            raise EditorError("YAML validation failed. Candidate was not updated.") from exc

        if not isinstance(parsed, dict):
            raise EditorError("Edited content must be a YAML mapping. Candidate was not updated.")
        return parsed
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
