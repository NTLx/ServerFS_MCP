---
title: Getting Started
description: Deploy ServerFS MCP with Docker Compose and the OpenAI Secure MCP Tunnel.
---

## Prerequisites

- Linux server
- Docker + Docker Compose
- An OpenAI Secure MCP Tunnel
- A tunnel Runtime API Key with the permissions required by the official tunnel client

## Base deployment

```bash
git clone https://github.com/NTLx/ServerFS_MCP.git
cd ServerFS_MCP
cp .env.example .env
chmod 600 .env
```

Configure at least one workdir and the tunnel credentials in `.env`, then:

```bash
docker compose pull
docker compose up -d
docker compose ps
docker compose logs -f openai-tunnel
```

The base deployment exposes the 11 filesystem tools and remains read-only unless a workdir explicitly sets `WORKDIR_XX_READ_ONLY=false`.

## Agent deployment

Agent support is deliberately an explicit overlay:

```bash
docker compose -f compose.yml -f compose.agent.yml up -d
```

Before enabling it, install and verify the host-side Agent Bridge as described in the repository's [Agent Bridge deployment guide](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/agent-bridge/README.md).

## ChatGPT file-parameter ingress

v0.5.0 can accept a ChatGPT/OpenAI `file` parameter as the source for `upload_binary_file` without giving the main MCP container Internet egress. Binary transfer and file ingress are separate opt-ins. For the common ChatGPT path, set:

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
docker compose --profile file-ingress -f compose.yml -f compose.agent.yml pull
docker compose --profile file-ingress -f compose.yml -f compose.agent.yml up -d
```

The sidecar has no workdir mounts, tunnel/OpenAI credentials, or published port. Generic hostname wildcards are not supported; see [Binary Transfer](./binary-transfer/) and [Security Model](./security/) for the v0.5.0 host policy and network boundary.

## Release image tags

The `v0.5.0` tag is the publication trigger for these GHCR release tags; before the tag exists, use `edge` for release-candidate testing:

```text
ghcr.io/ntlx/serverfs_mcp:latest
ghcr.io/ntlx/serverfs_mcp:0.5
ghcr.io/ntlx/serverfs_mcp:0.5.0
```

After publication, use the pinned `0.5.0` tag for production deployments rather than `latest` or `edge`.
