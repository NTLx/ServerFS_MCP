---
title: Getting Started
description: Deploy ServerFS MCP with standard Streamable HTTP; add the OpenAI Secure MCP Tunnel only when needed.
---

This page covers the Linux/Docker deployment. For native deployments, use [Windows Native](./windows-native/) or [macOS Native](./macos-native/) instead.

## Prerequisites

- Linux server
- Docker + Docker Compose

An OpenAI Secure MCP Tunnel and Runtime API Key are optional and only needed for the ChatGPT Tunnel ingress path.

## Standard MCP deployment

```bash
git clone https://github.com/NTLx/ServerFS_MCP.git
cd ServerFS_MCP
cp .env.example .env
chmod 600 .env
```

Configure at least one workdir in `.env`, then start the standard Streamable HTTP endpoint:

```bash
docker compose pull
docker compose -f compose.yml -f compose.http.yml up -d serverfs-mcp
docker compose -f compose.yml -f compose.http.yml ps
```

The HTTP override publishes `127.0.0.1:8000/mcp` by default and keeps the OpenAI Tunnel disabled. The 11-tool filesystem surface remains read-only unless a workdir explicitly sets `WORKDIR_XX_READ_ONLY=false`. Before exposing ServerFS beyond loopback, configure the Host allowlist and terminate TLS/authentication at an explicit ingress boundary.

## Optional OAuth 2.1 for remote HTTP

v0.16.0 can add OAuth to the generic Streamable HTTP deployment without changing local/native stdio:

```bash
mkdir -p .serverfs-auth-state
chmod 700 .serverfs-auth-state

docker compose --env-file .env \
  -f compose.yml \
  -f compose.http.yml \
  -f compose.oauth.yml \
  up -d
```

Before starting, set `SERVERFS_OAUTH_ENABLED=true`, the canonical public MCP resource URL, the `serverfs-auth` public/issuer URL, an introspection secret, at least one provider and the exact stable-principal allowlist. Public deployments should expose the MCP and authorization-server loopback ports only through an HTTPS reverse proxy. See [OAuth 2.1](./oauth/) for provider callbacks, CIMD/DCR, backup/revocation and the OpenAI Tunnel compatibility boundary.

## Optional ChatGPT / OpenAI Tunnel deployment

Set `CONTROL_PLANE_TUNNEL_ID` and `CONTROL_PLANE_API_KEY` in `.env`, then use the historical base Compose path:

```bash
docker compose pull
docker compose up -d
docker compose ps
docker compose logs -f openai-tunnel
```

This remains a supported authenticated ingress adapter; it is no longer required for generic MCP clients.

## Agent deployment

Agent support is deliberately an explicit overlay:

```bash
docker compose --env-file .env -f compose.yml -f compose.agent.yml up -d
python3 deployment/agent-bridge/verify_host.py --require-runtimes
```

Before enabling it, install and verify the host-side Agent Bridge as described in the repository's [Agent Bridge deployment guide](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/agent-bridge/README.md). Keep the Agent overlay on every later recreation; using base Compose alone removes the Agent socket/lock mounts.

### Optional Jev advisors

If you want advisory task preflight, runtime routing, and provider-approval context, add a TypeSafe API key to the existing untracked `.env` before installing/updating the Bridge:

```text
SERVERFS_JEV_API_KEY=<your key>
```

Leave it empty to keep the Agent Bridge completely Jev-free. No extra MCP container configuration or ChatGPT plugin refresh is required. See [Jev Advisors](./jev-advisors/) for details.

## ChatGPT file-parameter ingress

Since v0.5.0, ServerFS has supported accepting a ChatGPT/OpenAI `file` parameter as the source for `upload_binary_file` without giving the main MCP container Internet egress. Binary transfer and file ingress are separate opt-ins. For the common ChatGPT path, set:

```text
SERVERFS_BINARY_TRANSFER_ENABLED=true
SERVERFS_FILE_INGRESS_ENABLED=true
SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS=true
```

Then start the isolated sidecar with the `file-ingress` profile:

```bash
docker compose --profile file-ingress pull
docker compose --profile file-ingress up -d
```

If Agent Bridge is also enabled, keep both the Agent overlay and the file-ingress profile in the same invocation:

```bash
docker compose --env-file .env --profile file-ingress -f compose.yml -f compose.agent.yml pull
docker compose --env-file .env --profile file-ingress -f compose.yml -f compose.agent.yml up -d
```

The sidecar has no workdir mounts, tunnel/OpenAI credentials, or published port. Generic hostname wildcards are not supported; see [Binary Transfer](./binary-transfer/) and [Security Model](./security/) for the file-ingress host policy and network boundary introduced in v0.5.0.

## Current stable release: v0.15.0

The v0.15.0 release uses immutable tag `v0.15.0` and publishes these stable GHCR tags:

```text
ghcr.io/ntlx/serverfs_mcp:latest
ghcr.io/ntlx/serverfs_mcp:0.15
ghcr.io/ntlx/serverfs_mcp:0.15.0
```

For production deployments, pin `0.15.0` rather than following `latest` or `edge`. The same tag carries the three Windows installation wheels (product + `serverfs-agent-bridge` + `serverfs-windows-native` abi3) as GitHub Release assets with recorded SHA-256 digests. macOS uses the product and Bridge Python distributions directly, has no separate native filesystem wheel, and retains the small ServiceManagement companion introduced in v0.13.2 for persistent Tunnel/Bridge services. See [Windows Native](./windows-native/) and [macOS Native](./macos-native/).
