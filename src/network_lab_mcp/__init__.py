# SPDX-License-Identifier: Apache-2.0
"""Network Lab MCP: an AI Network Engineer Workspace for real network labs."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import metadata as _package_metadata
from importlib.metadata import version as _package_version

_DISTRIBUTION_NAME = "network-lab-mcp"


def _metadata_field(field: str) -> str:
    """Read one field from installed package metadata (pyproject.toml is
    its source), never raising -- `show version` must not fail merely
    because the package is unavailable or the field is unset."""
    try:
        value = _package_metadata(_DISTRIBUTION_NAME).get(field)
    except PackageNotFoundError:
        return "unavailable"
    return value or "unavailable"


try:
    __version__ = _package_version(_DISTRIBUTION_NAME)
except PackageNotFoundError:
    # Not installed (e.g. run directly from a checkout without `pip install
    # -e .`); mcp_server.py and `show version` both tolerate this.
    __version__ = "unavailable"

__author__ = _metadata_field("Author")
# PEP 639 packages publish `License-Expression`; older metadata uses `License`.
__license__ = _metadata_field("License-Expression")
if __license__ == "unavailable":
    __license__ = _metadata_field("License")

# Not a standard packaging metadata field, so pyproject.toml has nowhere to
# put it; this constant is the single source of truth for it instead of a
# second copy living in cli/main.py.
__release_date__ = "2026-10-03"

try:
    # The MCP SDK is a required (never optional) runtime dependency -- see
    # pyproject.toml's `dependencies` list -- so this is read the same
    # never-raising way as __version__ itself, not hard-coded: `show
    # version` reports whatever SDK is actually installed, not whatever
    # version happened to be current when this was written.
    __mcp_sdk_version__ = _package_version("mcp")
except PackageNotFoundError:
    __mcp_sdk_version__ = "unavailable"
