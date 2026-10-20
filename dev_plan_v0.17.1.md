# ServerFS MCP v0.17.1 Maintenance Release Plan

Status: released — v0.17.1 maintenance release complete; issues #15/#16/#17 closed as completed
Baseline: v0.17.0 (`e63f4c676a88d594d20adafba97b6ef27ca4a012`)
Scope: Linux/Docker operational reliability and documentation only
Issues: #15, #16, #17

## 1. Objective

v0.17.1 is a narrow maintenance release. It closes three operational/documentation gaps reported against the Linux/Docker deployment without changing the MCP tool surface, workdir authorization model, Agent Bridge contract, OAuth/API-Key semantics, native Windows behavior, or native macOS behavior.

Success means:

1. `openai-tunnel` has an explicit readiness-oriented Compose healthcheck rather than relying on container `Up` state.
2. Operators have a current troubleshooting path that distinguishes process liveness, tunnel readiness, MCP evidence, control-plane polling, and response delivery.
3. A least-privilege read-only host-observability recipe is documented without granting shell or Agent authority.
4. ARM64 operators on restricted networks have a documented online-proxy path and an offline image-transfer path, including architecture verification and exact image pinning.
5. English and Simplified-Chinese site documentation remain semantically aligned.
6. Applicable root/deployment tests and the static site build pass before the issues are closed.

## 2. Non-goals

- No new MCP tools or schema changes.
- No generic shell/command execution capability.
- No changes to Agent Bridge runtime/provider behavior.
- No change to OAuth 2.1 or static API Key behavior.
- No Windows/macOS lifecycle changes.
- No automatic Docker-daemon proxy mutation by ServerFS.
- No deployment/restart of the separate Pi1 `serverfs-mcp` runtime workdir as part of development.

## 3. Issue #15 — Tunnel readiness and control-plane diagnostics

### Problem

`docker compose ps` can report the Tunnel container as running while the tunnel is not operationally ready. The current Compose service exposes the tunnel health listener but has no healthcheck. Current user documentation also lacks a complete diagnostic ladder.

### Implementation

- Add an `openai-tunnel` Compose healthcheck using the tunnel-client's own supported health command. The check derives the port from `HEALTH_LISTEN_ADDR` and runs:

  `tunnel-client health --port <configured-port> --require-control-plane-poll`

  The upstream command probes `/healthz` and `/readyz`, returns non-zero when the daemon is not ready, and with `--require-control-plane-poll` requires evidence that at least one control-plane poll succeeded after startup. This is preferable to treating `/healthz`, local readiness, or container liveness alone as operational readiness.
- Keep the health listener internal to the Docker network; do not publish the diagnostic port to the host.
- Add a regression test that locks the healthcheck contract into `compose.yml`.
- Add an English/Chinese `Tunnel diagnostics` guide that distinguishes:
  1. container/process liveness;
  2. `/readyz` readiness;
  3. MCP observation/discovery;
  4. control-plane polling;
  5. response delivery and local queue/dispatcher state.
- Document `/health?details=true`, `/health/mcp`, `/health/control-plane`, `/health/response-delivery`, `tunnel-client health`, logs, and `doctor --explain` where applicable.
- Explicitly state that a healthy local readiness check is not a perpetual proof of remote control-plane continuity; component observations and logs remain necessary for intermittent failures.

Acceptance:

- Compose renders successfully.
- Focused tunnel deployment tests pass.
- Documentation no longer presents `Up` alone as evidence that the ChatGPT Tunnel path is usable.

## 4. Issue #16 — Read-only host observability recipe

### Problem

ServerFS can safely expose a generated status directory as a read-only workdir, but there is no concrete recipe showing how to do that without Agent Bridge or shell authority.

### Implementation

Add English/Chinese documentation for this architecture:

```text
host collectors -> sanitized status directory -> read-only ServerFS workdir -> MCP client
```

The recipe will:

- use an allowlisted set of host facts such as uptime, memory, filesystem usage, selected container state and optional GPU summaries;
- avoid raw environment dumps, unrestricted logs, Docker inspect payloads, credentials and private application data;
- write files atomically into a dedicated directory;
- document permissions required for the default ServerFS container UID/GID;
- show a minimal periodic refresh example using a systemd timer, with cron as a simple alternative;
- keep `WORKDIR_XX_READ_ONLY=true` and require no Agent Bridge.

