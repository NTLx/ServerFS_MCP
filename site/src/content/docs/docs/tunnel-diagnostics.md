---
title: Connector Diagnostics
description: Diagnose Core reachability, Connector registration, remote MCP and response delivery independently.
---

Use this guide for an ingress failure. Do not restart the complete deployment. Record the exact Core/Connector, platform, version, endpoint, time of failure and recent changes. Direct HTTP has no Connector to diagnose.

## 1. Isolate the failing boundary

| Check | What success proves |
| --- | --- |
| Core listener and authenticated MCP initialize/tools/list | Local Core and caller admission work |
| Selected Connector process and readiness | The intended local ingress process runs |
| Provider registration, DNS/TLS and route | The external service can reach the intended route |
| Authenticated remote MCP and safe read | End-to-end service works for that client |
| Long-lived SSE / completed response | The relevant session and delivery path work |

Check in order and stop at the first failing boundary. Verify the actual Host and credentials without exposing secrets. For API Key 401, OAuth challenges or resource mismatch, see [API Key](../api-key/) and [OAuth](../oauth/), not a broader allowlist or disabled security.

## 2. Inspect the selected component

Linux: use the exact project's existing env, overlays and profiles. In Bash, retain the complete Compose command array from the [Linux runbook](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/linux/README.md), referred to below as `deployment`. Do not build a new incomplete selection just for troubleshooting.

```bash
# Select exactly openai-tunnel OR cloudflared, as configured.
service=cloudflared
"${deployment[@]}" ps serverfs-mcp "$service"
"${deployment[@]}" logs --tail=100 "$service"
```

macOS: use named instance `status` or `owned-status` and the exact staged Connector. Windows: use its component CLI `status` with Core and Connector IDs. Follow [macOS](../macos-native/) or [Windows](../windows-native/) for native status/log paths. A registered job or running process alone does not prove remote reachability.

## 3. Cloudflare

Verify that the configured Tunnel identity is the intended one, its provider route avoids unrelated hostnames, and the actual origin is reachable from cloudflared's network namespace (Linux service DNS, native loopback). Check credential-file permissions, provider registration/egress, external DNS/TLS, actual forwarded Host, then authenticated public MCP. A provider connection is not a proof of Core authorization. See [Cloudflare Connector](../cloudflare-tunnel/).

## 4. OpenAI

For Linux named HTTP the health listener is fixed to internal loopback port 8080. The compatibility Compose path derives the health port from `HEALTH_LISTEN_ADDR`. Native jobs may use an allocated port; do not assume 8080 on those hosts.

```bash
"${deployment[@]}" exec openai-tunnel \
  /usr/bin/tunnel-client health --port 8080 --require-control-plane-poll --json
```

This probe requires readiness and at least one successful control-plane poll since daemon start. It does not prove continuous polling or response delivery. For installed clients exposing detailed health routes, inspect only the relevant snapshot (use the actual health port and a trusted local HTTP client; the tested Alpine image includes wget).

```bash
"${deployment[@]}" exec openai-tunnel \
  wget -qO- 'http://127.0.0.1:8080/health?details=true'
```

| Evidence | Interpret |
| --- | --- |
| `/healthz` / `/readyz` | Liveness / startup readiness |
| `/health/mcp` | Observed upstream MCP access; missing observations may mean no traffic |
| `/health/control-plane` | Completed polls, failures and retry/backoff; an in-flight long poll is normal |
| `/health/response-delivery` | Completed results accepted upstream, separately from polling |
| `/health/queue`, `/health/dispatcher` | Backlog, backpressure and active local work |

A component route may return HTTP 200 with degraded JSON status. Health routes and flags depend on the actual client build; check its help and verified release requirements. The repository's historical v0.0.17 baseline is not a compatibility guarantee. `tunnel-client doctor --explain` is preflight, not a replacement for live process evidence.

If MCP access fails, check the named HTTP upstream URL, exact Host and matching static Core bearer file. Control-plane credentials are separate. For failed polls, inspect provider connectivity and the Connector's independent proxy; it does not configure registry pulls. When polls succeed but results do not arrive, inspect response delivery before restarting anything. Named OpenAI HTTP does not support OAuth-only Core; historical stdio-Tunnel OAuth issues are documented in the [OAuth guide](../oauth/).

## 5. Recover and verify

Preserve evidence before any authorized scoped repair. Restart only the component at fault, using the platform's lifecycle procedure; confirm unrelated Core, Bridge and Connector identities remain unchanged. Verify fresh remote MCP, and SSE separately when required. Do not assume old-session replay.

For Agent diagnostics, consume small relevant status snapshots and recent redacted logs, not complete env files, unrestricted logs or raw response payloads. Never publish health/admin ports or credentials. Report observed state, timestamp, failing boundary and remaining uncertainty.
