---
title: Agent Bridge
description: Optional structured delegation to native Codex, Claude and Qoder runtimes.
---

The Agent Bridge is an **optional host-side boundary**. It lets ServerFS expose structured Agent task tools without putting Codex, Claude or Qoder inside the MCP container. v0.13.0 is the current published stable release: it preserves v0.12's Linux egress, native interactive approval and configurable result-spooling contracts while adding the native macOS launchd/AF_UNIX deployment.

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
  ├── Claude Code
  └── Qoder
```

## Why it is separate

The base `compose.yml` remains Agent-unaware. Agent deployments explicitly add `compose.agent.yml`.

This keeps:

- the filesystem MCP usable without native Agent runtimes
- the host socket and writer locks out of the base deployment
- the security boundary visible in deployment configuration
- existing filesystem-only deployments backward compatible

## Runtime behavior

ServerFS exposes ten structured Agent tools when Agent policy is enabled. They cover runtime/model discovery, task submission, status/events, exact retrieval of spooled final results, approval/question handling, steering where supported, and cancellation.

They are **not** a shell, argv passthrough, or generic command executor.

Current deployments can expose:

- 21 tools: filesystem + Agent
- 23 tools: filesystem + binary + Agent

## v0.12 Linux egress and interaction contract

v0.12.0 keeps the same ten public Agent tools and adds explicit Linux deployment controls rather than a new orchestration layer. `SERVERFS_AGENT_USE_PROXY` independently selects the shared HTTP proxy for native Agent provider traffic. Claude and Qoder receive a scrubbed deterministic proxy environment; proxied Codex uses a Bridge-owned standalone app-server so ServerFS never restarts or mutates the user's shared managed Codex daemon merely to impose proxy policy. Direct mode preserves the existing native provider paths.

Agent proxying is intentionally credentialless-only in v0.12. If `SERVERFS_AGENT_USE_PROXY=true` while the shared proxy username/password is configured, deployment rendering fails closed. An authenticated upstream must be represented by a credentialless local broker. Tunnel and Jev have their own independent proxy switches and may use the authenticated shared endpoint.

Provider-native approvals are surfaced through the existing `respond_agent_approval` tool; ServerFS does not auto-approve or weaken provider semantics. The inline/spool boundary is configured by `SERVERFS_AGENT_RESULT_SPOOL_THRESHOLD_BYTES`, with the public default unchanged at 256 KiB and the maximum spooled result unchanged at 8 MiB. Large normalized message events are bounded independently so a valid large final response can still reach spool handling.

## v0.9 model discovery and selection

`list_agent_models` is the one new read-only Agent tool. Codex uses App Server `model/list`; Qoder uses the structured Agent SDK current-account catalog; Claude returns `model_discovery=unsupported` because the installed Claude Code/Agent SDK does not expose an equivalent stable native-account enumeration API. Discovery never starts an inference turn.

`submit_agent_task` accepts optional `model`. Omitting it preserves the runtime's native default or resumed-session behavior. An explicit provider-native ID applies only to that submission; ServerFS stores it as task/manifest evidence and includes it in idempotency identity, but never writes it as a runtime/workdir/user default. Unknown/unavailable models fail through the provider path; ServerFS never silently falls back.

## v0.8 Qoder runtime

v0.8.0 added `qoder` as the third production runtime. It uses the official Qoder Agent SDK with the existing system `qodercli`, supports native session continuation, approval and `AskUserQuestion` brokerage, and cancellation through `interrupt()`.

Qoder restart semantics are conservative: a persisted native session can be resumed by a new task, but ServerFS does not claim that an old in-flight qodercli process can be reattached after Bridge restart. Live steering is also deliberately disabled: a 2026-09-29 real SDK/CLI probe showed that `priority="now"` first ends the current `receive_response()` with an `error_during_execution` Result, while the steered success arrives only from a second response iteration. That does not fit the current one-task/one-terminal-Result Bridge contract.

## v0.7 runtime reliability

v0.7.0 introduced reliability and evidence around the existing Bridge rather than adding orchestration. New tasks carry an immutable execution manifest and optional opaque `correlation_id`; normalized events use envelope schema v1. The original v0.7.0 task deadline default was 24 hours and terminal retention was seven days.

v0.7.1 keeps that contract unchanged and fixes a narrow Codex recovery edge case around a proven pre-provider-start control-socket failure. v0.7.2 adds maintenance-only recovery state hygiene: when lazy reconciliation proves `provider_active=false`, any still non-terminal ServerFS task is first marked `interrupted` with `AGENT_PROVIDER_INACTIVE`, pending interaction state becomes stale through the normal terminal transition, and only then is the recovery guard removed. Unknown provider state remains fail-closed and preserves the guard.

### v0.7.3: lifecycle reliability

The v0.7.3 release adds retry-safe Agent submission for callers that lose the `task.submit` response. `submit_agent_task` accepts an optional opaque `idempotency_key`, distinct from `correlation_id`. Reusing the same key with the same semantic submission returns the retained original task and does not start a second provider turn; conflicting reuse fails with `AGENT_IDEMPOTENCY_CONFLICT`.

Agent lifetime is now administrator policy rather than a hard-coded 24-hour window. Release defaults are a 2-hour task timeout, a 30-minute approval/question timeout, four active tasks, and seven-day terminal retention. An unanswered interaction becomes stale and interrupts the task with `AGENT_INTERACTION_TIMED_OUT`. Explicit cancellation, including an approval decision of `cancel_task`, is persisted as terminal before the RPC returns. Provider interrupt is best-effort and internally bounded to 10 seconds so a stuck provider RPC cannot indefinitely block Bridge-side cancellation. The live writer lease is released only after background cleanup, and the persistent recovery guard is cleared only when provider-aware reconciliation proves the provider has stopped. Client disconnect or stopped polling alone does not cancel a healthy asynchronous task.

These limits are separate from the MCP-to-Bridge RPC timeout and provider event-idle timeout. The deployment `.env` uses `SERVERFS_AGENT_TASK_TIMEOUT_SECONDS`, `SERVERFS_AGENT_INTERACTION_TIMEOUT_SECONDS`, `SERVERFS_AGENT_MAX_ACTIVE_TASKS`, and `SERVERFS_AGENT_TASK_RETENTION_HOURS`.

Workspace-write tasks also publish a persistent per-slot recovery guard alongside the existing `flock`. If the Bridge exits abnormally, mutations fail closed with `WORKDIR_RECOVERY_REQUIRED` until provider-aware reconciliation proves the prior provider is no longer active. ServerFS does not blindly rerun an interrupted task.

Final responses stay inline up to the configured spool threshold. The default remains 256 KiB; v0.12 exposes that boundary as `SERVERFS_AGENT_RESULT_SPOOL_THRESHOLD_BYTES`. Larger responses through 8 MiB are atomically spooled to private Bridge state and exposed by `get_agent_task` as a bounded preview plus size/SHA-256 metadata. `read_agent_task_result` retrieves the exact UTF-8 result in bounded chunks. Results above 8 MiB fail with `AGENT_RESULT_TOO_LARGE`.

## Deployment verification

For current Agent-enabled deployments, run:

```bash
python3 deployment/agent-bridge/verify_host.py --require-runtimes
```

The strict mode first proves the user-scoped Bridge deployment, then performs bounded read-only `runtime.list` retries for every enabled native runtime. It never starts, restarts, bootstraps, updates, kills, or otherwise manages Codex/Claude/Qoder. If a runtime remains unavailable after the retry window, verification fails and provider-native lifecycle diagnostics remain an operator action. For Codex specifically, if `codex app-server daemon update` succeeds with `runningVersion: null` and the managed control socket is absent, first prove there are no active Agent tasks, then run `codex app-server daemon bootstrap` without `--remote-control` and re-run `daemon version` plus this strict verifier.

When recreating the MCP container, always preserve both `-f compose.yml -f compose.agent.yml`; using base Compose alone removes the Agent socket/lock mounts even if `.env` still contains Agent policy.

## Optional Jev advisors

The host Bridge can optionally use TypeSafe Jev as an **advisory-only** decision layer. It does not become an Agent runtime or automatic router.

With `SERVERFS_JEV_API_KEY` configured, one task-submission request provides Agent Task Preflight and Runtime Router advice. v0.9.0 also lets `list_agent_models` include a concrete proposed task: after successful native model discovery, Model Advisor may recommend one of the currently exposed candidates before submission. In v0.12, `SERVERFS_JEV_USE_PROXY` independently selects the shared proxy for Jev's explicit HTTP client; this does not affect Agent provider or Tunnel routing. ServerFS returns that advice with `automatic=false` and never copies it into `submit_agent_task.model`. If the native provider later creates a concrete approval request, Approval Advisor may make one additional Jev request; identical approvals within the same task reuse cached advice. With no key, model discovery/submission still work and no Jev requests occur.

Jev never overrides the explicit runtime/model, workdir policy, writer lease, provider approval state, or `respond_agent_approval`. See [Jev Advisors](./jev-advisors/) for the model contract, request flow, data minimization, and failure behavior.

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
