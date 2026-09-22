# Jev Approval Advisor experiment

Status: opt-in experimental capability on `main` (developed on `experiment/jev-agent-preflight`)

This experiment adds the third Jev-backed advisory capability to the ServerFS Agent Bridge:
an Approval Advisor for provider-originated approval requests.

## Goal

When Codex or Claude pauses an active task and asks the user to approve a command, file
change, tool call, provider permission, or similar capability, the Bridge may ask Jev for a
structured risk/fit assessment before exposing the pending approval to the user.

The Advisor is **advisory only**. It does not:

- approve or deny anything;
- choose a provider-native approval decision automatically;
- expand a permission request;
- alter provider approval policy;
- bypass ServerFS workdir/runtime policy;
- bypass provider safety checks;
- replace the existing explicit `respond_agent_approval` flow.

The human/ChatGPT caller still supplies the final approval decision through the existing
Bridge contract.

## Request economy

Approval Advisor deliberately does not run at ordinary task submission.

Task submission already performs one Jev `system_one` call containing the task-quality
Preflight and Runtime Router questions. The concrete approval object does not exist yet at
that point, so evaluating approval risk there would be speculative and unreliable.

Approval Advisor therefore performs **one additional Jev call only when a provider actually
creates an approval request**. All approval judgments are parallel questions in that one
request:

- `necessary_for_objective`
- `scope_bounded`
- `destructive_or_irreversible`
- `sensitive_access`
- `external_side_effect`
- `recommendation`

Questions and normal Agent turns add no Approval Advisor request.

## Approval state

The approval call receives:

- task runtime;
- workdir alias;
- relative path;
- execution profile;
- the current task/turn prompt;
- a minimized approval object.

The minimized approval object uses a fixed top-level allowlist:

- `category`
- `title`
- `reason`
- `command_display`
- `relative_cwd`
- `additional_permissions`
- `network_approval_context`
- `file_changes`
- `requested_permissions`
- `tool`
- `blocked_path`
- `available_decisions`

Raw generic `tool_input` is intentionally omitted. Before this stage the Bridge has already
redacted host paths. The Jev-specific sanitizer additionally strips nested values whose keys
look like secrets/credentials/tokens/passwords, redacts common Authorization/Bearer and
secret-assignment forms inside strings, truncates large text, and bounds list size.

This minimization is not a general secret scanner. It exists to reduce unnecessary disclosure
to the external TypeSafe API while preserving the information needed for risk judgment.

## Advisor outputs

### necessary_for_objective

Noul probability that the requested capability/action is materially needed for the stated
task.

### scope_bounded

Noul probability that the requested scope, target, duration, and side effects are
proportionate and minimally sufficient.

### destructive_or_irreversible

Noul probability that approval may delete, overwrite, reset, publish, deploy, permanently
alter, or otherwise create difficult-to-recover effects.

### sensitive_access

Noul probability that the request involves credentials, secrets, protected/outside paths,
elevated permissions, or unusually broad access.

### external_side_effect

Noul probability that approval may mutate something outside the local workdir/analysis
state, such as a remote service, deployment, push, publish, or message send.

### recommendation

Choice among:

- `approve_once`
- `approve_session`
- `deny`
- `cancel_task`
- `review_carefully`

The recommendation remains advisory. `approve_session` should only be suggested when
session scope is offered and repeated narrow access is genuinely needed.
`review_carefully` means the model does not have enough confidence/context for a concrete
decision.

The normalized result also reports whether the recommended provider decision is actually
present in `available_decisions`. `review_carefully` is treated as an advisory meta-choice
rather than a provider-native decision.

## Bridge integration

For Jev-enabled deployments, an approval request is exposed through the existing pending
request payload with an additional field:

```json
{
  "kind": "approval",
  "payload": {
    "category": "command",
    "command_display": "uv run pytest ...",
    "available_decisions": [
      "approve_once",
      "approve_session",
      "deny",
      "cancel_task"
    ],
    "approval_advice": {
      "status": "completed",
      "model": "jev-1.13.0",
      "answers": {
        "necessary_for_objective": 0.95,
        "scope_bounded": 0.91,
        "destructive_or_irreversible": 0.05,
        "sensitive_access": 0.03,
        "external_side_effect": 0.01,
        "recommendation": {
          "choice": "approve_once",
          "confidence": 0.88,
          "probabilities": {}
        }
      },
      "recommended_decision_available": true,
      "automatic": false
    }
  }
}
```

The same advisory result is persisted as an `approval.advice` event.

If Jev is unavailable, the pending approval still appears normally with:

```json
{
  "approval_advice": {
    "status": "unavailable",
    "automatic": false
  }
}
```

The human approval path remains fully usable.

## Authority boundary

The deterministic order remains:

1. ServerFS workdir/runtime/profile policy authorizes the task.
2. The native provider runs the task under its own settings and safety behavior.
3. The provider decides whether an operation requires approval.
4. Approval Advisor may evaluate the already-created approval request.
5. A human/ChatGPT caller decides through `respond_agent_approval`.
6. The Bridge validates that the selected decision was offered by the provider and that any
   granted permission ID belongs to the pending request.
7. Only then is the decision returned to the provider.

Jev never becomes step 5 or step 6.

## Evaluation

Live validation should cover at least:

- narrow read-only/local command approvals;
- bounded file modifications;
- session-scope requests;
- destructive commands;
- outside-workdir/protected-path access;
- network/external side effects;
- provider permissions with multiple permission IDs;
- Codex and Claude approval payloads;
- Chinese and English task prompts.

Evaluate false-safe and false-risk judgments separately. A useful approval advisor must not
only identify dangerous requests; it must also avoid making routine, well-bounded approvals
look risky.

Do not add automatic approval or denial thresholds during this experiment.

## Identical-approval cache

To reduce avoidable Jev traffic, completed Approval Advisor results are cached only within the
lifetime of the current task. The cache key is a SHA-256 fingerprint of the normalized,
redacted approval payload. If the provider emits the same approval payload again in that task,
the Bridge reuses the prior completed advice, adds `cached=true`, and makes no second Jev
request. Different approval payloads are evaluated independently. Failed/unavailable advice
is not cached, and the entire task-local cache is discarded when the task terminates.

This cache does not authorize or resolve an approval; it only reuses advisory context.

The first real-provider validation is recorded in
[`jev-approval-advisor-live-validation-2026-09-22.md`](jev-approval-advisor-live-validation-2026-09-22.md).
