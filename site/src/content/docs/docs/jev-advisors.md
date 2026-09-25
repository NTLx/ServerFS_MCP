---
title: Jev Advisors
description: Optional TypeSafe Jev decision support for Agent task quality, routing, and provider approvals.
---

ServerFS can optionally use [TypeSafe Jev](https://docs.typesafe.ai/introduction) inside the **host-side Agent Bridge** as a structured decision-support layer.

Jev is a System One model: instead of generating prose, it evaluates typed questions against shared state and returns structured values such as Choice probabilities/confidence and Noul values. ServerFS uses that shape for narrow advisory judgments and keeps deterministic policy in code.

The integration is **opt-in, advisory-only, and fail-open**. Jev is not an Agent runtime, authorization source, or security boundary.

> **Status:** this opt-in experimental capability was introduced in v0.6.0 and remains included in the current v0.7.2 release. It changes the host Agent Bridge only and does not change the MCP public tool schema.

## Enable it

Set the TypeSafe API key in the repository's existing untracked `.env`:

```text
SERVERFS_JEV_API_KEY=<your key>
```

Leave the value empty to disable every Jev feature. When disabled:

- no TypeSafe client is constructed;
- no Jev network request is made;
- Agent submission and approval behavior follow the non-Jev path.

The installer renders a configured key only into the user-owned Agent Bridge config with mode `0600`. The key is not passed into the MCP container.

## Current model contract

ServerFS currently pins:

```text
jev-1.13.0
typesafe-sdk==0.7.1
```

The version is pinned instead of using `jev-latest` so evaluation behavior can be calibrated and reproduced before deliberately moving to a newer model.

TypeSafe currently documents Jev 1.13 as text-only with a 64k total request budget and an additional 32k limit for the state plus the single longest question. English is its strongest language; CJK is supported but should be validated on the application's own data. ServerFS therefore treats all scores and recommendations as evidence, never as authority.

## Three advisory capabilities

### Agent Task Preflight

Every Jev-enabled `submit_agent_task` can evaluate:

- whether the prompt is one narrow objective;
- whether mutation scope is explicit;
- whether stop conditions are explicit;
- whether verification evidence is explicit;
- whether the task fits structured ServerFS primitives or a native Agent.

### Runtime Router

The same task-submission Jev request also recommends one route:

- `direct_serverfs_tool`
- `codex`
- `claude`
- `human_review`

This does **not** add `runtime=auto`. The explicitly requested runtime and deterministic per-workdir runtime policy remain authoritative.

### Approval Advisor

If Codex or Claude later produces a real provider approval request, ServerFS may make one additional Jev request for that concrete approval.

It evaluates:

- necessity for the authorized objective;
- whether scope is bounded;
- destructive or irreversible risk;
- sensitive access;
- external side effects;
- an advisory recommendation such as `approve_once`, `approve_session`, `deny`, `cancel_task`, or `review_carefully`.

The provider request remains blocked until the caller explicitly responds through the existing approval tool. Jev never resolves the approval itself.

## Request economy

ServerFS deliberately minimizes Jev traffic:

```text
submit_agent_task
  └─ 1 Jev request
       ├─ Preflight
       └─ Runtime Router

provider approval?
  ├─ no  -> 0 extra Jev requests
  └─ yes -> 1 approval-specific Jev request
              └─ identical approval in the same task -> reuse cached advice
```

Preflight and Runtime Router share one `system_one` request because Jev can evaluate multiple typed questions independently against the same state. Approval Advisor runs later only because the concrete provider approval object does not exist at task-submission time.

## Data minimization

ServerFS does not send the workdir contents to Jev just because the advisor is enabled.

Task-level evaluation receives the submitted task context needed for the advisory questions. Approval evaluation uses the existing redacted approval object, restricts it to an allowlisted field set, and applies another sanitizer for obvious secret/token/password/credential fields and patterns.

No Jev result may weaken:

- workdir/path authorization;
- runtime allowlists;
- writer leases;
- native provider approval semantics;
- MCP transport/network isolation;
- provider safety checks.

## Failure behavior

If TypeSafe is unavailable, times out, rate-limits the request, or returns an unexpected result, the advisor reports `status=unavailable`.

The already-authorized Agent task or pending provider approval continues through the normal ServerFS path. Jev failure does not fail open into broader authority; it only removes advisory context.

## Evaluation status

The current integration has been live-tested with:

- bounded filesystem reads;
- Git/test tasks;
- explicit Codex/Claude provider mismatches;
- human-review decisions;
- intentionally bundled or vague prompts;
- bounded one-time writes;
- repeated session-scoped writes;
- outside-workdir approval requests.

The project intentionally does **not** use automatic routing or automatic approval thresholds yet.

Upstream references: [Introduction](https://docs.typesafe.ai/introduction), [Models](https://docs.typesafe.ai/models), and [API reference](https://docs.typesafe.ai/api).

For implementation and measured examples, see the repository notes:

- [Agent Task Preflight](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/jev-agent-preflight-experiment.md)
- [Runtime Router](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/jev-runtime-router-experiment.md)
- [Approval Advisor](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/jev-approval-advisor-experiment.md)
