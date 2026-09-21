# v0.4 Phase F release-candidate acceptance — 2026-09-21

## Result

**PASS** for the code, container, security and migration release-candidate gates.

The only intentionally deferred acceptance is the direct invocation of the two newly
added binary tools from a refreshed ChatGPT connector. The current conversation retained
its previously discovered 19-tool schema even while real connector requests were
successfully routed to a temporary 21-tool v0.4 service. That client discovery boundary
is tested after the final edge image is deployed and the plugin is refreshed; it is not a
code or container gate.

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
- scratch image build from the release-candidate source: PASS

## Security boundary

A disposable isolated Compose project using the release-candidate image verified:

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

## Release-candidate conclusion

The v0.4 code and deployment model are ready to be frozen into the final `0.4.0`
release-candidate commit. After that commit reaches `main`, the corresponding `edge`
image is used for the production refresh. A refreshed ChatGPT plugin then performs the
final direct `upload_binary_file` / `download_binary_file` connector acceptance before
the maintainer creates the `v0.4.0` tag.
