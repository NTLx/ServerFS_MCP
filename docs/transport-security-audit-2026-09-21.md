# Streamable HTTP Transport Security Audit — 2026-09-21

Issue: GitHub #10
Release gate: ServerFS MCP v0.4.0

## Scope

This audit establishes the transport-security inputs before ServerFS enables
`mcp==2.2.0` DNS-rebinding protection for its Streamable HTTP endpoint.

No production service was restarted, recreated, reconfigured or proxied during S1.

## Runtime topology

Current production components:

- MCP endpoint configured in tunnel-client:
  `http://serverfs-mcp:8000/mcp`
- tunnel-client image: `ghcr.io/openai/tunnel-client:v0.0.14`
- tunnel-client repo digest:
  `sha256:41d7c85dab37797a3eaa17c41b94a7206dd0bc186fd361034c9ec3863596ff6c`
- no `MCP_EXTRA_HEADERS`;
- no `MCP_DISCOVERY_EXTRA_HEADERS`;
- no runtime Host/Origin override configuration.

The MCP container remains on the Docker internal network with no published port.

## Host determination

The v0.0.14 source tag is commit:

`0f870e50a973fa820d4c409000059e181e8d242b`

The runtime HTTP transport is built from the configured MCP endpoint. The forwarding
path does not explicitly assign `req.Host` and does not set a special Host header.
Under Go `net/http`, when `Request.Host` is empty, the request Host is the target
URL authority.

For the deployed endpoint the effective Host is therefore:

`serverfs-mcp:8000`

This is also the Compose DNS service name and fixed internal port used by the production
Tunnel -> MCP hop.

## Origin behavior

The v0.0.14 tunnel-client does not synthesize an Origin header.

Connector-forwarded headers are applied to same-origin runtime requests after static MCP
headers. An inbound connector Origin, if present, can therefore be forwarded; it is
removed on cross-origin redirects together with other connector-forwarded headers.

Current production has no static Origin injection.

A non-disruptive AF_PACKET capture was attempted from an ephemeral sidecar sharing the
MCP container network namespace. The kernel rejected raw-socket creation with
`EPERM` before capture started. Existing `/healthz`, `/readyz`, health JSON and
normal INFO logs contain no request-header field, so the pre-fix live connector Origin
cannot be observed through existing read-only diagnostics without adding instrumentation.

This uncertainty is intentionally resolved by S3 rather than by widening the allowlist:
v0.4 permits the determined Host and permits **no non-empty Origin**. In `mcp==2.2.0`,
an absent Origin is accepted as same-origin. S3 then validates that exact policy through
the real ChatGPT -> OpenAI Secure MCP Tunnel path; a 403 or 421 would be a release blocker
and measurement signal rather than a reason to fall back to a wildcard.

## mcp==2.2.0 behavior

Installed project version: `mcp==2.2.0`.

`TransportSecuritySettings` defaults:

- `enable_dns_rebinding_protection=True`
- `allowed_hosts=[]`
- `allowed_origins=[]`

The released v0.3.1 baseline runs Streamable HTTP at `host="0.0.0.0"` with
`transport_security=None`. For non-loopback bind hosts that leaves Host/Origin
validation disabled. The v0.4 working tree explicitly supplies the policy described
below.

When protection is enabled:

- Host exact matches are accepted;
- `host:*` supports a port wildcard;
- missing/invalid Host -> HTTP 421;
- absent Origin -> accepted as same-origin;
- non-empty Origin must match the configured allowlist;
- disallowed Origin -> HTTP 403;
- POST Content-Type validation remains independent.

## v0.4 decision

The smallest production policy is:

- DNS-rebinding protection: enabled;
- allowed Host: exactly `serverfs-mcp:8000`;
- allowed non-empty Origins: none.

No configurable wildcard allowlist is introduced for v0.4.0 unless real deployment
evidence proves an additional legitimate authority/origin is required.

Regression tests exercise the real ASGI Streamable HTTP layer in
`tests/test_security.py`, plus the production startup wiring in `tests/test_main.py`:

1. `Host: serverfs-mcp:8000`, no Origin -> transport accepts;
2. unexpected or missing Host -> 421;
3. legal Host + unexpected non-empty Origin -> 403;
4. legal Host + no Origin reaches normal MCP request processing;
5. `main()` passes the explicit transport-security object to `MCPServer.run()`;
6. existing MCP transport behavior remains green.

S2 validation on the v0.4 working tree:

- targeted `tests/test_security.py tests/test_main.py`: **47 passed**;
- Ruff check: pass;
- Ruff format check: pass;
- complete root suite: **697 passed**.

## S3 real Tunnel verification

For this live revalidation, the retained log boundary was
`2026-09-21T04:52:07Z UTC`. Logs from the temporary project
`serverfs_issue10_live` show six visible successful `list_workdirs` tool-call records,
each with `returned: 5`, after the boundary. The same logs show temporary-MCP
Streamable HTTP traffic with 50 `POST /mcp` responses of HTTP 200 and one HTTP 400
session/setup response. The temporary tunnel log shows dispatcher traffic forwarding
commands to the MCP server. S3 is therefore **PASS** (visible successful
`list_workdirs` count: 6; at least one successful real probe was required).

No HTTP 421 or HTTP 403 response was observed after the boundary. No invalid Host,
invalid Origin, DNS-rebinding rejection, or MCP session/protocol error was observed in
the focused temporary MCP/tunnel logs. This records only the observed post-boundary
runtime evidence; it does not infer headers that were not logged.

Before cleanup, the production containers were unchanged and healthy:

- MCP `b9535a41661c5ca5b4b2ecc614472088b5ae4ea64ac73fc7db1b59b30560e040`,
  StartedAt `2026-09-21T00:09:41.184115023Z`, RestartCount `0`, health `healthy`;
- Tunnel `a2ab2de7ecf9148734bbd68df964999807900b4aedae7a0825ff7086afadb7d2`,
  StartedAt `2026-09-21T00:09:47.026184697Z`, RestartCount `0`.

The temporary project was then removed with the prescribed Compose command. Final
verification found zero containers for `serverfs_issue10_live`; production remained
running, and the production MCP health was `healthy` with `FailingStreak 0`.
