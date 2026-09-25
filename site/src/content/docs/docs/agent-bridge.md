---
title: Agent Bridge
description: Optional structured delegation to native Codex and Claude runtimes.
---

The Agent Bridge is an **optional host-side boundary**. It lets ServerFS expose structured Agent task tools without putting Codex or Claude inside the MCP container.

```text
ChatGPT
  │
  ▼
ServerFS MCP
  │  structured Agent RPC
  ▼
Unix socket
  │
  ▼
Host Agent Bridge
  ├── Codex
  └── Claude Code
```

## Why it is separate

The base `compose.yml` remains Agent-unaware. Agent deployments explicitly add `compose.agent.yml`.

This keeps:

- the filesystem MCP usable without native Agent runtimes
- the host socket and writer locks out of the base deployment
- the security boundary visible in deployment configuration
- existing filesystem-only deployments backward compatible

## Runtime behavior

ServerFS exposes nine structured Agent tools when Agent policy is enabled. They cover runtime discovery, task submission, status/events, exact retrieval of spooled final results, approval/question handling, steering where supported, and cancellation.

They are **not** a shell, argv passthrough, or generic command executor.

Current deployments can expose:

- 20 tools: filesystem + Agent
- 22 tools: filesystem + binary + Agent

## v0.7 runtime reliability

v0.7.0 introduced reliability and evidence around the existing Bridge rather than adding orchestration. New tasks carry an immutable execution manifest and optional opaque `correlation_id`; normalized events use envelope schema v1; tasks default to a 24-hour deadline and seven-day terminal retention.

v0.7.1 keeps that contract unchanged and fixes a narrow Codex recovery edge case around a proven pre-provider-start control-socket failure. v0.7.2 adds maintenance-only recovery state hygiene: when lazy reconciliation proves `provider_active=false`, any still non-terminal ServerFS task is first marked `interrupted` with `AGENT_PROVIDER_INACTIVE`, pending interaction state becomes stale through the normal terminal transition, and only then is the recovery guard removed. Unknown provider state remains fail-closed and preserves the guard.

Workspace-write tasks also publish a persistent per-slot recovery guard alongside the existing `flock`. If the Bridge exits abnormally, mutations fail closed with `WORKDIR_RECOVERY_REQUIRED` until provider-aware reconciliation proves the prior provider is no longer active. ServerFS does not blindly rerun an interrupted task.

Final responses up to 256 KiB stay inline. Responses above 256 KiB through 8 MiB are atomically spooled to private Bridge state and exposed by `get_agent_task` as a bounded preview plus size/SHA-256 metadata. `read_agent_task_result` retrieves the exact UTF-8 result in bounded chunks. Results above 8 MiB fail with `AGENT_RESULT_TOO_LARGE`.

## Deployment verification

For Agent-enabled release acceptance, run:

```bash
python3 deployment/agent-bridge/verify_host.py --require-runtimes
```

The strict mode first proves the user-scoped Bridge deployment, then performs bounded read-only `runtime.list` retries for every enabled native runtime. It never starts, restarts, bootstraps, updates, kills, or otherwise manages Codex/Claude. If a runtime remains unavailable after the retry window, verification fails and provider-native lifecycle diagnostics remain an operator action.

When recreating the MCP container, always preserve both `-f compose.yml -f compose.agent.yml`; using base Compose alone removes the Agent socket/lock mounts even if `.env` still contains Agent policy.

## Optional Jev advisors

The host Bridge can optionally use TypeSafe Jev as an **advisory-only** decision layer. It does not add any MCP tool or Agent runtime.

With `SERVERFS_JEV_API_KEY` configured, one task-submission request provides both Agent Task Preflight and Runtime Router advice. If the native provider later creates a concrete approval request, Approval Advisor may make one additional Jev request; identical approvals within the same task reuse cached advice. With no key, none of these Jev paths exist.

Jev never overrides the explicit runtime, workdir policy, writer lease, provider approval state, or `respond_agent_approval`. See [Jev Advisors](./jev-advisors/) for the model contract, request flow, data minimization, and failure behavior.

## Delegation hygiene

`submit_agent_task` is intentionally an objective-level interface, not a place to embed an
entire operational transcript. For normal authorized engineering or operations, keep each
delegation narrow and explicit:

- state the user-owned workdir or existing deployment being operated on;
- give one concrete objective;
- state the allowed mutation scope and stop conditions;
- ask for the minimum evidence needed to verify completion;
- prefer existing structured ServerFS/project actions over unrelated shell, network or
  security-analysis detail;
- split implementation, validation, deployment and Git publication when they are genuinely
  separate capability boundaries.

A compact pattern is:

```text
Objective: <one thing to accomplish>
Scope: <workdir/path or existing deployment component>
Allowed changes: <exact mutation boundary>
Stop if: <failure/ambiguity condition>
Return: <specific evidence>
```

This is a clarity and least-authority rule, not a mechanism for evading provider safety
checks. ServerFS forwards the submitted prompt as written; it does not automatically
rewrite, encode or sanitize instructions to change provider safety outcomes. Do not encode,
disguise, fragment or relocate instructions merely to bypass a classifier. If a legitimate
task is blocked, narrow it at a real capability boundary or use a more structured existing
ServerFS primitive without weakening authorization, audit, writer-lease or network-isolation
controls.
