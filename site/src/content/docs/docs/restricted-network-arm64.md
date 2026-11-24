---
title: Restricted-network ARM64 Deployment
description: Prepare a version-matched Linux ARM64 image set for a restricted or offline host.
---

This guide applies to Linux ARM64 Docker deployments. ServerFS release workflows build amd64 and arm64 images. Select the actual target release: v0.25.0 is the stable release; do not assume v0.26.0 images are published before candidate release gates pass. Candidate testing uses the verified local candidate image instead.

## 1. Separate image acquisition from runtime networking

| Network path | Required when |
| --- | --- |
| Docker daemon → registry | Pulling any selected image |
| Host → source/dependency cache | Obtaining the checkout or installing a host Bridge |
| Connector → provider | Using that OpenAI or Cloudflare Connector |
| Authorization service → identity provider | Enabling OAuth |
| File ingress sidecar → allowed HTTPS source | Enabling file-parameter ingress |
| Host Agent → provider | Running a configured native Agent |

A file-only Core with direct HTTP needs neither Tunnel nor Agent credentials. A disconnected registry does not imply disconnected runtime traffic, and a proxy configured for one component does not configure the others.

Check the target using `uname -m`; a native ARM64 deployment expects `aarch64` or `arm64`. The downloading host may be a different architecture.

## 2. Choose the exact image

Get the version-matched checkout and a trusted exact image reference from the intended release or candidate build. Avoid `latest` and ambiguous development tags. The examples use shell variables; replace the placeholder with the actual verified, version-tagged image reference and use the same reference on both machines.

```bash
IMAGE='<exact-version-tagged-image-reference>'
ARCH=linux/arm64
docker pull --platform "$ARCH" "$IMAGE"
docker image inspect --format '{{.Os}}/{{.Architecture}} {{.Id}}' "$IMAGE"
```

For an already built, trusted local ARM64 candidate image, skip the pull above and inspect that exact local image instead. Require `linux/arm64`; record the expected image ID. Validate the source against the trusted release manifest/digest or your verified candidate build. Image architecture alone is not authenticity.

If the target can access GHCR through a proxy, configure the Docker daemon using the [official Docker daemon proxy guide](https://docs.docker.com/engine/daemon/proxy/) and execute the pull on the target. Docker daemon proxy configuration is host-level and outside ServerFS ownership. Otherwise acquire the image on a connected machine and transfer it using the next section.

## 3. Transfer images when the target cannot reach the registry

On the connected machine, after the ARM64 pull and inspection:

```bash
ARCHIVE=serverfs-core-linux-arm64.tar
docker image save --platform "$ARCH" -o "$ARCHIVE" "$IMAGE"
```

Use platform-selective save only if the installed Docker API supports it. Otherwise first ensure the local tag contains the intended ARM64 image, then use `docker save -o "$ARCHIVE" "$IMAGE"`. In either case create the checksum *after* the final archive:

```bash
sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"
docker image inspect --format '{{.Os}}/{{.Architecture}} {{.Id}}' "$IMAGE"
```

Use save/load, not export/import. Save/load retains the image/tag/layer model used by Compose. Transfer the tar, checksum, exact image reference and expected image ID through a trusted channel. The archive checksum checks transfer integrity; it does not by itself establish publisher authenticity.

On the target, set the same actual image reference and archive filename again:

```bash
IMAGE='<same-exact-version-tagged-image-reference>'
ARCHIVE=serverfs-core-linux-arm64.tar
sha256sum -c "$ARCHIVE.sha256" &&
  docker image load -i "$ARCHIVE" &&
  docker image inspect --format '{{.Os}}/{{.Architecture}} {{.Id}}' "$IMAGE"
```

Stop if the checksum fails, the architecture is not `linux/arm64`, or the image ID does not match the trusted recorded ID. Do not accept accidental emulation as equivalent to native ARM64.

## 4. Start only the selected Core without pulling or rebuilding

Follow [Getting Started](../getting-started/) to prepare a new private env, actual workdir, nonroot UID/GID, unused port, Host allowlist and caller authentication. Set `SERVERFS_IMAGE` to the exact locally loaded tag. Do not add Tunnel or Bridge settings unless those components are required. From the matching checkout root, for a direct-HTTP file-only deployment:

```bash
ENV_FILE=/absolute/private/blue.env
core=(docker compose -p serverfs-blue --env-file "$ENV_FILE"
  -f compose.yml -f compose.http.yml
  -f deployment/linux/compose.publish-http.yml
  -f deployment/linux/compose.bridge-disabled.yml)

"${core[@]}" config --quiet
"${core[@]}" up -d --no-deps --no-build --pull never serverfs-mcp
"${core[@]}" ps serverfs-mcp
```

Fail closed on missing images or invalid configuration. With all optional capabilities disabled, verify authenticated MCP initialize, tools/list (11 tools), list_workdirs and safe read/negative write checks, as described in Getting Started. The same exact project, env and overlay selection applies to later operations.

## 5. Optional components on a restricted network

Prepare only what the chosen topology needs:

- Cloudflare: separately acquire the selected `CLOUDFLARED_IMAGE`, verify the ARM64 variant and transfer it. The runtime also needs its independent public Tunnel route and provider connectivity.
- OpenAI: separately acquire the verified `OPENAI_TUNNEL_IMAGE` that supports the named HTTP upstream and file-based credential options; verify ARM64 compatibility and transfer it. If runtime proxying is needed, use `SERVERFS_OPENAI_TUNNEL_USE_PROXY` with the documented `SERVERFS_PROXY_*` settings. This does not configure Docker daemon pulling or any other component's proxy. See [Tunnel Diagnostics](../tunnel-diagnostics/).
- OAuth and file ingress: their Linux services use the same `SERVERFS_IMAGE`; prepare their separate configuration, persisted auth state and actual required provider/fetch egress. A local image does not establish runtime external connectivity.
- Host Agent Bridge: prepare the version-matched Bridge source, uv cache and dependencies on the host. Use the documented offline installer option only when the required cache is available; provider executables, credentials and routing remain host-side. Repo Ops-only does not require a provider execution baseline.

Use the [Linux runbook](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/linux/README.md) for exact overlay/profile selection, signed attachment and scoped start/stop. Maintain the complete selected Compose arguments; use `--no-build --pull never` for offline starts, and start/check required dependencies according to their runbooks. Public Connector, OAuth and native Agent behavior requires its own live verification, not merely successful image transfer.
