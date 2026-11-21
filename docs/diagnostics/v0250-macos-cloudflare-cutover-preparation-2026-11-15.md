# ServerFS MCP macOS v0.25.0 Cloudflare cutover preparation — 2026-11-15

> Historical preparation-only snapshot. The stopped-pair state and proposed commands below were superseded by the [executed cutover](v0250-macos-cloudflare-cutover-2026-11-15.md), including migration of the repository-local rollback directory to private storage, and the [later Connector-only restart](v0250-macos-cloudflare-connector-restart-acceptance-2026-11-16.md). Do not reuse the old rollback path or replay these commands as current authorization. Current release/operational scope is governed by the [release checklist](../v0.25.0-release-checklist-2026-11-21.md) and [macOS runbook](../../deployment/macos/README.md).

## Readiness

**NOT READY for cutover acceptance.** The dirty current-source Core passed an
authenticated local MCP preflight without ServiceManagement registration, and
private rollback material is prepared. The Cloudflare API and ChatGPT connector
read-only checks were rejected by tool approval, so the current remote Tunnel
route and public MCP path were not independently reverified in this run.

Per the user's preparation-only correction, no further service operation was
performed after the current state was inventoried. This session had already
stopped the Cloudflare Connector and HTTP Core. Both are now unregistered and
not running; their independent-owner manifest entries remain intact. OpenAI
Tunnel and Agent Bridge remain running.

## Host and service evidence

- Host: Apple Silicon, macOS 27.2.1.
- OpenAI Tunnel: launchd `com.ntlx.serverfs.tunnel`, PID **1779**, enabled.
- Agent Bridge: launchd `com.ntlx.serverfs.bridge`, PID **1790**, enabled.
- HTTP Core: launchd `com.ntlx.serverfs.http-origin`, owner `independent-core`,
  manifest entry retained, **not registered**, no PID, no listener on 8765.
- Cloudflare Connector: launchd `com.ntlx.serverfs.cloudflare`, owner
  `independent-connector`, manifest entry retained, **not registered**, no PID.
- Existing Core configuration is `/Users/lx/Projects/ServerFS_MCP/serverfs.toml`;
  Core port is **8765** and allowed public hostname is
  `macbook-serverfs.ntlx.top`.
- Existing Connector token file path is
  `/Users/lx/Library/Application Support/ServerFS/credentials/cloudflare/macbook-serverfs.token`.
  The file was not copied into the backup; its bytes were not included in this
  report or tool output.
- The latest prior public baseline records Tunnel
  `1a19184a-741d-41de-a86f-c0f3119f95bb` routing
  `macbook-serverfs.ntlx.top` to `http://127.0.0.1:8765` with the matching Host
  override. That route was **not live-read in this run** because the Cloudflare
  API read was rejected. No DNS, WAF, Tunnel or hostname change was made here.

## Candidate and policy checks

- Current checkout is a dirty v0.25.0 candidate; installed project metadata is
  still **0.24.2**. The local preflight imported the current checkout source,
  so that metadata alone did not disqualify this candidate.
- With port **18765** confirmed unused, a temporary Core was started directly
  from the current source and stopped by its owning preflight process. It did
  not register or start an SMAppService job and did not touch the production
  manifest.
- Local anonymous and incorrect credentials returned **401**. Authenticated
  MCP initialize, `tools/list` (23 tools), `list_workdirs` (two configured
  read-write workdirs), and `list_agent_runtimes` passed. Codex, Claude and
  Qoder were all reported available through the existing Bridge. No workdir
  files were changed.
- The installed native Service Manager app signature and opt-in agents
  verified. Component-scoped `service start/stop --component {core,cloudflare}`
  syntax is present. The normal `core stage` and `cloudflare stage` paths refuse
  to replace already registered Core/Connector entries; this is why a
  production replacement requires explicit stop and unstage steps.
- Before service stopping, PIDs were Core **99271** and Connector **99279**;
  Tunnel **1779** and Bridge **1790** were unchanged. After the two scoped stop
  operations, only Tunnel **1779** and Bridge **1790** remain enabled. Current
  8765 and 18765 listeners: none.

## Private rollback material

Prepared at:

`/Users/lx/Projects/ServerFS_MCP/deployment/macos/.private-v0250-cutover-9a4dedacbde3`

The directory is mode **0700**; all files are mode **0600**. It contains only
the old Core/Cloudflare service definitions (with the token path, not bytes),
the original private HTTP-origin environment snapshot, a CLI-compatible
rollback env file, and `rollback-plan.txt`. The env roundtrip was verified.
Do not commit or publish this directory.

## Smallest next-stage action

The current old pair is stopped but still staged in the manifest. In the
separate authorized cutover task, keep the hostname, port, token path, Tunnel
route and DNS unchanged. Use the existing component-scoped lifecycle only:

```bash
.venv/bin/serverfs service cloudflare unstage
.venv/bin/serverfs service core unstage
.venv/bin/serverfs service core stage \
  --config /Users/lx/Projects/ServerFS_MCP/serverfs.toml \
  --env-file /Users/lx/Projects/ServerFS_MCP/deployment/macos/.private-v0250-cutover-9a4dedacbde3/rollback-core-http.env \
  --http-port 8765 --hostname macbook-serverfs.ntlx.top
.venv/bin/serverfs service cloudflare stage \
  --config /Users/lx/Projects/ServerFS_MCP/serverfs.toml \
  --cloudflared /Users/lx/Library/Application\ Support/ServerFS/bin/cloudflared-2026.10.7-darwin-arm64/cloudflared \
  --token-file /Users/lx/Library/Application\ Support/ServerFS/credentials/cloudflare/macbook-serverfs.token \
  --hostname macbook-serverfs.ntlx.top
.venv/bin/serverfs service start --component core
.venv/bin/serverfs service start --component cloudflare
```

Verify local authenticated MCP and both process owners, then verify the public
Cloudflare connector before declaring cutover accepted. If the prior pair must
be restored, follow the exact sequence in the private `rollback-plan.txt`.
That rollback sequence was prepared and env-validated, but not executed.

For immediate restoration of the currently stopped pair without replacing its
staged definitions, the two scoped start commands are:

```bash
.venv/bin/serverfs service start --component core
.venv/bin/serverfs service start --component cloudflare
```

## Verification limits

- Cloudflare API route inspection and both connected ChatGPT MCP read-only
  checks returned `user rejected MCP tool call`; they were not retried through
  another channel.
- The public baseline reports that Mac-originated HTTPS can receive Cloudflare
  403/1010. That response does not verify origin health or public MCP.
- No v0.25 Core/Connector was staged or registered in this run; no public route
  or DNS was changed. No commit, push, tag, or release was made.
