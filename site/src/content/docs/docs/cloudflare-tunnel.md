---
title: Cloudflare Tunnel
description: Optional outbound-only Linux Docker MCP ingress (released in v0.24.0).
---

> **Release status:** Linux Compose Cloudflare Tunnel was **published in v0.24.0**. Native macOS Cloudflare shipped in v0.24.1 for Apple Silicon/macOS 27. Native Windows Cloudflare is implemented in the v0.24.2 candidate but remains **unreleased pending exact-SHA and public MCP acceptance**. Unlike macOS, Windows uses a single current-user Task Scheduler owner; do not switch the live OpenAI provider. See the [Windows native guide](../windows-native/) and [Windows Cloudflare runbook](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/windows-cloudflare-tunnel.md). Existing OpenAI Secure MCP Tunnel and Agent Bridge are preserved. Staging evidence and operator steps are documented in the [macOS Native guide](../macos-native/).

Cloudflare Tunnel is an alternative to the OpenAI Secure MCP Tunnel and the generic HTTP reverse-proxy deployment. It publishes the existing Streamable HTTP `/mcp` endpoint via a Cloudflare-managed hostname **without opening any inbound port on the server**. It does not change ServerFS workdir policy or MCP tools and is **not** MCP caller authentication.

## Data path

```text
MCP client -- HTTPS 443 --> Cloudflare edge
                                |
                          outbound tunnel
                                |
     cloudflared container -----+----- tunnel_egress
              |
       serverfs_internal
              |
     serverfs-mcp:8000/mcp
     (API Key / OAuth + existing workdir policy)
```

Unlike host-managed cloudflared, the Docker connector must use `http://serverfs-mcp:8000`, not localhost, as its origin. The MCP container also belongs to the existing `serverfs_file_ingress` Docker network; **both MCP networks are `internal: true`**. Only `cloudflared` reaches `tunnel_egress`. The connector never joins `serverfs_file_ingress`, which does not itself enable the optional file-ingress service.

## Setup (Linux Compose)

1. Create or reuse a remotely managed named Cloudflare Tunnel. The current VPS test Tunnel is `serverfs-v024-dev` and its selected hostname is `vps-serverfs-mcp.ntlx.top`, with HTTP origin `http://serverfs-mcp:8000`. Do not create a duplicate. Other deployments use their own operator-managed hostname.
2. Write the **Tunnel Token** into a 0600 file **outside the repository and every configured workdir**, readable by configured `CLOUDFLARED_UID:CLOUDFLARED_GID` (default `10001:10001`). To keep a private 0600 file owned by the non-root host operator, set `CLOUDFLARED_UID` and `CLOUDFLARED_GID` to the output of `id -u` and `id -g` respectively; these do not affect the ServerFS MCP UID/GID. Put only the absolute file path in ignored `.env` as `CLOUDFLARE_TUNNEL_TOKEN_FILE`.
3. Add the external hostname to `SERVERFS_HTTP_ALLOWED_HOSTS`, retaining the internal `serverfs-mcp:8000` authority. Configure the existing `SERVERFS_HTTP_API_KEY` or OAuth 2.1 separately; the Tunnel Token never authenticates MCP clients. The tested image tag `cloudflare/cloudflared:2026.10.7` is recommended but overridable; no digest locking is required.
4. Render before any launch:

   ```sh
   docker compose -f compose.yml -f compose.cloudflare.yml config --quiet
   ```

5. For a dedicated **new** deployment, first verify its unique Compose project, private `--env-file`, complete overlay set, API Key/OAuth admission and exact workdir mounts. Then follow the [full Linux runbook](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/cloudflare-tunnel.md) to start that isolated project. **Do not copy a generic `docker compose up` into an existing production checkout.**

Preserve any existing Agent / Repo Ops / OAuth Compose overlays. This shape publishes no host ports; OpenAI Tunnel remains disabled unless explicitly profiled. Never paste tokens into chat, logs or source control.

**On the current VPS, do not use the generic startup command against the live `serverfs_mcp` project.** Its production API Key/OAuth are not enabled. An isolated, authenticated `serverfs_cf_staging` project is already running with a single read-only fixture and no public port. Missing/incorrect credentials returned 401 and valid `initialize` returned 200 plus a session ID. The dedicated connector is now active and Cloudflare reports the Tunnel as healthy. Proxied DNS and public HTTPS TLS were verified. Public `initialize`, `tools/list`, read-only workdir discovery and harmless file read passed, as did read-only mutation and path-traversal denial. A staging-only Connector restart recovered to a healthy Cloudflare connection and public HTTPS/401 without touching the production containers. Direct ChatGPT tool calls have since succeeded, including file listing/reading and search on the read-only fixture. Sensitive `.env` paths and path traversal were denied. A public SSE GET stayed open for at least 65 seconds with an event/heartbeat and three same-session tool requests; the same MCP Session ID remained usable after a Connector restart without reinitialization. This does not validate production workdirs or lossless replay of in-flight SSE messages. See the full runbook for the explicit staging Compose file set and handoff.

## VPS staging HTTPS readiness

The intended retained address is `https://vps-serverfs-mcp.ntlx.top/mcp`. This single-level hostname is **covered by the currently active Cloudflare Universal SSL certificate `*.ntlx.top`**, so no additional certificate subscription is required. Public HTTPS and authenticated MCP JSON-RPC have now been tested successfully through the active Cloudflare Tunnel with an isolated read-only fixture. The private Tunnel Token is installed under the host operator's credentials, and proxied DNS is live. This is **staging acceptance**, not authorization to expose production workdirs or production ServerFS acceptance. Direct ChatGPT tool acceptance for the isolated fixture has succeeded. See the [full runbook](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/cloudflare-tunnel.md).

## Authentication and verification

Preferred remote client configuration is `https://your-host.example.com/mcp` with OAuth 2.1 or a Bearer API Key when available. The compatibility URL `/mcp?key=...` works in ServerFS but may expose credentials in upstream logs and should be avoided where possible.

Acceptance is layered: confirm Cloudflare/DNS/connector health, unauthenticated initialize 401, authorized MCP initialize and session, `tools/list`, `list_workdirs`, read-only and sensitive-path denials, streaming/reconnect and optional large transfers or Agent tasks. A successful tunnel connection is not sufficient evidence for client interoperability.

See the [full Linux operator runbook on GitHub](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/cloudflare-tunnel.md), [Static API Key](../api-key/) and [OAuth 2.1](../oauth/).
