# MCP Registry metadata and schema

`2025-12-11.json` is vendored from the official MCP Registry repository at immutable commit
[`11b7b0e274d518b6f449c5ee3518a6151d9848b9`](https://github.com/modelcontextprotocol/registry/tree/11b7b0e274d518b6f449c5ee3518a6151d9848b9):
[`internal/validators/schemas/2025-12-11.json`](https://github.com/modelcontextprotocol/registry/blob/11b7b0e274d518b6f449c5ee3518a6151d9848b9/internal/validators/schemas/2025-12-11.json).

SHA-256: `2e1e2dc758185bfc91023df4e5ace0e1c1bdb7b7b3d6614bb9ac6b9e9a9c2f68`.

The validator uses this local copy and does not fetch schemas at runtime.

## Package scope

Root `server.json` describes a single version-pinned OCI package, not the named multi-component deployment or the skills-only Plugin. Its Docker run configuration publishes `127.0.0.1:8000:8000`, mounts exactly one operator-selected workspace read-only at `/workdirs/01`, sets workdir policy read-only and admits local Host/Origin. The package does not configure built-in caller authentication, Agent Bridge or Connectors. Loopback is a local exposure boundary, not per-caller identity. Do not publish it externally or treat it as a general Compose installation. For remote or optional capabilities use the relevant platform runbook and explicit admission.

## Validate

From the repository root with the locked development environment:

```bash
uv run python deployment/registry/validate.py
```

The validator checks the vendored JSON Schema, root version and exact OCI tag, repository/OCI ownership identity, loopback transport and required read-only runtime configuration. Do not change schema pins or add secrets merely to pass validation. Metadata validation neither pulls the image nor proves live MCP behavior or Registry publication.
