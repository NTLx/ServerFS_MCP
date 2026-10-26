---
title: Getting Started
description: Deploy ServerFS MCP with standard Streamable HTTP; add the OpenAI Secure MCP Tunnel only when needed.
---

This page covers the Linux/Docker deployment. For native deployments, use [Windows Native](../windows-native/) or [macOS Native](../macos-native/) instead.

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

Configure at least one workdir in `.env`. Before starting, set the Host allowlist in `.env` for the default loopback endpoint:

```text
SERVERFS_HTTP_ALLOWED_HOSTS=serverfs-mcp:8000,127.0.0.1:8000,localhost:8000
```

An empty allowlist only permits the Docker-internal authority. Keep `serverfs-mcp:8000` for the Tunnel/internal path and explicitly add `127.0.0.1:8000` and `localhost:8000` for direct local clients. If you customize ports, use the actual published port for loopback authorities and the container HTTP port for `serverfs-mcp`. For external clients or a reverse proxy, add each exact Host authority received by ServerFS to the allowlist rather than disabling Host validation. Then start the standard Streamable HTTP endpoint:

```bash
docker compose pull
docker compose -f compose.yml -f compose.http.yml up -d serverfs-mcp
docker compose -f compose.yml -f compose.http.yml ps
```

The HTTP override publishes `127.0.0.1:8000/mcp` by default and keeps the OpenAI Tunnel disabled. The 11-tool filesystem surface remains read-only unless a workdir explicitly sets `WORKDIR_XX_READ_ONLY=false`. Before exposing ServerFS beyond loopback, configure the Host allowlist and terminate TLS at an explicit ingress boundary.

## Optional static API Key for remote HTTP

For a simple built-in shared-secret boundary, set:

```text
SERVERFS_HTTP_API_KEY=<long-random-secret>
```

Keep the same `compose.yml` + `compose.http.yml` deployment. Clients should prefer `Authorization: Bearer <key>`. ServerFS also accepts a DingTalk-style `https://mcp.example.com/mcp?key=<key>` URL for clients that only support a complete URL, but query credentials may be captured by upstream access logs. API Key mode is mutually exclusive with built-in OAuth. See [Static API Key](../api-key/).

## Optional OAuth 2.1 for remote HTTP

v0.16.0 can add OAuth to the generic Streamable HTTP deployment without changing local/native stdio:

Before starting, set `SERVERFS_OAUTH_ENABLED=true`, the canonical public MCP resource URL, the `serverfs-auth` public/issuer URL, an introspection secret, at least one provider and the exact stable-principal allowlist.

Before starting Compose, prepare the auth state directory for the configured `SERVERFS_UID:SERVERFS_GID`. With the default `10001:10001` and `SERVERFS_AUTH_STATE_HOST_DIR=./.serverfs-auth-state`, run from the checkout root:

```bash
sudo install -d -m 700 -o 10001 -g 10001 -- .serverfs-auth-state
```

Assigning ownership to `10001:10001` normally requires `sudo` (or an administrator); the container still runs as that non-root identity. If you customize `SERVERFS_UID`, `SERVERFS_GID` or `SERVERFS_AUTH_STATE_HOST_DIR` in `.env`, replace the numeric IDs and path above with those actual values. Compose reads `.env`; these values are not automatically exported into your shell.

For a current-user deployment, you may instead set `SERVERFS_UID` and `SERVERFS_GID` in `.env` to the outputs of `id -u` and `id -g`, provided that identity also suits the MCP workdir permissions. For a new directory owned by that user, no privilege is needed:

```bash
install -d -m 700 -- .serverfs-auth-state
```

Use the configured state path if customized. Mode `700` alone does not grant a different container UID write access; verify the directory owner matches the configured UID before starting. Keep the directory private; do not widen its permissions.


```bash
docker compose --env-file .env \
  -f compose.yml \
  -f compose.http.yml \
  -f compose.oauth.yml \
  up -d
```

Public deployments should expose the MCP and authorization-server loopback ports only through an HTTPS reverse proxy. See [OAuth 2.1](../oauth/) for provider callbacks, CIMD/DCR, backup/revocation and the OpenAI Tunnel compatibility boundary.

## Optional ChatGPT / OpenAI Tunnel deployment

