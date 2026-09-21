# v0.4 Phase E acceptance — 2026-09-21

## Scope

Phase E validates the v0.4 filesystem/binary/Agent capability matrix through real
Streamable HTTP MCP clients and through the real OpenAI Secure MCP Tunnel. Production
was not restarted, recreated, or modified during these acceptance runs.

## E1 — disposable container matrix

A fresh image from v0.4-dev HEAD was exercised with isolated Compose projects and the
disposable `agent-e2e` workdir.

| Effective capabilities | tools/list |
| --- | ---: |
| filesystem only | 11 |
| filesystem + binary | 13 |
| filesystem + Agent | 19 |
| filesystem + binary + Agent | 21 |

Binary transfer round trips were performed through the real Streamable HTTP MCP
protocol, not by calling implementation helpers directly.

- PNG-like payload: 19 raw bytes; upload/download exact; SHA-256
  `4e42a25d57b426e30549cb1ad9c63dc183fcfae0a57cebf3c2703bece5d4e739`.
- ZIP-like payload: 17 raw bytes; upload/download exact; SHA-256
  `e0da4a26686c1e9412940ad369dd8ebbfb78c88b81bba54dfc6fb27c136da1f0`.
- Revision-guarded PNG overwrite: previous revision
  `v1:425e797bab641944`, new revision `v1:f2017dbc6b3ee3c9`; replacement bytes
  downloaded exactly with SHA-256
  `35072afbf7d571c15376b9ca8af4a1cf39d459aefcd33e13478266782d768b03`.
- Test files were deleted through revision-guarded deletion and the disposable
  workdir was empty afterwards.

Policy precedence was also exercised through the public MCP surface:

- global binary off + workdir explicit on => 13 tools and
  `binary_transfer=true`;
- global binary on + workdir explicit off => 11 tools and
  `binary_transfer=false`.

The production MCP container ID, StartedAt, RestartCount=0, running state and healthy
state were unchanged across E1. The root engineering gate at this point was
754 passed; Ruff, Ruff format, Compose config, and scratch build also passed.

## E2 — real ChatGPT -> OpenAI Secure MCP Tunnel -> v0.4

A separate 21-tool v0.4 Compose project (`serverfs_v04_e2`) was connected to the
real OpenAI Secure MCP Tunnel using the existing tunnel credentials. Its only
workdir was the disposable `agent-e2e`, with binary transfer enabled and Agent mode
`workspace-write`.

Before external probing, an internal MCP client confirmed:

- `tools/list` = 21;
- `agent-e2e` = read-write;
- `binary_transfer=true`;
- `agent_mode=workspace-write`.

After `E2_TEMP_READY`, the current ChatGPT conversation issued eight real
`list_workdirs` connector calls. Dispatcher routing alternated between the existing
production v0.3.1 tunnel target and the temporary v0.4 target. The v0.4 responses were
unambiguous because they returned only `agent-e2e` and included the v0.4 capability
fields.

Log-side cross-check found four successful temporary-v0.4 `list_workdirs` dispatches,
with Tunnel forwarding and HTTP 200. In the same probe window:

- HTTP 421: 0;
- HTTP 403: 0;
- no Host rejection;
- no Origin rejection;
- no DNS-rebinding rejection;
- no MCP session error.

This proves the real transport path:

`ChatGPT -> OpenAI Secure MCP Tunnel -> protected v0.4 MCP -> tool dispatch`.

The temporary E2 project was then removed. Its containers and network no longer exist,
`agent-e2e` is empty, and production container identity/start/restart/health remained
unchanged.

## Current ChatGPT tool-discovery limitation

The current conversation's connector schema remains the previously discovered 19-tool
surface. Although real connector traffic successfully reached the temporary 21-tool
v0.4 service, the conversation did not dynamically add `download_binary_file` or
`upload_binary_file` to its callable tool catalog.

Therefore the server-side and Tunnel-side binary capability is accepted, while the
final direct ChatGPT binary upload/download call is intentionally deferred until the
production connector is refreshed after the v0.4 release-candidate edge image is
deployed. This is a measured client/tool-discovery cache boundary, not a ServerFS
registration or transport failure.
