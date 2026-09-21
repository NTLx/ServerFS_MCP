# v0.4.0 Phase F release acceptance record — 2026-09-21

This is historical evidence for the published v0.4.0 release, not a changelog.

## Result

**PASS** for the code, container, security, migration and post-release connector gates.

The v0.4.0 tag and GitHub Release were created, and the release workflow succeeded.
After the ChatGPT connector plugin was refreshed, direct binary upload/download calls
were verified successfully; the connector now exposes the binary download `outputSchema`.
Issue #10 was fixed and closed in v0.4.0.

## Root package

- `uv sync --frozen`: PASS
- `uv lock --check`: PASS
- `uv run ruff check .`: PASS
- `uv run ruff format --check .`: PASS
- `uv run pytest`: **754 passed**

## Agent Bridge

- `uv sync --frozen`: PASS
- `uv lock --check`: PASS
- Ruff check: PASS
- Ruff format check: PASS
- tests: **83 passed**

## Compose, shell and image

- base Compose config: PASS
- base + Agent overlay config: PASS
- `bash -n deployment/agent-bridge/install.sh`: PASS
- `bash -n deployment/agent-bridge/rollback_app.sh`: PASS
- scratch image build from the release source: PASS

## Security boundary

A disposable isolated Compose project using the release image verified:

- MCP container healthy;
- no published host port;
- internal Docker network only;
- external DNS unavailable/blocked;
- TCP egress to public IP targets unavailable;
- no `CONTROL_PLANE_*` or provider credential environment variables inside MCP;
- Streamable HTTP transport-security regressions pass:
  - legitimate internal Host accepted;
  - unexpected Host rejected with HTTP 421;
  - non-empty unapproved Origin rejected with HTTP 403.

Transport-security targeted suite: **47 passed**.

## Capability surface and executor boundary

The v0.4 full surface is exactly:

- 11 filesystem tools;
- 2 binary tools;
- 8 Agent tools;
- **21 tools total**.

No public shell, arbitrary argv executor, generic command runner or generic `write_file`
surface exists.

## v0.3.1 migration compatibility

The existing production `.env` contains no binary-transfer variables. Under v0.4 it
therefore resolves binary transfer to disabled by default. Existing Agent configuration
continues to parse, and no new overlay, network or provider reconfiguration is required.

Targeted migration/surface suite: **242 passed**.

The unchanged configuration retains the legacy 11/19-tool behavior until binary transfer
is explicitly enabled.

## Production invariance during acceptance

The production deployment was not recreated or restarted during Phase F.

At the start and end of the gate:

- MCP container ID: `b9535a41661c`
- MCP restart count: `0`
- MCP status: running / healthy
- Tunnel container ID: `a2ab2de7ecf9`
- Tunnel restart count: `0`
- Tunnel status: running

Disposable Phase F containers, network, workdir and scratch image were cleaned up after
validation.

## Release conclusion

The v0.4.0 code and deployment model were accepted and frozen in the published release.
The final refreshed-connector acceptance completed after plugin refresh, including direct
`upload_binary_file` / `download_binary_file` calls and the exposed download output schema.
