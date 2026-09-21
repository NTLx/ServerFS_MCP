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

## Published images

Stable releases are published to GHCR:

```text
ghcr.io/ntlx/serverfs_mcp:latest
ghcr.io/ntlx/serverfs_mcp:0.4
ghcr.io/ntlx/serverfs_mcp:0.4.0
```

Use a pinned release tag for production deployments.
