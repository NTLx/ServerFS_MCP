---
title: Getting Started
description: Deploy and verify a minimal Core, then attach optional components.
---

## 1. Choose a deployment

Read [Architecture](../architecture/) first. For local MCP clients, native stdio on macOS or Windows does not require a persistent HTTP service. For persistent remote MCP, start with a file-only HTTP Core and add only the components you need.

| Platform | Start here |
| --- | --- |
| Linux amd64 / arm64 | Docker Compose; the direct HTTP example below |
| Apple Silicon macOS 27, APFS | [macOS native deployment](../macos-native/) |
| Windows 11 x64, NTFS | [Windows native deployment](../windows-native/) |

Use version-matched artifacts and source. Do not treat an unpublished build as a stable release or implicitly migrate an existing instance.

## 2. Linux: a file-only Core with direct HTTP

Install Docker and Compose v2.24+. Obtain a verified image for the intended version (or locally build under a distinct scratch tag). Use an actual existing directory and a nonroot container UID/GID that can read it. In a private mode-0600 env file outside Git and every exposed directory, configure the following example values, replacing placeholders and choosing a unique high-entropy bearer key locally.

```text
SERVERFS_IMAGE=<verified-version-matched-image>
SERVERFS_UID=1000
SERVERFS_GID=1000
SERVERFS_HTTP_HOST=0.0.0.0
SERVERFS_HTTP_PORT=8000
SERVERFS_HTTP_PATH=/mcp
SERVERFS_HTTP_PUBLISH_HOST=127.0.0.1
SERVERFS_HTTP_PUBLISH_PORT=18080
SERVERFS_HTTP_ALLOWED_HOSTS=serverfs-mcp:8000,127.0.0.1:18080
SERVERFS_HTTP_ALLOWED_ORIGINS=
SERVERFS_HTTP_API_KEY=<generate-a-unique-secret-locally>
SERVERFS_OAUTH_ENABLED=false
WORKDIR_01_ALIAS=docs
WORKDIR_01_PATH=/srv/serverfs-blue/docs
WORKDIR_01_READ_ONLY=true
SERVERFS_AGENT_BRIDGE_ENABLED=false
SERVERFS_AGENT_MODE=disabled
SERVERFS_AGENT_RUNTIMES=
WORKDIR_01_AGENT_MODE=disabled
WORKDIR_01_REPO_OPS_ENABLED=false
CONTROL_PLANE_API_KEY=
CONTROL_PLANE_TUNNEL_ID=
```

Do not expose the literal example directory or credentials automatically. Preserve unused workdir slots as disabled, read-only placeholders. This example binds the container's port 8000 to host loopback port 18080; keep them distinct. The explicit allowlist includes the internal and published authorities.

In Bash from the version-matched checkout root:

```bash
ENV_FILE=/absolute/private/blue.env
core=(docker compose -p serverfs-blue --env-file "$ENV_FILE"
  -f compose.yml -f compose.http.yml
  -f deployment/linux/compose.publish-http.yml
  -f deployment/linux/compose.bridge-disabled.yml)

"${core[@]}" config --quiet
# If using a published image not yet present:
"${core[@]}" pull serverfs-mcp
"${core[@]}" up -d --no-deps --no-build serverfs-mcp
"${core[@]}" ps serverfs-mcp
```

Skip the pull when using the already built local scratch image. Stop on any failed command. Use the same project, private env and overlay selection for subsequent operations. Do not print expanded Compose configuration.

## 3. Connect and verify

Configure your MCP client for Streamable HTTP at `http://127.0.0.1:18080/mcp`, with the configured bearer credential in the client's private authorization-header field. Client and Core must be on the same host for this loopback URL unless you explicitly configure another access path.

A running container or TCP healthcheck is not enough. Confirm initialize and tools/list, list_workdirs, a safe read of the selected directory, rejected writes to the read-only workdir, and denied requests with absent/wrong credentials. With all optional capabilities disabled the tool surface has 11 tools.

## 4. Extend the deployment only when needed

- Direct remote HTTP: configure the exact Host/Origin admission and an independently administered HTTPS reverse proxy; public access needs caller authentication. The [API Key](../api-key/) and [OAuth](../oauth/) guides explain the available modes.
- Tunnel-only or internal-only: select the internal-only overlay instead of the publication overlay. Add [Cloudflare](../cloudflare-tunnel/) or OpenAI using the provider-specific overlay/profile and separate credentials; neither provider is required for direct HTTP.
- Agent delegation or deterministic [Repo Ops](../repo-ops/): deploy an authorized [host Bridge](../agent-bridge/), with the exact Core identity and allowed workdir/method scope. Do not start a provider runtime just to enable Repo Ops.
- [Binary transfer](../binary-transfer/): enable the capability explicitly; optional file ingress is a separate sidecar path.

See the [complete Linux runbook](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/linux/README.md) for configuration, Connector-specific lifecycle, signed Bridge provision, rollback and retirement. If operating another platform, use its native runbook rather than translating these Docker commands.
