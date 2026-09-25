# Jev Runtime Router experiment

Status: opt-in experimental capability included in the current **v0.7.2 stable release** (the original `experiment/jev-agent-preflight` branch was removed after fast-forward merge)

This experiment adds the second Jev feature proposed for ServerFS: an advisory Runtime
Router layered on the existing Agent Task Preflight.

## Goal

For every Jev-enabled `submit_agent_task`, recommend one of four execution routes:

- `direct_serverfs_tool`
- `codex`
- `claude`
- `human_review`

The recommendation is advisory only. It does not change the submitted runtime, authorize
anything, block the task, rewrite the prompt, invoke a different MCP tool, or create a
human approval request.

The existing explicit `runtime=codex|claude` public contract remains unchanged. There is
no `runtime=auto`.

## Why the router shares the Preflight request

The Runtime Router is implemented as one additional Jev Choice question in the existing
`system_one` request. This keeps the following properties:

- one external Jev request per delegated task;
- the same pinned `jev-1.13.0` model;
- the same API-key master gate and fail-open behavior;
- no additional MCP tool or Bridge RPC method;
- no additional network round trip solely for routing.

The existing Preflight answers and the new route recommendation are therefore produced from
the same task state.

## Route semantics

### direct_serverfs_tool

Recommend bounded ServerFS filesystem primitives when they are sufficient: list, find,
search, read, stat, create, edit, delete, upload, or download.

Do not recommend this route when the task requires shell commands, Git, test runners,
builds, deployment, provider-native sessions, or broad coding-agent reasoning.

### codex

Recommend the Codex native Agent route for shell/Git/test/build/deployment/general
coding-agent work, explicit Codex requests, or tasks that require live steering, unless a
Claude-specific requirement is present.

### claude

Recommend the Claude Code native Agent route when the task explicitly requests Claude or
Claude Code, or requires Claude-specific sessions, settings, skills, or provider-native
behavior.

### human_review

Recommend human review when an automated route should not be chosen yet because the task
requires human authorization/business judgment or is too ambiguous/underspecified to route
responsibly.

`human_review` is a routing recommendation, not an approval mechanism.

## Result shape

A Jev-enabled `submit_agent_task` keeps the existing `preflight` field and adds:

```json
{
  "routing_advice": {
    "status": "completed",
    "model": "jev-1.13.0",
    "requested_runtime": "codex",
    "recommendation": {
      "choice": "direct_serverfs_tool",
      "confidence": 0.91,
      "probabilities": {
        "direct_serverfs_tool": 0.91,
        "codex": 0.05,
        "claude": 0.01,
        "human_review": 0.03
      }
    },
    "matches_requested_runtime": false,
    "automatic": false
  }
}
```

The same advisory object is persisted as `task.routing_advice`.

If Jev is unavailable, both Preflight and Runtime Router degrade to:

```json
{"status": "unavailable"}
```

and the already-authorized task continues on the explicitly requested runtime.

## Security and authority boundary

The Runtime Router has no authority over:

- workdir/path authorization;
- runtime allowlists;
- Agent profiles;
- writer leases;
- provider approvals or questions;
- MCP filesystem mutations;
- Codex/Claude settings or credentials;
- provider safety checks.

A recommendation that disagrees with the requested runtime is evidence for evaluation only.

## Evaluation

Router evaluation should cover at least:

- bounded read-only filesystem tasks;
- bounded filesystem mutations that do not require shell/Git;
- Git/test/build/deployment tasks;
- explicit Codex requests;
- explicit Claude/Claude Code requests;
- tasks requiring human authorization or business judgment;
- intentionally vague/open-ended tasks;
- Chinese and English prompts.

Evaluate the existing Preflight signals at the same time. In particular, route quality must
not be inferred only from `execution_fit`: the four Noul task-quality signals can identify
ambiguity and poor verification criteria even when a route Choice is confident.

Do not enable automatic routing until a larger labeled corpus demonstrates useful accuracy,
stable behavior across languages, and acceptable false-routing cost.

The first joint real-key evaluation is recorded in
[`jev-runtime-router-live-validation-2026-09-22.md`](jev-runtime-router-live-validation-2026-09-22.md).
The third advisory capability is documented separately in
[`jev-approval-advisor-experiment.md`](jev-approval-advisor-experiment.md); it runs only for
actual provider approval requests and does not add a task-submission round trip.
