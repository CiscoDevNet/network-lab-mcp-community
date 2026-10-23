# SPDX-License-Identifier: Apache-2.0
"""Tests for cli/main.py's Quick Start help, help topics, and `show
version` rendering. Grammar-level parsing/completion/help for these
commands is covered in test_grammar.py; this file covers the rendered
content and the read-only, side-effect-free nature of `show version`."""

from __future__ import annotations

import subprocess

import pytest

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain


# ---- Quick Start / help topics ----


def test_quick_start_mentions_all_configuration_areas_and_topics():
    text = climain.render_quick_start()
    for area in ("running-config", "access-info", "topology", "scenario", "reference"):
        assert area in text
    for topic in ("help claude", "help workflow", "help editor", "help cli"):
        assert topic in text
    assert "Network Lab MCP CLI" in text


def test_quick_start_does_not_read_like_a_bare_command_list():
    text = climain.render_quick_start()
    # The old bare-`help` output was just a list of "<token>  <description>"
    # rows; the new one must not merely reproduce that.
    assert "Purpose:" in text
    assert "Typical workflow:" in text


def test_help_claude_mentions_seven_tools_and_privacy_boundary():
    text = climain.render_help_claude()
    assert "seven" in text.lower()
    for tool in (
        "get_active_topology",
        "get_execution_instructions",
        "terminal_open",
        "terminal_send",
        "terminal_read",
        "terminal_list",
        "terminal_close",
    ):
        assert tool in text
    assert "password" in text.lower()
    assert "access-info" in text
    assert "jump" in text.lower()  # ProxyJump resolution is internal to MCP
    assert "logical device id" in text.lower()
    # Reuses the exact, already-verified registration command from
    # README.md rather than inventing a new one.
    assert "claude mcp add network-lab -s user -- /path/to/network-lab-mcp/.venv/bin/network-lab-mcp" in text


def test_help_claude_command_matches_readme():
    with open("README.md", encoding="utf-8") as handle:
        readme = handle.read()
    assert "claude mcp add network-lab -s user -- \\\n  /path/to/network-lab-mcp/.venv/bin/network-lab-mcp" in readme


def test_help_workflow_lists_ordered_steps_and_mentions_discovery():
    text = climain.render_help_workflow()
    assert "1. Configure access-info" in text
    assert "8. Use Claude Code." in text
    assert "discover topology" in text.lower()


def test_help_editor_describes_resolution_order_and_candidate_flow():
    text = climain.render_help_editor()
    assert "$VISUAL" in text and "$EDITOR" in text and "vim" in text
    assert "topology, scenario, and reference" in text.lower() or "topology" in text
    assert "commit" in text.lower()
    assert "clear" in text.lower()
    assert ".vimrc" in text


def test_help_cli_is_short_and_does_not_duplicate_full_reference():
    text = climain.render_help_cli()
    assert "?" in text
    assert "clear" in text
    assert "commit" in text
    assert "root" in text
    assert "end" in text
    assert "exit" in text
    assert "show configuration" in text
    assert "show running-config" in text
    assert "stays in the current mode" in text.lower()
    # Concise: much shorter than the full cli_reference.md.
    with open("docs/cli_reference.md", encoding="utf-8") as handle:
        full_reference = handle.read()
    assert len(text) < len(full_reference) / 5


def test_help_topic_dispatch_table_covers_every_grammar_topic():
    from network_lab_mcp.cli import grammar

    assert set(climain._HELP_TOPIC_RENDERERS) == set(grammar.HELP_TOPICS)


# ---- show version ----


def test_render_version_info_contains_all_required_fields():
    text = climain.render_version_info()
    assert "Network Lab MCP" in text
    assert "Version:" in text
    assert "Release date:" in text
    assert "Git commit:" in text
    assert "Git branch:" in text
    assert "Author:" in text
    assert "License:" in text
    assert "Python:" in text
    assert "Platform:" in text
    assert "MCP SDK:" in text


def test_version_matches_expected_release_metadata():
    import network_lab_mcp

    assert network_lab_mcp.__version__ == "0.2.0"
    assert network_lab_mcp.__release_date__ == "2026-10-03"
    assert network_lab_mcp.__author__ == "Eitaro Uemura"
    assert network_lab_mcp.__license__ == "Apache-2.0"


def test_python_version_is_dynamic_not_hardcoded():
    import platform

    text = climain.render_version_info()
    assert platform.python_version() in text
    assert "3.13.x" not in text  # never a hard-coded placeholder


