---
title: Jev Advisors
description: Optional TypeSafe Jev decision support for Agent task quality, runtime routing, model choice, and provider approvals.
---

ServerFS can optionally use [TypeSafe Jev](https://docs.typesafe.ai/introduction) inside the **host-side Agent Bridge** as a structured decision-support layer.

Jev is a System One model: instead of generating prose, it evaluates typed questions against shared state and returns structured values such as Choice probabilities/confidence and Noul values. ServerFS uses that shape for narrow advisory judgments and keeps deterministic policy in code.

The integration is **opt-in, advisory-only, and fail-open**. Jev is not an Agent runtime, authorization source, or security boundary.

> **Status:** this opt-in experimental capability was introduced in v0.6.0, extended with Model Advisor in v0.9.0, and remains included in the current v0.13.0 stable release. v0.12 added an independent explicit Jev proxy switch; v0.13 carries that contract into the native macOS Bridge. Jev remains an internal host-Bridge advisor and never gains execution authority.

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

In v0.12, Jev egress is controlled independently with `SERVERFS_JEV_USE_PROXY`. When false, the Jev client is created without a proxy even if Tunnel or Agent proxying is enabled. When true, the Bridge supplies the shared ServerFS HTTP proxy to Jev through an explicit `httpx2.AsyncClient`; it does not mutate process-wide proxy environment variables, so provider children cannot inherit Jev routing accidentally. Jev may use authenticated shared proxy credentials; this is separate from the credentialless-only Linux Agent proxy rule.

## Current model contract

ServerFS currently pins:

```text
jev-1.13.0
typesafe-sdk==0.7.1
```

The version is pinned instead of using `jev-latest` so evaluation behavior can be calibrated and reproduced before deliberately moving to a newer model.

TypeSafe currently documents Jev 1.13 as text-only with a 64k total request budget and an additional 32k limit for the state plus the single longest question. English is its strongest language; CJK is supported but should be validated on the application's own data. ServerFS therefore treats all scores and recommendations as evidence, never as authority.

## Four advisory capabilities

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
- `qoder`
- `human_review`

This does **not** add `runtime=auto`. The explicitly requested runtime and deterministic per-workdir runtime policy remain authoritative.

### Model Advisor

`list_agent_models` can optionally include the exact task ChatGPT is considering. When the selected runtime successfully exposes a native model catalog and Jev is enabled, ServerFS sends the sanitized task context plus the normalized eligible model metadata to Jev before any Agent task is submitted.

Model Advisor:

- considers only currently exposed candidates that are not explicitly disabled/hidden;
- uses runtime-provided reasoning/context/modalities/free/price metadata when present;
- does not infer quality or cost from a model name alone;
- returns an exact provider-native `recommended_model`, confidence/probabilities, and `automatic=false`;
- never writes or forwards that recommendation into `submit_agent_task.model` automatically.

ChatGPT or the user remains responsible for the later submission choice. Claude model advice is currently `not_applicable` because Claude Code does not expose the same stable native-account model-enumeration capability as Codex/Qoder.

### Approval Advisor

If Codex, Claude or Qoder later produces a real provider approval request, ServerFS may make one additional Jev request for that concrete approval.

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
list_agent_models + task context
  └─ up to 1 Jev request
       └─ Model Advisor

submit_agent_task
  └─ 1 Jev request
       ├─ Preflight
       └─ Runtime Router

provider approval?
  ├─ no  -> 0 extra Jev requests
  └─ yes -> 1 approval-specific Jev request
              └─ identical approval in the same task -> reuse cached advice
```

Model Advisor is deliberately a separate pre-submit request because ChatGPT must receive its recommendation before deciding whether to pass a `model`. Preflight and Runtime Router then share one `system_one` request at actual submission because Jev can evaluate multiple typed questions independently against the same state. Approval Advisor runs later only because the concrete provider approval object does not exist at task-submission time.

## Data minimization

ServerFS does not send the workdir contents to Jev just because the advisor is enabled.

Task-level evaluation receives only the task context needed for the advisory questions. Model Advisor additionally receives the normalized model metadata already returned by native discovery; raw provider payloads and provider credentials are never sent. Approval evaluation uses the existing redacted approval object, restricts it to an allowlisted field set, and applies another sanitizer for obvious secret/token/password/credential fields and patterns.

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
- Qoder routing and pre-submit model advice are included in v0.9.0 and remain advisory-only;
- human-review decisions;
- intentionally bundled or vague prompts;
- bounded one-time writes;
- repeated session-scoped writes;
- outside-workdir approval requests.

The project intentionally does **not** use automatic runtime routing, automatic model selection, or automatic approval thresholds.

Upstream references: [Introduction](https://docs.typesafe.ai/introduction), [Models](https://docs.typesafe.ai/models), and [API reference](https://docs.typesafe.ai/api).

For implementation and measured examples, see the repository notes:

- [Agent Task Preflight](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/jev-agent-preflight-experiment.md)
- [Runtime Router](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/jev-runtime-router-experiment.md)
- [Approval Advisor](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/jev-approval-advisor-experiment.md)
