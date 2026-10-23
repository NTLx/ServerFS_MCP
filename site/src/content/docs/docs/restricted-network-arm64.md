---
title: Restricted-network ARM64 Deployment
description: Deploy ServerFS on Linux ARM64 when GHCR or GitHub access requires a proxy or an offline image transfer.
---

ServerFS publishes `linux/arm64` images. On restricted networks, keep two different network paths separate:

1. **Docker daemon registry access** — used by `docker pull` / Compose image pulls from GHCR.
2. **Tunnel control-plane egress** — used by the optional `openai-tunnel` container after it has started.

`SERVERFS_OPENAI_TUNNEL_USE_PROXY` configures only the second path. It does **not** configure the Docker daemon and cannot make an otherwise blocked `docker pull` succeed.

## Confirm the target architecture

On the target host:

```bash
uname -m
```

For the supported Linux ARM64 path, expect `aarch64` or `arm64`. Do not infer image architecture from the machine that downloaded the image.

## Path A: Docker daemon can use an HTTP/HTTPS proxy

If the target can reach GHCR through a proxy, configure the Docker daemon using Docker's documented daemon-proxy mechanism. Docker Engine supports daemon proxy settings in `daemon.json` and, for systemd installations, service environment configuration.

Official reference: [Docker daemon proxy configuration](https://docs.docker.com/engine/daemon/proxy/).

This is host-level Docker configuration, not a ServerFS setting. ServerFS does not edit Docker daemon configuration automatically.

After the daemon proxy is working, pin an exact ServerFS release rather than `latest`:

```bash
VERSION=0.17.0
IMAGE="ghcr.io/ntlx/serverfs_mcp:${VERSION}"
docker pull --platform linux/arm64 "$IMAGE"
docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE"
```

The final command must report:

```text
linux/arm64
```

Then set the same exact image in `.env`:

```text
SERVERFS_IMAGE=ghcr.io/ntlx/serverfs_mcp:0.17.0
```

Use the exact currently published release version for your deployment; the version above is an example of explicit pinning.

### Tunnel proxy is separate

If image pulls work but the optional OpenAI Tunnel also needs a proxy, configure ServerFS's Tunnel-specific switch and shared proxy endpoint:

```text
SERVERFS_OPENAI_TUNNEL_USE_PROXY=true
SERVERFS_PROXY_HOST=proxy.example.internal
SERVERFS_PROXY_PORT=3128
```

The Tunnel setting affects control-plane egress after startup. It does not alter `dockerd`, registry authentication or image pulling. See [Tunnel Diagnostics](../tunnel-diagnostics/) for runtime checks.

## Path B: target cannot reach GHCR

Use a connected machine to obtain the exact ARM64 image, save it, transfer it through a trusted channel, and load it on the target.

### 1. Pull the ARM64 variant on the connected machine

```bash
VERSION=0.17.0
IMAGE="ghcr.io/ntlx/serverfs_mcp:${VERSION}"
docker pull --platform linux/arm64 "$IMAGE"
docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE"
```

Require `linux/arm64` before creating the archive.

### 2. Save the image

On a current Docker Engine/API that supports platform-selective save:

```bash
docker image save --platform linux/arm64 \
  -o "serverfs-mcp-${VERSION}-linux-arm64.tar" \
  "$IMAGE"
sha256sum "serverfs-mcp-${VERSION}-linux-arm64.tar" \
  > "serverfs-mcp-${VERSION}-linux-arm64.tar.sha256"
```

If your Docker API does not support `docker image save --platform`, ensure the local tag contains the intended ARM64 variant, then use ordinary `docker save`:

```bash
docker save -o "serverfs-mcp-${VERSION}-linux-arm64.tar" "$IMAGE"
```

Use `docker save` / `docker load`, **not** `docker export` / `docker import`. Save/load preserves the image/tag/layer model expected by Compose.

### 3. Transfer and verify

Copy both the tar archive and checksum using your trusted transfer mechanism. On the target:

```bash
sha256sum -c "serverfs-mcp-${VERSION}-linux-arm64.tar.sha256"
docker load -i "serverfs-mcp-${VERSION}-linux-arm64.tar"
docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE"
```

Do not continue unless the loaded image reports:

```text
linux/arm64
```

### 4. Pin Compose to the loaded image

In `.env`:

```text
SERVERFS_IMAGE=ghcr.io/ntlx/serverfs_mcp:0.17.0
```

Then render the deployment before starting it:

```bash
docker compose config --quiet
docker compose up -d
```

Because the exact tag is already present locally, Compose can use the loaded image. Avoid `latest` for offline/restricted deployments because it creates ambiguity about what was transferred.

## If you also use the OpenAI Tunnel

The base Compose deployment also needs the separately pinned `OPENAI_TUNNEL_IMAGE`. A target with no registry access must have that image available locally as well. Apply the same pattern: explicitly pull the required `linux/arm64` Tunnel image on a connected machine, verify its architecture, `docker save`, transfer, `docker load`, and keep `OPENAI_TUNNEL_IMAGE` pinned to the tested tag from `.env.example`.

If you use only generic Streamable HTTP (`compose.yml` + `compose.http.yml`), the OpenAI Tunnel image is not required.

## Pre-start checklist

Before starting a restricted-network ARM64 deployment, verify:

```bash
uname -m
docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE"
docker compose config --quiet
```

Expected architecture: `linux/arm64`.

This catches the common failure mode where an amd64 image was downloaded or transferred accidentally. QEMU/emulation may make some mismatches appear to run, but ServerFS documentation does not treat accidental emulation as equivalent to a verified native ARM64 deployment.
