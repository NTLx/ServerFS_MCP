# Cloudflare Tunnel on Linux / Docker Compose (v0.24.0 stable)

This is **not** the OpenAI Secure MCP Tunnel. Do not use OpenAI control-plane credentials, client bootstrap, or health commands here.

## Read before action

- `docs/cloudflare-tunnel.md` — canonical operator instructions and live-acceptance evidence requirements.
- `compose.cloudflare.yml`, `compose.yml` and `.env.example` — actual deployment semantics.
- `docs/api-key.md` / `docs/oauth.md` — caller admission and its limitations.
- Cloudflare documentation for the current token-file, routing and networking behavior.

## Steps

1. Verify this is a Linux Compose installation. Locate the deployment and enumerate its **active** Compose file set without revealing secrets. Confirm the requested Cloudflare Tunnel is separate from any live OpenAI Tunnel or host-managed cloudflared.
2. Confirm the Cloudflare account/Zone/Tunnel and intended hostname by read-only account API or operator evidence before provisioning. Changes to DNS/public routes require explicit scope and must never overwrite an unrelated hostname.
3. Keep the remotely-managed Tunnel Token outside the checkout and all ServerFS workdirs. Store only its absolute path under `CLOUDFLARE_TUNNEL_TOKEN_FILE`; the file must be mode 0600 and readable by configured `CLOUDFLARED_UID` (independent of `SERVERFS_UID`). Never emit the token.
4. Bind the connector only to `serverfs_internal` and `tunnel_egress`. The MCP service also uses its existing `serverfs_file_ingress` network, which remains `internal:true`; do not attach MCP to `tunnel_egress`. Use Cloudflare service `http://serverfs-mcp:8000` (or configured container port), **not** 127.0.0.1, when both services run as containers.
5. Require exact ServerFS public Host allowlist and independent ServerFS API Key/OAuth. Prefer OAuth/Bearer; query-key compatibility has URL-leakage risks. Do not disable DNS rebinding or log full URLs.
6. For an existing Agent/Repo Ops deployment, preserve the same Compose overlay chain and service mounts; only create/recreate the connector and necessary MCP container when authorized.
7. Validate each layer: safe Compose render, Cloudflare-connected connector, DNS/HTTPS, valid MCP initialize (JSON or SSE response), `notifications/initialized` before persistent GET SSE, session/tools, read-only/hidden-deny policy, long-running streaming and same-session recovery after an isolated Connector restart. Cloudflare connected status alone is insufficient. Record unverified portions explicitly.
8. Record the exact scope of client E2E evidence: the VPS isolated read-only staging connection has now passed direct ChatGPT tool calls, SSE hold and same-session reconnect. Do **not** generalize this result to production workdirs, Agent/Repo Ops, macOS/Windows native runtimes, or a published v0.24.0 release.

## Stop conditions

- No credential-safe external file path or incorrect file ownership/mode.
- No independent public caller authentication.
- Ambiguous Tunnel ownership, hostname conflict, unauthorized Cloudflare API write or unverified account.
- Deployment would expose an MCP port publicly, remove an Agent/Repo Ops overlay, or relax Host/Origin policy.
- Only an OpenAI Tunnel install is available and no Cloudflare mode was requested.
