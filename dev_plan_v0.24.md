# ServerFS v0.24.0 — Linux Cloudflare Tunnel ingress

Status: **Linux release-candidate preparation; NOT published.** Current published stable release remains v0.23.3.

## Objective

Add a first-class, opt-in **Linux Docker Compose** Cloudflare Tunnel ingress for standards-compliant remote Streamable HTTP MCP clients, including ChatGPT when client authentication is compatible. Retain the existing OpenAI Secure MCP Tunnel, generic loopback HTTP, OAuth, and native Windows/macOS behavior.

## Architecture and trust boundaries

```text
Remote MCP client -- HTTPS:443 --> Cloudflare edge
                                    |
                            outbound-only tunnel
                                    |
Linux Docker: cloudflared -- private HTTP --> serverfs-mcp:8000/mcp
                 |                             |
         serverfs_internal              existing API Key/OAuth
                 |                      + workdir/Agent/Repo Ops policy
           tunnel_egress
```

- The origin runs on `serverfs_internal` with **no published host port** for this deployment.
- `cloudflared` attaches to both `serverfs_internal` and `tunnel_egress`; ServerFS never joins `tunnel_egress`. The connector cannot see workdir mounts, Agent sockets, or ServerFS HTTP credentials.
- Named **remotely managed** tunnels only. The public hostname and service mapping are administered in Cloudflare, not inferred by Compose. The service origin **must** be `http://serverfs-mcp:8000` when cloudflared shares the Docker network.
- A token file is an **operator-owned credential outside all ServerFS workdirs and outside the checkout**, bind-mounted read-only at a constant container path; never an environment variable, inline command argument or checked-in file. Match the token file owner to independently configurable `CLOUDFLARED_UID` / `CLOUDFLARED_GID` and set permission 0600. A non-root host operator can use their own numeric IDs without changing the ServerFS MCP container identity. Do not pass tokens into ChatGPT, Agent events or logs.
- Enforce `SERVERFS_HTTP_ALLOWED_HOSTS` with the exact public hostname and necessary Docker-internal authority; preserve DNS rebinding protection. For a browser Origin, allowlist only its exact origin if needed.
- Public access requires effective ServerFS API Key or OAuth, or an independently verified trusted upstream authentication boundary. Cloudflare Tunnel itself is not authentication.
- Query-key URLs have unavoidable upstream logging/exposure risks. Prefer OAuth or Bearer when the client supports them.
- No automatic Cloudflare API provisioning, new MCP tools, core protocol changes, OAuth changes, or production deployment in the Linux source implementation.

## Scope

1. `compose.cloudflare.yml`: standalone override of base Compose; disable the legacy OpenAI Tunnel unless explicitly requested, add a minimally privileged, restartable cloudflared container with token-file mounting and split private/egress networks.
2. `.env.example`: recommend the tested `cloudflare/cloudflared:2026.10.7` tag (overridable; no digest enforcement) and document the **absolute** external token-file path; retain `SERVERFS_HTTP_API_KEY` as the existing ingress auth switch.
3. A dedicated `docs/cloudflare-tunnel.md` operational runbook and bilingual website pages / quick start, configuration, architecture and README cross-links.
4. Linux tunnel references under the existing `serverfs-tunnel` Agent Skill; keep the package at seven skills.
5. Structural regression tests for network boundaries, compose profile behavior, token handling, and documentation/example contract; verify real MCP API Key/Host behavior with existing tests.

## Acceptance and gates

**Source/deployment:** `docker compose -f compose.yml -f compose.cloudflare.yml config --quiet` and `docker compose -f compose.yml -f compose.cloudflare.yml config --services`, plus security assertions over the rendered config (no host-published port, no OpenAI Tunnel by default, no token env leak, correct network placement). A dummy temporary token *path* may be used in offline render tests, not a real token.

**Regression:** `uv sync --frozen`, root Ruff lint/format, root pytest, existing base and HTTP Compose renders, scoped tests, Agent Skills validation/package tests, Astro website build, `git diff --check`. No release or deployed service changes.

