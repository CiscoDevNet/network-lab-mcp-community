#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Human-facing entry point for the Network Lab CLI.
#
# This launches the IOS XR-compatible Network Lab CLI: a human configuration
# and control plane for lab/settings.yaml and lab/topologies/*.yaml, using a
# candidate -> commit model. It does not speak the MCP stdio protocol and is
# not registered with Claude Code -- see README.md and docs/cli_reference.md.
#
# Requires the same local editable installation as network-lab-mcp itself
# (`pip install -e .` in an activated environment); there is no separate
# console script for this CLI, so it is launched as a module.
set -euo pipefail

exec python3 -m network_lab_mcp.cli.main "$@"
