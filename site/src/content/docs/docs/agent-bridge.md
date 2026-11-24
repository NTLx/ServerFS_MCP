---
title: Agent Bridge
description: Optional host-side Agent delegation and deterministic Repo Ops.
---

Agent Bridge runs on the host and manages provider execution, task state, writer coordination and Repo Ops independently of MCP Core and Connectors.

## Choose the attachment

| Mode | When to use |
| --- | --- |
| `disabled` | File service only; no Bridge needed. |
| `owned` | One Core has a dedicated owner. |
| `shared` | Independently managed owner serves explicitly authorized Cores on the same host. |

Named Linux uses a host user systemd service and signed Unix-socket attachments. macOS uses native SMAppService and Unix sockets; owned mode uses private manifest/policy checks, not the shared v2 grant path. Windows uses native scheduled tasks and Named Pipe authorization. Follow the [Linux](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/linux/README.md), [macOS](../macos-native/) or [Windows](../windows-native/) guide.

Shared Core grants restrict methods and aliases. A shared Core cannot start, stop or retire its owner. Matching UID/SID is not sufficient authorization; independent owners must not overlap writable roots. Keep owner credentials, state and configuration outside exposed workdirs.

## Enable only the required capabilities

The base Core has 11 filesystem tools. Binary transfer adds two; Agent delegation adds ten; [Repo Ops](../repo-ops/) adds four independently. Repo Ops-only still needs a Bridge but does not require a provider. It never creates an Agent task.

Agent delegation is not a shell or arbitrary argv interface. Codex, Claude, Qoder and configured [ACP profiles](../acp/) require an authorized writable workdir and `workspace-write` policy. Not every platform attachment supports every capability: for example, named macOS shared Cores expose one Agent workdir in v0.25.0 or 1–16 owner-bound Agent workdirs in the v0.26.0 candidate, and no Repo Ops.

## Submit and follow an Agent task

1. Call `list_workdirs` to check the alias, access, Agent policy and runtime allowlist. Use `list_agent_runtimes` for current capabilities; it does not install or start providers.
2. Optionally call `list_agent_models`. Codex uses its runtime catalog, Qoder its current-account catalog; Claude and ACP report unsupported discovery. Discovery does not start inference.
3. Submit one authorized objective via `submit_agent_task`. Include the allowed mutation boundary, stop conditions and required evidence. An optional provider-native `model` applies to this task only; omission preserves native defaults/session behavior. Invalid selections are not silently replaced.
4. Poll `get_agent_task` and read bounded cursor-paginated `read_agent_task_events`. Respond to actual provider requests using `respond_agent_approval` or `answer_agent_question`. ServerFS does not auto-approve. Use `send_agent_message` only when live steering is advertised.
5. Retrieve the result, verify the requested outcome independently, or use `cancel_agent_task` when cancellation is authorized.

On v1-compatible attachment paths, use the same optional `idempotency_key` and semantic submission after a lost submit response; this returns the retained original task, not a duplicate turn. Changed content with the same key fails with `AGENT_IDEMPOTENCY_CONFLICT`. Signed v2 Core grants (Linux named, macOS shared and grant-enabled Windows) currently reject both `idempotency_key` and `continue_from_task_id`, even if a runtime advertises session continuation. Omit them on those paths; an uncertain submit response is not safe to retry blindly. The optional `correlation_id` is for tracing, not deduplication. Tasks record execution manifests and normalized v1 event envelopes.

Qoder supports native session continuation, approvals, questions and cancellation, but not live steering or reattachment to a pre-restart in-flight process. Check advertised capabilities instead of inferring them from another provider.

## Limits and recovery

| Setting | Default |
| --- | --- |
| Task lifetime | 2 hours |
| Approval/question timeout | 30 minutes |
| Active tasks | 4 |
| Terminal retention | 7 days |
| Inline result threshold | 256 KiB |
| Maximum stored result | 8 MiB |

These Bridge policies are distinct from the Core RPC and provider event-idle timeouts. Named owners use their platform's private configuration; the Linux compatibility renderer maps the corresponding `SERVERFS_AGENT_*` environment settings.

Results are normalized and workdir-path-redacted before storage; they are not raw provider logs. Above the inline threshold, `get_agent_task` provides a bounded preview with byte count and SHA-256. Use `read_agent_task_result` for the exact stored UTF-8 text in bounded chunks (up to 64 KiB). Oversized results fail with `AGENT_RESULT_TOO_LARGE`. Read only the necessary chunks.

Disconnecting or stopping polling does not cancel a healthy task. An unanswered interaction becomes stale and interrupts it with `AGENT_INTERACTION_TIMED_OUT`. Cancellation (including `cancel_task` in an approval) is persisted before return; the provider interrupt is best-effort and bounded to 10 seconds. A terminal status is not proof the provider or writer has stopped.

Workspace-write tasks and Repo Ops coordinate with file mutations through writer leases. After abnormal exit, a persistent guard can refuse writes with `WORKDIR_RECOVERY_REQUIRED`. Reconciliation must prove the provider inactive before clearing the guard; retained nonterminal records then become interrupted with `AGENT_PROVIDER_INACTIVE`. Unknown state fails closed. Never remove guards, restart unrelated providers or blindly resubmit interrupted work.

## Egress and optional advisors

Provider authentication remains in the native runtime. Linux named owners use private Bridge JSON (`proxy` and runtime `use_proxy`); the older singleton renderer uses `SERVERFS_AGENT_USE_PROXY`. Agent proxy endpoints must be credentialless. An authenticated upstream requires a credentialless local broker. Proxied Linux Codex uses a Bridge-owned standalone app-server rather than rewriting the user's managed daemon; Claude and Qoder receive controlled proxy settings. The v0.26.0 candidate also supports explicit per-runtime/ACP proxy opt-in for named Windows Bridge owners via a protected credentialless `--agent-proxy-env-file`; direct runtimes do not inherit ambient proxy variables. Tunnel and Jev egress are independent.

Optional [Jev advisors](../jev-advisors/) may advise before submission, during native model discovery with explicit task context, or on a real approval request. Advice remains `automatic=false`; it cannot change the runtime, model, policy, writer lease or provider decision. Without a configured key the Agent path does not depend on Jev.

## Verify and diagnose

First verify the exact owner, attachment, workdir authorization, and runtime discovery. Then use a narrowly scoped task only when authorized. Follow the platform runbook for named instance diagnostics; the generic `serverfs diagnostics snapshot --format json` and `serverfs diagnostics task <task-id> --format json` commands require a compatible local endpoint and are not proof of signed named attachment.

Diagnostics are bounded, private RPC projections, not public tools or persistent distributed traces. Task disposition may be `available`, `unknown_task`, `not_retained` or CLI `bridge_unavailable`. Restart does not reconstruct lost in-memory history. Do not expose private logs or credentials.

For a delegated task, state the objective, precise scope, allowed changes, stop conditions and evidence needed. Split tasks only along real capability boundaries. ServerFS forwards the prompt as submitted; do not disguise or fragment instructions to bypass provider safety or weaken authorization and auditing.