**Live Cloudflare/ChatGPT acceptance (separate external gate, NOT implied by source tests):** verified remotely managed tunnel + approved unique hostname, valid DNS, connector `/ready`/actual connected status, malformed/absent credentials denied, protocol initialize and tool discovery, read-only workdir and hidden-credential denial, stateful MCP session, response streaming, long Agent tasks where enabled, large file transfer where enabled, restart/reconnect behavior and no credential leakage. Do not publish v0.24.0 as fully Cloudflare-live-validated before observing these checks.

**Platform boundary:** Linux amd64/arm64 Compose implementation only. Native Windows/macOS Cloudflare Tunnel service integration is out of scope. Preserve prior release claims and do not silently expand platform support.

## Release boundary

Develop on the VPS `ServerFS` checkout. Keep the published stable version v0.23.3 in live-facing metadata until the v0.24.0 release-candidate gate. Version changes, tag creation, push, GitHub Release and GHCR publication are separate, explicitly authorized steps under the repository dispatch-first release workflow. Never create Cloudflare routes or activate public access as a side-effect of repository tests.

## Cloudflare staging state (2026-11-06)

- Reuse the remotely managed Cloudflare Tunnel `serverfs-v024-dev` (ID `b8724123-72ed-4b97-9be3-0abf6700a77d`) in the operator's Cloudflare account. Do not create a duplicate.
- The approved staging-and-future-production HTTPS hostname is `vps-serverfs-mcp.ntlx.top`. The Cloudflare remote ingress routes to `http://serverfs-mcp:8000` and the fallback is `http_status:404`.
- **Live staging status (2026-11-06):** Cloudflare reports Tunnel `healthy` with one `linux_amd64` connector running `cloudflare/cloudflared:2026.10.7`. Proxied DNS CNAME `vps-serverfs-mcp.ntlx.top` points to this Tunnel; only the isolated `serverfs_cf_staging` project is exposed, not the unauthenticated production instance.
- **TLS and HTTP ingress verified:** the existing Universal SSL certificate covers `vps-serverfs-mcp.ntlx.top`. A real HTTPS request through Cloudflare returned `ssl_verify_result=0` and an unauthenticated HTTP 401. An authenticated public JSON-RPC `initialize` also succeeded. No paid certificate service is needed. Actual ChatGPT connector onboarding and read-only tool calls have subsequently been verified.
- The VPS Linux development environment operator `lx` has UID:GID `1000:1000`; the Cloudflare Compose connector can independently set `CLOUDFLARED_UID=1000`, `CLOUDFLARED_GID=1000` for a privately owned token file outside all workdirs. This does not change `SERVERFS_UID:GID`.
- The operator securely provisioned the Tunnel Token at `/home/lx/.config/serverfs-cloudflare/tunnel-token` outside all configured workdirs; existence, `0600`, UID/GID `1000:1000`, and absence of symlink were checked without reading the contents. The staging-only API Key likewise remains in a private `0600` environment file. No credential has been copied into Git, chat messages or Agent task text. The ChatGPT connector is configured by the operator with a credential-bearing URL; query-string logging/exposure risks must be considered before production use.

### Isolated VPS staging acceptance — 2026-11-06