Set `CONTROL_PLANE_TUNNEL_ID` and `CONTROL_PLANE_API_KEY` in `.env`, then use the historical base Compose path:

```bash
docker compose pull
docker compose up -d
docker compose ps
docker compose logs -f openai-tunnel
```

This remains a supported authenticated ingress adapter; it is no longer required for generic MCP clients. For operational troubleshooting, use [Tunnel Diagnostics](../tunnel-diagnostics/); container `Up` state alone is not proof of Tunnel readiness or control-plane continuity.

On Linux hosts with restricted registry/network access, see [Restricted-network ARM64 Deployment](../restricted-network-arm64/) for the separate Docker-daemon proxy and offline image-transfer paths.

For a least-privilege monitoring pattern that exposes generated host status rather than host authority, see [Read-only Host Observability](../host-observability/).

## Agent deployment

Agent support is deliberately an explicit overlay:

```bash
docker compose --env-file .env -f compose.yml -f compose.agent.yml up -d
uv run --frozen --no-sync python deployment/agent-bridge/verify_host.py --require-runtimes
```

Before enabling it, install and verify the host-side Agent Bridge as described in the repository's [Agent Bridge deployment guide](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/agent-bridge/README.md). Keep the Agent overlay on every later recreation; using base Compose alone removes the Agent socket/lock mounts. Source-checkout Python commands should run through the project uv environment rather than the host's unqualified `python3`.

## Optional Deterministic Repo Ops (v0.23.0)

Repo Ops is separate from Agent delegation but reuses the authenticated host Bridge transport and writer-lease infrastructure. On a writable Linux/Compose workdir, set:

```text
WORKDIR_01_REPO_OPS_ENABLED=true
WORKDIR_01_REPO_ACTIONS_JSON={...}
```

No Agent runtime needs to be enabled. v0.23.0 adds `inspect_repo`, `run_repo_action`, `commit_repo_changes`, and `push_repo`; named actions use operator-owned fixed argv plus mandatory SHA-256 integrity pins, and the caller never receives shell/argv/remote/ref/force control. Release acceptance covered Linux bootstrap/internal E2E, public ChatGPT MCP E2E, full root/Agent Bridge CI, MCP conformance, Windows/macOS regression, and the post-freeze site build/deploy. See [Deterministic Repo Ops](../repo-ops/) before enabling it.

### Optional Jev advisors

If you want advisory task preflight, runtime routing, and provider-approval context, add a TypeSafe API key to the existing untracked `.env` before installing/updating the Bridge:

```text
SERVERFS_JEV_API_KEY=<your key>
```

Leave it empty to keep the Agent Bridge completely Jev-free. No extra MCP container configuration or ChatGPT plugin refresh is required. See [Jev Advisors](../jev-advisors/) for details.

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

The sidecar has no workdir mounts, tunnel/OpenAI credentials, or published port. Generic hostname wildcards are not supported; see [Binary Transfer](../binary-transfer/) and [Security Model](../security/) for the file-ingress host policy and network boundary introduced in v0.5.0.

## Current stable release: v0.23.0

The published v0.23.0 release uses immutable tag `v0.23.0` and these GHCR stable tags:

```text
ghcr.io/ntlx/serverfs_mcp:latest
ghcr.io/ntlx/serverfs_mcp:0.23
ghcr.io/ntlx/serverfs_mcp:0.23.0
```

For production deployments, pin `0.23.0` rather than following `latest` or `edge`. The v0.23.0 GitHub Release includes the three Windows installation wheels (product + `serverfs-agent-bridge` + `serverfs-windows-native` abi3) and the skills-only portable Agent Plugin `serverfs-plugin-v0.23.0.zip`, with release evidence recorded in the [v0.23.0 release checklist](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.23.0-release-checklist-2026-10-26.md); see [Agent Skills](../agent-skills/). macOS uses the product and Bridge Python distributions directly, has no separate native filesystem wheel, and retains the small ServiceManagement companion introduced in v0.13.2 for persistent Tunnel/Bridge services. See [Windows Native](../windows-native/) and [macOS Native](../macos-native/).

For complete ACP setup, validation, task lifecycle, and troubleshooting, see the [ACP Guide](../acp/).
