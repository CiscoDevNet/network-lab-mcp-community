# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex CLI, GitHub Copilot, Cursor, Gemini CLI, etc.) that **develop** this
repository. This file is guidance only. It is not a safety control: the safety boundaries live in the implementation,
in [`lab/principles.yaml`](lab/principles.yaml), and in [SECURITY.md](SECURITY.md).

## Project overview

Network Lab MCP is an AI Network Engineer Workspace. It gives an MCP-capable AI client structured lab context and
controlled, observable terminal access to real network lab devices ("Vibe Networking"). The AI client does the
reasoning; Network Lab MCP provides the context and access; the human engineer keeps authority over decisions and
committed definitions.

Read these first, in this order:

1. [README.md](README.md): positioning, configuration model, quick start.
2. [SECURITY.md](SECURITY.md): trust model, credential handling, terminal and logging risks.
3. [docs/architecture.md](docs/architecture.md): design goals and component behavior.
4. [docs/mcp_tools.md](docs/mcp_tools.md): the MCP tool contract.
5. [docs/cli_reference.md](docs/cli_reference.md): the human CLI.
6. [docs/scenario_format.md](docs/scenario_format.md): Scenario and Reference format.
7. [`lab/principles.yaml`](lab/principles.yaml): principles shared by every scenario, including execution-source and
   filesystem-boundary rules.

## Components

- **MCP server** (`src/network_lab_mcp/mcp_server.py`, console script `network-lab-mcp`): a stdio server that exposes
  exactly **seven** tools:
  - `get_active_topology`: the committed, safe logical topology (never access-info).
  - `get_execution_instructions`: the active scenario and execution instructions.
  - `terminal_open`, `terminal_send`, `terminal_read`, `terminal_list`, `terminal_close`: managed terminal sessions.

  Do not add, remove, or rename tools unless the task explicitly asks for it, and keep
  [docs/mcp_tools.md](docs/mcp_tools.md) in sync with the code.
- **Human CLI** (`src/network_lab_mcp/cli/`, launched with `./run_cli.sh`): an IOS XR-style configuration and control
  plane for the lab definitions. It is not an MCP server and is not registered with an AI client.
- **Terminal management** (`src/network_lab_mcp/terminal.py`): sessions live in a dedicated `tmux` server (separate
  named socket) and use `ssh` or `telnet`. Production, validation, and Discovery sessions are structurally separate
  namespaces. Sessions are observable by the human and logged.
- **Topology Discovery** (`src/network_lab_mcp/discovery.py`): builds a topology candidate from LLDP/CDP (IOS XR,
  IOS XE, classic IOS) with optional L3 enrichment.
- **Lab data** (`src/network_lab_mcp/lab.py`, `lab/`): running-config selections, access-info, topologies, scenarios,
  and references.

## Configuration model

A human edits a **candidate** through the CLI and applies it with **commit**; the MCP tools only ever read
*committed* state, reloaded from disk on every call. `access-info` (address, transport, username, password) is private:
no MCP tool returns it, and it is edited through structured CLI commands. Keep this boundary intact.

Only `lab/*/sample_lab.yaml`, `lab/scenarios/getting_started.yaml`, `lab/references/cisco_platform_guidance.yaml`,
`lab/settings.example.yaml`, and `lab/principles.yaml` are tracked. Other files under `lab/`, `logs/`, and
`docs/development_history.md` are local, private, and git-ignored. Never commit them and never copy their contents
into code, tests, documentation, or issues.

## Development environment

- Python 3.10 or later; `tmux` is required for terminal features; `ssh` and `telnet` clients are needed only to reach
  real devices.
- Install from a local checkout in a virtual environment:

  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  python -m pip install -e ".[dev]"
  ```

- Reinstall after changing `pyproject.toml`: `show version` reads installed package metadata, and an older editable
  install keeps serving the old metadata.
- Runtime dependencies: `mcp`, `pyyaml`, `prompt_toolkit`. Dev dependency: `pytest`.
- The project is licensed under Apache-2.0. Python and shell sources start with `# SPDX-License-Identifier: Apache-2.0`.
  Do not add Copyright lines or other organizations' names to files without a confirmed basis.

## Testing

```bash
python -m pytest
```

- The default run never touches real hardware, and the test suite redirects `tmux` to a disposable socket.
- Tests that need real devices (`tests/test_real_lab_iosxr.py`) are skipped unless `NETWORK_LAB_REAL_TESTS=1` is
  set. Set it only when the user has explicitly approved running against their lab, never by default, and never use
  `--showlocals` in that case.
- Add or update tests with every behavior change.

## Security and safety

- Never connect to real devices, send configuration commands, or open terminal sessions unless the user explicitly
  asked for that in the current task.
- Never print, log, commit, or paste credentials, `access-info` contents, terminal logs, or device output that may
  contain secrets. Use placeholders and the RFC 5737 documentation address ranges (`192.0.2.0/24`,
  `198.51.100.0/24`, `203.0.113.0/24`) in tests and docs.
- Preserve the candidate/commit boundary, the committed-only MCP boundary, and the credential-isolation behavior
  described in [SECURITY.md](SECURITY.md). Do not weaken a safety check to make a test pass.
- During normal lab work (as opposed to development), follow [`lab/principles.yaml`](lab/principles.yaml): treat the
  project directory as protected, save generated artifacts in a confirmed workspace outside it, and change lab
  definitions only through the official CLI workflow with human approval.
- When developing, change only what the task requires, keep unrelated files untouched, and report exactly which files
  you created or changed.
- Report suspected security issues as described in [SECURITY.md](SECURITY.md), not in public issues.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