Acceptance:

- Recipe is usable as documentation only; it does not add host authority to ServerFS.
- Security/redaction boundary is explicit.

## 5. Issue #17 — Restricted-network ARM64 deployment

### Problem

ServerFS publishes `linux/arm64`, and Tunnel runtime proxying is already supported, but current documentation does not distinguish Docker daemon registry access from Tunnel control-plane proxying and does not document a safe offline ARM64 image-transfer flow.

### Implementation

Add English/Chinese documentation covering two supported operational patterns.

### Path A — Host can reach GHCR through a proxy

- Explain that `SERVERFS_OPENAI_TUNNEL_USE_PROXY` affects Tunnel control-plane egress only; it does not configure `dockerd` registry pulls.
- Link to Docker's daemon-proxy guidance rather than having ServerFS mutate daemon configuration.
- Pin the ServerFS image to an exact release tag in production.

### Path B — Target cannot reach GHCR

On a connected machine:

1. Pull the exact `linux/arm64` release image explicitly.
2. Inspect the image OS/architecture.
3. Save the image archive (optionally selecting `linux/arm64` where the local Docker API supports platform-selective save).
4. Transfer the archive using the operator's trusted mechanism.

On the target ARM64 host:

1. Verify `uname -m` is `aarch64`/`arm64` as expected.
2. Load the archive with `docker load`.
3. Inspect the loaded image and require `linux/arm64` before Compose startup.
4. Set `SERVERFS_IMAGE` to the exact loaded release tag and start Compose without relying on `latest`.

The guide will distinguish `docker save`/`docker load` from `docker export`/`docker import`; only the former preserves the image/tag/layer model expected here.

Acceptance:

- The guide makes architecture mismatch detectable before service startup.
- The guide does not imply that ServerFS proxy variables configure Docker daemon pulls.

## 6. Documentation/navigation changes

- Add the three operational guides to the Starlight sidebar under a dedicated Operations section.
- Cross-link them from Getting Started where users first encounter the relevant deployment path.
- Keep English and Simplified Chinese content aligned.
- Keep v0.17.0 marked as the current stable release until a v0.17.1 tag/release is actually published.

## 7. Validation gates

Run the smallest complete affected set:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
docker compose config --quiet
docker compose -f compose.yml -f compose.http.yml config --quiet
cd site
npm ci
npm run build
cd ..
git diff --check
```

A focused test run may be used during development. If a full-suite failure is caused by a measured validation-host prerequisite rather than repository changes, record that evidence explicitly rather than changing unrelated product code.

Validation evidence on Pi1 ARM64 for this implementation:

- `git diff --check`: PASS;
- `tests/test_tunnel_proxy_deployment.py`: **20 passed**;
- `ruff check .`: PASS;
- `ruff format --check .`: PASS after the one formatting-only correction;
- base Compose render: PASS;
- `compose.yml + compose.http.yml` render: PASS;
- Starlight/Astro production build: PASS; both English and Simplified-Chinese versions of all three new operations pages are present in `site/dist`;
- `site/package.json` and `site/package-lock.json` remain unmodified after `npm ci`;
- full root pytest: **PASS** with `PATH=/home/linuxbrew/.linuxbrew/bin:$PATH`; **1903 tests total: 1587 passed, 316 skipped, 0 failed in 125.11s**. Pi1's Homebrew-installed `rg` is `/home/linuxbrew/.linuxbrew/bin/rg`; adding Linuxbrew to the Agent subprocess PATH removes the earlier `rg` resolution failure and confirms there is no search-contract regression.

No Agent Bridge, Windows-native, or macOS-native acceptance is required because those boundaries are intentionally unchanged.

## 8. GitHub issue closeout

Shared implementation commit: `b006a2ab0c514e3ec456912c08ae8a476f84e3fc` on `main`.

GitHub closeout is complete:

- #15: resolution reply posted; closed as `completed`.
- #16: resolution reply posted; closed as `completed`.
- #17: resolution reply posted; closed as `completed`.

All three issues were closed only after the implementation was pushed to the shared branch.

## 9. Release boundary

v0.17.1 is the current stable maintenance release. Its immutable `v0.17.1` tag and GitHub Release are bound to the validated release commit under the normal release workflow. The stable tag must never be moved or recreated after publication.
