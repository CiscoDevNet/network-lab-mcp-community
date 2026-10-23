# SPDX-License-Identifier: Apache-2.0
"""Tests for cli/editor.py: $VISUAL/$EDITOR/vim resolution order, the
candidate <-> temp-file <-> editor round trip, and error handling. A fake
editor script (see conftest.fake_editor) stands in for a real interactive
session so this suite never depends on one."""

from __future__ import annotations

import tempfile

import pytest

from network_lab_mcp.cli import editor


@pytest.fixture(autouse=True)
def _isolated_tempdir(tmp_path, monkeypatch):
    # edit_yaml_candidate() uses tempfile.mkstemp() with no explicit `dir`,
    # so it honors this module-global override -- keeps every test's
    # temp file (and cleanup check) inside pytest's own tmp_path.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


# ---- editor resolution order ----


def test_visual_takes_precedence_over_editor(monkeypatch):
    monkeypatch.setenv("VISUAL", "my-visual-editor")
    monkeypatch.setenv("EDITOR", "my-editor")
    assert editor.resolve_editor_command() == ["my-visual-editor"]


def test_editor_used_when_visual_unset(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "my-editor")
    assert editor.resolve_editor_command() == ["my-editor"]


def test_vim_fallback_when_neither_set(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    command = editor.resolve_editor_command()
    assert command[0] == "vim"
    # syntax-on / filetype=yaml (added only for our own fallback) plus
    # paste mode (added because the fallback itself is confirmed Vim).
    assert command == ["vim", "-c", "syntax on", "-c", "set filetype=yaml", "-c", "set paste"]


def test_explicit_vim_gets_paste_mode_but_no_decorative_options(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "vim")
    # The operator explicitly chose vim themselves -- Network Lab MCP must
    # not silently add its own decorative syntax/filetype options, but it
    # *does* add paste mode once the resolved editor is confirmed Vim,
    # regardless of whether that came from $VISUAL/$EDITOR or the fallback.
    assert editor.resolve_editor_command() == ["vim", "-c", "set paste"]


def test_editor_with_arguments_is_split_safely(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "code --wait")
    assert editor.resolve_editor_command() == ["code", "--wait"]


def test_malformed_editor_value_raises_clear_error(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", 'unterminated "quote')
    with pytest.raises(editor.EditorError):
        editor.resolve_editor_command()


# ---- candidate <-> temp file <-> editor round trip ----


def test_valid_yaml_edit_updates_candidate(monkeypatch, fake_editor):
    script = fake_editor("open(path, 'w').write('name: edited\\nobjectives: []\\n')")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    result = editor.edit_yaml_candidate({"name": "original", "objectives": ["x"]})
    assert result == {"name": "edited", "objectives": []}


def test_invalid_yaml_raises_and_leaves_no_temp_file(monkeypatch, fake_editor, tmp_path):
    script = fake_editor("open(path, 'w').write('name: [unterminated\\n')")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    with pytest.raises(editor.EditorError, match="line"):
        editor.edit_yaml_candidate({"name": "original"})
    assert list(tmp_path.glob("network-lab-mcp-*.yaml")) == []


def test_non_mapping_root_is_rejected(monkeypatch, fake_editor):
    script = fake_editor("open(path, 'w').write('- just\\n- a\\n- list\\n')")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    with pytest.raises(editor.EditorError, match="mapping"):
        editor.edit_yaml_candidate({"name": "original"})


def test_nonzero_exit_raises_and_candidate_untouched(monkeypatch, fake_editor):
    script = fake_editor("open(path, 'w').write('name: should-not-be-used\\n')\nsys.exit(3)")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    with pytest.raises(editor.EditorError, match="non-zero"):
        editor.edit_yaml_candidate({"name": "original"})


def test_semantic_no_change_round_trips_to_an_equal_mapping(monkeypatch, fake_editor):
    # Opening and saving without edits must not appear as a change to the
    # caller's dirty check (definition_candidate == definition_original).
    script = fake_editor("pass  # leave the file exactly as the caller wrote it")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    original = {"name": "original", "objectives": ["a", "b"]}
    result = editor.edit_yaml_candidate(original)
    assert result == original


def test_missing_editor_binary_raises_clear_error(monkeypatch):
    monkeypatch.setenv("EDITOR", "definitely-not-a-real-editor-binary")
    with pytest.raises(editor.EditorError, match="not found"):
        editor.edit_yaml_candidate({"name": "original"})


def test_temp_file_has_yaml_suffix_visible_to_editor(monkeypatch, fake_editor):
    script = fake_editor("assert path.endswith('.yaml'), path\nopen(path, 'w').write('name: ok\\n')")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    result = editor.edit_yaml_candidate({"name": "original"})
    assert result == {"name": "ok"}


def test_temp_file_cleaned_up_after_success(monkeypatch, fake_editor, tmp_path):
    script = fake_editor("open(path, 'w').write('name: ok\\n')")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    editor.edit_yaml_candidate({"name": "original"})
    assert list(tmp_path.glob("network-lab-mcp-*.yaml")) == []


# ---- Unicode readability ----


def test_existing_unicode_is_readable_before_editor_opens(monkeypatch, fake_editor):
    # The fake editor script inspects the raw temp file exactly as a real
    # vim session would see it, before making any change.
    script = fake_editor(
        "raw = open(path, encoding='utf-8').read()\n"
        "assert '20フローによるトラフィック経路確認' in raw, raw\n"
        "assert '\\\\u30D5' not in raw, raw\n"
    )
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    result = editor.edit_yaml_candidate({"name": "original", "description": "20フローによるトラフィック経路確認"})
    assert result == {"name": "original", "description": "20フローによるトラフィック経路確認"}


def test_unicode_entered_in_editor_survives_validation(monkeypatch, fake_editor):
    script = fake_editor(
        "import yaml\n"
        "data = yaml.safe_load(open(path, encoding='utf-8'))\n"
        "data['description'] = '日本語テスト café 🚀'\n"
        "with open(path, 'w', encoding='utf-8') as handle:\n"
        "    yaml.safe_dump(data, handle, allow_unicode=True)\n"
    )
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    result = editor.edit_yaml_candidate({"name": "original", "description": ""})
    assert result == {"name": "original", "description": "日本語テスト café 🚀"}


# ---- Vim paste-mode detection (positive, fail-safe; never Neovim, never
# an unconfirmed plain `vi`) ----


def test_visual_vim_gets_paste_mode(monkeypatch):
    monkeypatch.setenv("VISUAL", "vim")
    monkeypatch.delenv("EDITOR", raising=False)
    assert editor.resolve_editor_command() == ["vim", "-c", "set paste"]


def test_visual_absolute_vim_path_gets_paste_mode(monkeypatch):
    monkeypatch.setenv("VISUAL", "/usr/bin/vim")
    monkeypatch.delenv("EDITOR", raising=False)
    assert editor.resolve_editor_command() == ["/usr/bin/vim", "-c", "set paste"]


def test_editor_vim_gets_paste_mode_when_visual_unset(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "vim")
    assert editor.resolve_editor_command() == ["vim", "-c", "set paste"]


def test_vim_basic_and_vim_tiny_basenames_are_confirmed_vim():
    assert editor._is_confirmed_vim("vim") is True
    assert editor._is_confirmed_vim("/usr/bin/vim") is True
    assert editor._is_confirmed_vim("vim.basic") is True
    assert editor._is_confirmed_vim("vim.tiny") is True


def test_plain_vi_unresolvable_is_left_alone(monkeypatch):
    # No which() match and no symlink -- genuinely unconfirmed: must not
    # be guessed into Vim just because its name is "vi".
    monkeypatch.setattr(editor.shutil, "which", lambda name: None)
    monkeypatch.setattr(editor.os.path, "realpath", lambda p: p)
    monkeypatch.setenv("VISUAL", "vi")
    monkeypatch.delenv("EDITOR", raising=False)
    assert editor.resolve_editor_command() == ["vi"]


def test_plain_vi_that_is_busybox_is_left_alone(monkeypatch):
    # A `vi` that resolves (via which()+realpath()) to something that is
    # not one of Vim's own binary names (e.g. BusyBox) must never get
    # Vim-specific options injected.
    monkeypatch.setattr(editor.shutil, "which", lambda name: "/usr/bin/vi")
    monkeypatch.setattr(editor.os.path, "realpath", lambda p: "/bin/busybox")
    monkeypatch.setenv("VISUAL", "vi")
    monkeypatch.delenv("EDITOR", raising=False)
    assert editor.resolve_editor_command() == ["vi"]


def test_plain_vi_symlinked_to_vim_gets_paste_mode(monkeypatch):
    # Debian/Ubuntu-style alternatives: /usr/bin/vi -> /etc/alternatives/vi
    # -> /usr/bin/vim.basic. Resolved purely via which()/realpath(), no
    # subprocess probe -- confirmed Vim, so paste mode is added.
    monkeypatch.setattr(editor.shutil, "which", lambda name: "/usr/bin/vi")
    monkeypatch.setattr(editor.os.path, "realpath", lambda p: "/usr/bin/vim.basic")
    monkeypatch.setenv("VISUAL", "vi")
    monkeypatch.delenv("EDITOR", raising=False)
    assert editor.resolve_editor_command() == ["vi", "-c", "set paste"]


def test_nvim_never_gets_paste_mode(monkeypatch):
    monkeypatch.setenv("VISUAL", "nvim")
    monkeypatch.delenv("EDITOR", raising=False)
    assert editor.resolve_editor_command() == ["nvim"]


def test_nvim_absolute_path_never_gets_paste_mode(monkeypatch):
    monkeypatch.setenv("VISUAL", "/usr/bin/nvim")
    monkeypatch.delenv("EDITOR", raising=False)
    assert editor.resolve_editor_command() == ["/usr/bin/nvim"]


def test_nvim_is_never_confirmed_even_if_it_resolves_oddly(monkeypatch):
    # Defense in depth: nvim is excluded by name before any resolution is
    # even attempted, regardless of what which()/realpath() might return.
    monkeypatch.setattr(editor.shutil, "which", lambda name: "/usr/bin/vim.basic")
    monkeypatch.setattr(editor.os.path, "realpath", lambda p: "/usr/bin/vim.basic")
    assert editor._is_confirmed_vim("nvim") is False


def test_non_vim_editor_gets_no_vim_options(monkeypatch):
    monkeypatch.setenv("VISUAL", "nano")
    monkeypatch.delenv("EDITOR", raising=False)
    assert editor.resolve_editor_command() == ["nano"]


def test_editor_with_arguments_keeps_them_when_not_vim(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "code --wait")
    assert editor.resolve_editor_command() == ["code", "--wait"]


def test_vim_with_existing_arguments_keeps_them_and_adds_paste_mode(monkeypatch):
    monkeypatch.setenv("VISUAL", "vim -f")
    monkeypatch.delenv("EDITOR", raising=False)
    assert editor.resolve_editor_command() == ["vim", "-f", "-c", "set paste"]


def test_vim_with_existing_c_option_coexists_with_paste_mode(monkeypatch):
    monkeypatch.setenv("VISUAL", "vim -c 'set number'")
    monkeypatch.delenv("EDITOR", raising=False)
    assert editor.resolve_editor_command() == ["vim", "-c", "set number", "-c", "set paste"]


def test_vim_paste_mode_is_never_duplicated_if_user_already_set_it(monkeypatch):
    monkeypatch.setenv("VISUAL", "vim -c 'set paste'")
    monkeypatch.delenv("EDITOR", raising=False)
    assert editor.resolve_editor_command() == ["vim", "-c", "set paste"]


def test_target_file_still_appended_after_vim_paste_mode(monkeypatch, fake_editor, tmp_path):
    # edit_yaml_candidate() appends the temp-file path after whatever
    # resolve_editor_command() returns -- confirm the file path lands
    # after the injected -c option, not swallowed as its argument, by
    # having the fake "vim" assert its own argv shape.
    script = fake_editor(
        "import sys\n"
        "assert sys.argv[1:-1] == ['-c', 'set paste'], sys.argv\n"
        "assert sys.argv[-1] == path, sys.argv\n"
        "open(path, 'w').write('name: ok\\n')\n"
    )
    monkeypatch.setenv("VISUAL", f"python3 {script}")
    monkeypatch.setattr(editor, "_is_confirmed_vim", lambda executable: True)
    result = editor.edit_yaml_candidate({"name": "original"})
    assert result == {"name": "ok"}


# ---- Vim lifecycle regression: paste-mode injection changes argv only,
# never the candidate/temp-file/exit-code flow ----


def test_vim_paste_mode_does_not_affect_successful_round_trip(monkeypatch, fake_editor):
    script = fake_editor("open(path, 'w').write('name: edited\\n')")
    monkeypatch.setenv("VISUAL", f"python3 {script}")
    monkeypatch.setattr(editor, "_is_confirmed_vim", lambda executable: True)
    assert editor.edit_yaml_candidate({"name": "original"}) == {"name": "edited"}


def test_vim_paste_mode_does_not_affect_nonzero_exit_handling(monkeypatch, fake_editor):
    script = fake_editor("sys.exit(3)")
    monkeypatch.setenv("VISUAL", f"python3 {script}")
    monkeypatch.setattr(editor, "_is_confirmed_vim", lambda executable: True)
    with pytest.raises(editor.EditorError, match="non-zero"):
        editor.edit_yaml_candidate({"name": "original"})


# ---- Real environment, opportunistic (skipped when the binary genuinely
# isn't installed -- mirrors conftest.py's tmux-missing skip policy) ----


def test_real_vim_binary_is_confirmed_if_installed():
    import shutil as _shutil

    if _shutil.which("vim") is None:
        pytest.skip("vim is not installed on this system")
    assert editor._is_confirmed_vim("vim") is True


def test_real_vi_binary_reflects_actual_system_resolution():
    import shutil as _shutil

    if _shutil.which("vi") is None:
        pytest.skip("vi is not installed on this system")
    resolved = _shutil.which("vi")
    import os as _os

    real_basename = _os.path.basename(_os.path.realpath(resolved))
    expected = real_basename in editor._CONFIRMED_VIM_BASENAMES
    assert editor._is_confirmed_vim("vi") is expected
