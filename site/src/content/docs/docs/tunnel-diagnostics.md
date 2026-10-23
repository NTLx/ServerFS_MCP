---
title: Tunnel Diagnostics
description: Distinguish OpenAI Tunnel liveness, readiness, MCP evidence, control-plane polling and response delivery.
---

The OpenAI Secure MCP Tunnel is an optional ChatGPT ingress adapter. A running container is only the first layer of evidence: `docker compose ps` showing `Up` does **not** by itself prove that the Tunnel can currently poll the control plane or deliver MCP responses.

## Health model

Use these layers in order:

| Layer | Evidence | Meaning |
| --- | --- | --- |
| Container | `docker compose ps` | The container process exists. |
| Liveness | `/healthz` | The tunnel-client process is alive. |
| Readiness | `/readyz` or `tunnel-client health` | Startup readiness gates have passed. |
| MCP | `/health/mcp` | What the Tunnel has actually observed from the configured MCP target. |
| Control plane | `/health/control-plane` | Poll attempts, successes, waits, retries/backoff and bounded failure state. |
| Response delivery | `/health/response-delivery` | Whether completed MCP results are being accepted upstream. |
| Local execution | `/health/queue` and `/health/dispatcher` | Local backlog, backpressure and active operations. |

`/healthz` is intentionally weaker than `/readyz`. The detailed `/health/...` component routes are diagnostic snapshots: a component endpoint can return HTTP 200 while its JSON status is degraded, so do not use component HTTP status as a readiness gate.

## Compose healthcheck

ServerFS v0.20.0 configures the bundled `openai-tunnel` service with the tunnel-client's own probe:

```text
/usr/bin/tunnel-client health --port <configured-port> --require-control-plane-poll
```

The Compose check derives the port from `HEALTH_LISTEN_ADDR`. The tunnel-client probe checks liveness and readiness and, with `--require-control-plane-poll`, also requires evidence of at least one successful control-plane poll before reporting healthy. This distinguishes a merely running/local-ready Tunnel from one that has actually reached the control plane during startup.

The health listener remains internal to the container/network. ServerFS does not publish port 8080 to the host by default.

Check the current state with:

```bash
docker compose ps openai-tunnel
docker compose exec openai-tunnel /usr/bin/tunnel-client health --port 8080
docker compose exec openai-tunnel \
  /usr/bin/tunnel-client health --port 8080 --require-control-plane-poll --json
```

The control-plane requirement proves that at least one poll has succeeded since this daemon started. It is still historical startup evidence, not a perpetual guarantee of remote control-plane continuity. For intermittent failures after an earlier successful poll, inspect component state and logs.

## Read component diagnostics

The bundled image is Alpine-based, so its BusyBox `wget` can query the loopback-only health listener from inside the Tunnel container:

```bash
docker compose exec openai-tunnel \
  wget -qO- 'http://127.0.0.1:8080/health?details=true'

docker compose exec openai-tunnel \
  wget -qO- 'http://127.0.0.1:8080/health/mcp'

docker compose exec openai-tunnel \
  wget -qO- 'http://127.0.0.1:8080/health/control-plane'

docker compose exec openai-tunnel \
  wget -qO- 'http://127.0.0.1:8080/health/response-delivery'
```

Older tunnel-client releases may not expose every detailed component route. ServerFS v0.20.0 uses the repository-tested v0.0.17 baseline as its tested default unless `OPENAI_TUNNEL_IMAGE` is explicitly overridden; this is not a compatibility lock.

## Troubleshooting sequence

### 1. Container is not running

Start with:

```bash
docker compose ps openai-tunnel
docker compose logs --tail=200 openai-tunnel
```

Fix startup/configuration errors before investigating remote connectivity.

### 2. Container is running but unhealthy

Run:

```bash
docker compose exec openai-tunnel /usr/bin/tunnel-client health --port 8080 --json
```

Then inspect the detailed aggregate. Common readiness failures include MCP startup reachability, OAuth discovery when configured, and control-plane connectivity.

### 3. MCP is not observed

Inspect `/health/mcp`. A ready Tunnel can initially report that MCP has not yet been observed, depending on transport and traffic. Use the component's observation timestamp and state rather than assuming that missing historical evidence means the MCP server is absent.

Also verify the ServerFS container independently:

```bash
docker compose ps serverfs-mcp
docker compose logs --tail=200 serverfs-mcp
```

### 4. Control-plane polls fail

Inspect `/health/control-plane` and the Tunnel logs:

```bash
docker compose logs --tail=200 openai-tunnel
```

Repeated completed poll failures or retry/backoff state are stronger evidence of a remote connectivity/authentication problem than container `Up`. A current long poll is normal; it is not an outage by itself.

If `SERVERFS_OPENAI_TUNNEL_USE_PROXY=true`, also verify the configured HTTP proxy path. Tunnel proxy settings affect Tunnel control-plane egress only; they do not configure Docker image pulls. See [Restricted-network ARM64 deployment](../restricted-network-arm64/).

### 5. Polling succeeds but ChatGPT receives no result

Inspect `/health/response-delivery`. Polling commands and uploading completed responses are separate stages and can fail independently.

If local work appears stuck, inspect `/health/queue` and `/health/dispatcher` to distinguish backpressure from active execution.

## `doctor` versus runtime health

`tunnel-client doctor --explain` is useful as a preflight when running the client directly or reproducing configuration outside the managed Compose process. It validates local configuration/reachability checks; it does not replace runtime component evidence from the already-running Tunnel and does not prove that every future control-plane poll will succeed.

For the ServerFS Compose deployment, prefer this order:

1. `docker compose ps`;
2. `tunnel-client health --port 8080 --json` inside the running container;
3. `/health?details=true` and the relevant component route;
4. bounded Tunnel logs.

This keeps the diagnosis tied to the exact process and configuration that ChatGPT is using.

## Security notes

- Do not publish the Tunnel health/admin port to an untrusted network merely for troubleshooting.
- Do not paste `.env`, proxy credentials, control-plane keys or raw unrestricted logs into support channels.
- Prefer bounded component snapshots and recent redacted logs.
- Treat health observations as operational metadata; they are evidence at their timestamps, not a continuous availability guarantee.
