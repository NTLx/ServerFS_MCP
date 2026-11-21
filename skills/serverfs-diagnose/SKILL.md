---
name: serverfs-diagnose
description: Locate a ServerFS request, authorization, Connector or Agent failure using bounded read-only evidence; verify any authorized scoped repair.
---

# Diagnose ServerFS

## When to use

Unexpected health, failed MCP request, missing tools or Agent/Repo Ops/task failure.

## When NOT to use

An unverified restart, cleanup, upgrade or permission change. Diagnosis is read-only by default.

## Preflight

Identify affected endpoint, exact component IDs, installed versions, effective deployment selection, expected behavior, timestamp, recent changes and active work. Resolve matching source/help. If identity is uncertain, inspect only non-sensitive inventory; do not mutate.

## Procedure

1. Core: verify actual listener/transport, exact Host and caller admission, then authenticated initialize/tools/list and one safe read. Direct HTTP has no Connector. Registration/TCP health alone is insufficient.
2. Missing/denied tool: inspect effective workdir and capability policy. Binary, Agent and Repo Ops are independent; don't widen policy to make a probe pass. Do not submit an Agent task as a fallback for a missing or denied Repo Ops action.
3. Ingress: after local Core succeeds, check selected Connector, provider route/DNS/TLS, remote authenticated MCP, then response delivery/SSE. Cloudflare origin is Linux service DNS or native loopback; OpenAI control-plane poll is not response delivery.
4. Bridge: verify exact owner, IPC/grant and workdir mapping, runtime discovery, then the specific task/interaction/lease. File tools can survive Bridge outage. Terminal status is not proof of writer stop; unresolved recovery guard fails closed. Do not retry an uncertain v2 submission: signed grants reject continuation and idempotency keys.
5. Use bounded, redacted evidence; stop at the first failing boundary. Compare unaffected components. Repair only the proven cause with authorization, then repeat the original check.

## Platform branches

| Platform | Read the relevant runbook section | Evidence |
| --- | --- | --- |
| Linux | `deployment/linux/README.md` | Exact Compose project/env/overlay array; selected service ps/logs, exact user systemd Bridge unit. |
| macOS | `deployment/macos/README.md` | Mode-specific status/preflight, signed app/SMAppService; exact Connector ID. |
| Windows | `deployment/windows/v0250-native-operations.md` | Component CLI status with instance/owner and Connector ID; installed worker, ACL/pipe checks. |

## Authoritative sources

Paths are relative to the matching checkout, not the Skill archive. Read only the applicable runbook section; for ingress details use `site/src/content/docs/docs/tunnel-diagnostics.md`, for task recovery `site/src/content/docs/docs/agent-bridge.md`, for admission the specific API Key/OAuth guide. Generic `serverfs diagnostics snapshot/task` needs a compatible local endpoint; it is not a signed named attachment proof. Use actual CLI help for available flags/routes.

## Safety / stop conditions

No secrets, entire env dumps, raw provider payloads or broad logs. Shared Core cannot operate owner; never delete recovery guards, bypass security, kill by process name, or interrupt unrelated components. Do not infer old task telemetry from a fresh process.

## Verification

State failing boundary and exact observed result. If fixed, repeat the original safe test and relevant denial case; confirm unrelated identities/state and fresh remote session where applicable. Old SSE replay is not guaranteed.

## Success criteria

Failure isolated with evidence; authorized repair independently verified or precise blocker documented.

## Failure handling

Stop on unsafe work, ambiguity or repeated failure. Preserve evidence, report observed error, changes (or none), missing proof and next discriminating check.
