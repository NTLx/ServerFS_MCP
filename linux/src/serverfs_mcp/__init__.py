"""ServerFS MCP: secure MCP server for Linux workdirs.

A secure MCP server that exposes explicitly configured Linux directories as
controlled workdirs to AI agents, read-only by default with opt-in
per-workdir file mutation.
"""

from importlib.metadata import version

# Single source of truth for the version the MCP server reports: the
# installed package metadata (pyproject.toml). Docker image tags come from
# the Git tag and never flow back into the source tree.
SERVER_VERSION = version("serverfs-mcp")

__version__ = SERVER_VERSION