def test_git_commit_resolves_dynamically_when_available(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="abc1234\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert climain._resolve_git_commit() == "abc1234"


def test_git_commit_unavailable_when_git_binary_missing(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert climain._resolve_git_commit() == "unavailable"


def test_git_commit_unavailable_on_nonzero_exit(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: not a git repository")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert climain._resolve_git_commit() == "unavailable"


def test_git_commit_unavailable_on_timeout(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert climain._resolve_git_commit() == "unavailable"


def test_git_branch_resolves_dynamically_when_available(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert climain._resolve_git_branch() == "main"


def test_git_branch_is_literal_head_when_detached(monkeypatch):
    # `git rev-parse --abbrev-ref HEAD` prints the literal "HEAD" in a
    # detached-HEAD checkout -- a true, non-raising answer, distinct from
    # "unavailable" (reserved for "no git"/"not a repository").
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="HEAD\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert climain._resolve_git_branch() == "HEAD"


def test_git_branch_unavailable_when_git_binary_missing(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert climain._resolve_git_branch() == "unavailable"


def test_git_branch_unavailable_on_nonzero_exit(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: not a git repository")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert climain._resolve_git_branch() == "unavailable"


def test_git_branch_unavailable_on_timeout(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert climain._resolve_git_branch() == "unavailable"


def test_platform_is_dynamic_not_hardcoded():
    import platform

    text = climain.render_version_info()
    assert f"Platform:      {platform.system()}" in text


def test_mcp_sdk_version_matches_installed_package():
    from importlib.metadata import version as package_version

    text = climain.render_version_info()
    assert f"MCP SDK:       {package_version('mcp')}" in text


def test_mcp_sdk_version_unavailable_when_package_missing(monkeypatch):
    monkeypatch.setattr("network_lab_mcp.__mcp_sdk_version__", "unavailable")
    text = climain.render_version_info()
    assert "MCP SDK:       unavailable" in text


def test_show_version_does_not_raise_when_metadata_missing(monkeypatch):
    monkeypatch.setattr("network_lab_mcp.__version__", "unavailable")
    monkeypatch.setattr("network_lab_mcp.__author__", "unavailable")
    monkeypatch.setattr("network_lab_mcp.__license__", "unavailable")
    monkeypatch.setattr("network_lab_mcp.__mcp_sdk_version__", "unavailable")
    text = climain.render_version_info()
    assert "unavailable" in text


# ---- show version: no side effects ----


def test_show_version_handler_does_not_touch_candidate_state(lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.set_scenario("failover_test")
    dirty_before = session.overall_dirty()
    mode_before = session.mode
    climain.h_show_version(session, {})
    assert session.overall_dirty() == dirty_before
    assert session.mode == mode_before
    assert session.definition_kind is None
    captured = capsys.readouterr()
    assert "Network Lab MCP" in captured.out


def test_show_version_works_in_exec_mode_without_configure(lab_root, monkeypatch, capsys):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    session = cfgmod.CliSession(lab_root)
    assert session.mode == "exec"
    climain.h_show_version(session, {})
    captured = capsys.readouterr()
    assert "Version:" in captured.out
    assert session.mode == "exec"


# ---- packaging / license metadata ----


def test_pyproject_declares_expected_version_author_license():
    # Avoid a hard tomllib dependency (stdlib only since Python 3.11, while
    # this project supports >=3.10); a plain text check is enough here.
    with open("pyproject.toml", encoding="utf-8") as handle:
        text = handle.read()
    assert 'version = "0.2.0"' in text
    assert 'name = "Eitaro Uemura"' in text
    assert 'license = "Apache-2.0"' in text
    assert 'license-files = ["LICENSE"]' in text
    assert "License ::" not in text


def test_license_file_exists_and_is_apache_2():
    with open("LICENSE", encoding="utf-8") as handle:
        text = handle.read()
    assert "Apache License" in text
    assert "Version 2.0, January 2004" in text
    assert "GNU GENERAL PUBLIC LICENSE" not in text


def test_readme_documents_license():
    with open("README.md", encoding="utf-8") as handle:
        readme = handle.read()
    assert "Apache License, Version 2.0" in readme


def test_license_prefers_license_expression_then_legacy_license(monkeypatch):
    import importlib
    import importlib.metadata

    import network_lab_mcp

    def fake_metadata(fields):
        class _Meta:
            def get(self, name):
                return fields.get(name)

        return lambda _name: _Meta()

    try:
        monkeypatch.setattr(importlib.metadata, "metadata", fake_metadata({"License-Expression": "Apache-2.0", "License": "legacy"}))
        assert importlib.reload(network_lab_mcp).__license__ == "Apache-2.0"
        monkeypatch.setattr(importlib.metadata, "metadata", fake_metadata({"License": "Legacy-License"}))
        assert importlib.reload(network_lab_mcp).__license__ == "Legacy-License"
        monkeypatch.setattr(importlib.metadata, "metadata", fake_metadata({}))
        assert importlib.reload(network_lab_mcp).__license__ == "unavailable"
    finally:
        monkeypatch.undo()
        importlib.reload(network_lab_mcp)
