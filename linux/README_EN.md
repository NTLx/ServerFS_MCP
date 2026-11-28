# ServerFS MCP — Linux

**English** · [简体中文](README.md)

This is the independent Linux source tree for the v0.27.0 development candidate. It is not a released update. For operating existing deployments, use the [v0.26.0 release](https://github.com/NTLx/ServerFS_MCP/releases/tag/v0.26.0) and its matching source, artifacts and runbook.

## Native boundary

Linux amd64/arm64; Docker Compose v2.24+ for Core and optional ingress, host-native systemd user Bridge.

The Core owns the MCP surface and workdir policy. An optional Bridge owns host Agent execution and Repo Ops; Connectors have independent lifecycle and credentials. File-only mode needs neither a Bridge nor Tunnel. Keep explicit grants, disjoint writable roots and private state; no generic shell or central manager.

## Develop and validate

Run from this directory, whether it is a standalone copy or part of the repository:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build --wheel
cd agent_bridge
uv sync --frozen
uv run pytest
uv build --wheel
```

For Core image validation, run Docker Compose from the platform root with a distinct scratch image tag. Never shadow the released image tag.

## Local references

- [Platform deployment runbook](deployment/linux/README.md)
- [Agent Bridge](agent_bridge/README.md)
- [Configuration example](.env.example)
- [Product documentation](https://ntlx.github.io/ServerFS_MCP/docs/)

Build/test inputs live in this tree; the monorepo's external contract and release checks are separate. Candidate build success is not authorization for native registration, service migration or publication.