- Production Compose project `serverfs_mcp` runs v0.23.3 with OpenAI Tunnel and file ingress; its Streamable HTTP API Key and OAuth are **not enabled**. Never attach Cloudflare publicly to this running instance without an independent admission design.
- Separate project `serverfs_cf_staging` uses `compose.yml + compose.cloudflare.yml` and an external, operator-owned `0600` environment file at `/home/lx/.config/serverfs-cloudflare/staging.env`. Its high-entropy API Key was generated on the VPS and must not be printed or transported through Agent prompts or workdir files.
- Only an isolated read-only fixture at `/home/lx/.local/share/serverfs-cloudflare-staging/readonly` is configured (remaining 15 slots disabled); Agent, Repo Ops, file ingress and OAuth are disabled. Compose render confirms no public host port, no production network joining, and isolated network names.
- Staging `serverfs-mcp` container was started as `serverfs-mcp:dev` in `serverfs_cf_staging`; it remains healthy. The `cloudflared` service is running in the same isolated project, with its own `tunnel_egress` network and no host-published port. Proxied DNS is active. Existing production ServerFS/OpenAI Tunnel/file ingress containers remained healthy and were not recreated by these operations.
- **Actual private HTTP authentication passed:** `initialize` without credentials -> 401, incorrect Bearer -> 401, correct Bearer -> 200 plus `Mcp-Session-Id` returned; requests used `Host: vps-serverfs-mcp.ntlx.top`.
- **Real public Streamable HTTP / JSON-RPC acceptance PASSED:** `initialize` with `Mcp-Session-Id`, `tools/list`, `tools/call` for `list_workdirs` (exactly one read-only `staging_fixture`) and `read_text_file` on the harmless fixture; attempted write to the read-only workdir and relative path traversal were denied. A separate SDK-client test raised `TypeError` inside `ExceptionGroup` during `Client.__aenter__`, so that particular SDK path remains unverified; wire-protocol acceptance is independently proven.
- **Compatibility observation:** unauthenticated public MCP requests made with the regular MCP test User-Agent returned 401, and authenticated requests with that identifier returned 200. Requests with `Python-urllib/3` returned 403 in separate probes; the exact enforcement layer has not been isolated and must not be guessed. Do not weaken zone-wide Cloudflare security rules to suppress this without evidence.
- **Connector recovery PASSED:** a controlled restart of only `serverfs_cf_staging-cloudflared-1` produced a new Cloudflare Connector connection ID and restored Tunnel status `healthy`; the public HTTPS TLS check and no-credential 401 succeeded afterward. Staging MCP and all production container IDs were unchanged.
- **Direct ChatGPT MCP acceptance PASSED:** ChatGPT's newly connected `vps-serverfs` client directly invoked `list_workdirs`, `list_directory`, `stat_file`, `read_text_file`, `find_files`, and `search_text` against the isolated `staging_fixture`. The only file was the benign 38-byte `hello.txt`. Attempts to read `.env` and escape the workdir returned `HIDDEN_PATH_NOT_ALLOWED` and `PATH_OUTSIDE_WORKDIR`; a missing file returned `PATH_NOT_FOUND`. No mutation tools, Agent capabilities, Repo Ops or binary transfers were enabled.
- **Public SSE long-connection acceptance PASSED:** after `initialize` + `notifications/initialized`, the public `GET /mcp` returned HTTP 200 with `Content-Type: text/event-stream`. The stream stayed open for at least 65s, yielded an event or heartbeat, and three concurrent `tools/list` requests in the same MCP session passed. The prior 15-second timeout was caused by a too-short probe window; a subsequent probe that parsed only JSON also failed incorrectly because successful POST `initialize` responses use SSE in this deployment. Both test-harness mistakes are resolved, not product failures.
- **Existing MCP session continuity across Connector restart PASSED:** initialize + `tools/list` succeeded before a one-time restart of only `serverfs_cf_staging-cloudflared-1`; the **same** MCP Session ID completed `tools/list` again on the second bounded retry without reinitialization. The staging MCP and all production container IDs remained unchanged. This does not guarantee lossless replay of events that were in flight during a restart.
- **Runtime Docker security review PASSED:** the Connector runs as UID/GID 1000:1000 with a read-only root, all Linux capabilities dropped, `no-new-privileges`, a single readonly external Tunnel Token mount, no MCP API Key in its environment and no host port publication. The staging MCP runs on the expected `serverfs_internal` and `serverfs_file_ingress` networks (both Docker `internal:true`), does not join `tunnel_egress`, and all its workdir bind mounts are read-only. Both environments retain private mode-0600 credentials. Connector/production network isolation was validated. A first audit incorrectly expected exactly one MCP network; the corrected audit verified both intentional private networks without modifying deployment architecture.
- **Remaining release gates:** source-wide regression and bilingual website/Skill gates; release metadata and CI verification. For this isolated staging configuration, Agent task, writable workdir, and binary transfer tests are **not applicable** because those capabilities are disabled. Do not infer production capability acceptance from staging evidence; do not publish v0.24.0 before the release workflow is explicitly authorized.
- Do not run plain `docker compose -f compose.yml -f compose.cloudflare.yml up -d` from the production repository context. Always specify `-p serverfs_cf_staging --env-file /home/lx/.config/serverfs-cloudflare/staging.env`; never overwrite production `.env` or recreate the `serverfs_mcp` project.


