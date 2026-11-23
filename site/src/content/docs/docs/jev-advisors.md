---
title: Jev Advisors
description: Optional TypeSafe Jev advice for Agent tasks, routing, model selection and approvals.
---

ServerFS can use [TypeSafe Jev](https://docs.typesafe.ai/introduction) inside the host Agent Bridge. Jev evaluates structured questions against task context rather than executing Agent work. It is opt-in and advisory only; it is neither an Agent runtime nor an authorization boundary.

## Decide whether to enable it

Enabling Jev permits task context to leave the host for the external TypeSafe API. Leave it disabled if that transfer is not authorized. Do not include secrets, private file contents or unnecessary personal information in task prompts. The Bridge does not automatically upload workdir files, but task-submission preflight sends the supplied prompt itself, without the model/approval advisor's additional text sanitizer.

When disabled, no Jev client is constructed and no advisory request is made. Agent execution and explicit provider approval do not depend on Jev.

## Configure the owner

Configure Jev in the selected Bridge owner's private configuration, outside exposed workdirs. For a named Linux owner, the following is a fragment of the private Bridge JSON, not Core environment or a full owner configuration:

```json
{
  "jev": {
    "api_key": "<private TypeSafe API key>",
    "use_proxy": false
  }
}
```

Omit the `jev` section or leave the key empty to disable advice. Use the [Linux owner provisioning procedure](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/linux/README.md) and protected configuration. The older Linux singleton renderer uses `SERVERFS_JEV_API_KEY` and `SERVERFS_JEV_USE_PROXY` in a private env file and renders them to Bridge JSON. macOS's legacy native configuration helper also understands those env keys; named macOS owned staging does not pass that private overlay. Do not assume a native helper exposes Jev provisioning merely because the Bridge parser accepts it. Follow the exact supported owner procedure for [macOS](../macos-native/) or [Windows](../windows-native/); do not edit an installed owner snapshot or shared grant in place.

With `jev.use_proxy=true`, the private JSON must contain a valid shared `proxy` configuration. Jev uses an explicit HTTP client and does not modify process-wide proxy variables. With false, ServerFS supplies no explicit Jev proxy; verify the owner's environment and actual routing if isolation matters. Jev alone can use an authenticated shared endpoint; an Agent using that same endpoint requires it to be credentialless. Tunnel routing is independent.

The Bridge pins `jev-1.13.0` and `typesafe-sdk==0.7.1`. A model upgrade needs separate calibration. The client configures a five-second timeout and retry policy (`max_retries=1`, timeout eight seconds); a logical advisor call is not necessarily one network attempt.

## Four advisors

| Advisor | Trigger | Output and authority |
| --- | --- | --- |
| Task Preflight | Authorized Agent submission | Scores whether the task is narrow, scoped, bounded and verifiable, and evaluates execution fit. |
| Runtime Router | Same submission request | Suggests `direct_serverfs_tool`, `codex`, `claude`, `qoder` or `human_review`; no automatic/ACP routing option. |
| Model Advisor | Explicit task context in model discovery | Recommends an eligible provider-native model; never changes the submission automatically. |
| Approval Advisor | Actual provider approval | Assesses necessity, scope, irreversible risk, sensitive access and external effects; does not resolve approval. |

Task Preflight and Runtime Router share one logical assessment before execution. The router's fixed choices and criteria are not current capability discovery and do not cover every structured operation (including Repo Ops). Use actual tools, grants, policy and provider capabilities to decide.

For model advice, first choose an allowed runtime. Call `list_agent_models` with that runtime plus both `workdir` and `task_prompt` (optional relative `path` and `profile`). Omit task context for catalog-only discovery. Codex and Qoder expose native catalogs; Claude and ACP do not. The advisor considers unique, non-hidden, non-disabled candidate IDs (1–32), using only available metadata; it does not estimate cost or quality from names. Returned advice contains `recommended_model`, confidence/probabilities and `automatic=false` when completed. The caller may explicitly submit the chosen model only where the runtime supports it.

Approval advice runs only for a real approval. Provider-specific `response_options` lists, such as those used by ACP, are not applicable to this generic advisor. For supported approval shapes, the advice can recommend `approve_once`, `approve_session`, `deny`, `cancel_task` or `review_carefully`. Check `recommended_decision_available` and the actual pending request; do not treat a recommendation as an available option or authorization. The caller must explicitly respond via `respond_agent_approval` or follow the interaction expiry/cancellation path.

## Request and data boundaries

- Preflight/router: runtime, alias, relative path, profile, continuation indicator and original task prompt.
- Model advice: the task context and normalized eligible model metadata; text is sanitized and bounded.
- Approval advice: the task context and an allowlisted projection of the already path-redacted approval; another sanitizer masks obvious secret patterns. Only completed advice is cached for identical approvals in the same task.

The model/approval sanitizer caps strings at 4096 characters before the truncation marker and lists at 50 entries. This is best-effort filtering, not a guarantee of confidentiality. Different approvals can cause additional calls; failed evaluations are not cached. Retries may add network attempts. No provider raw payload or workdir scan is needed for model advice.

## Failure and verification

A failed advisor call (timeout, rate limit, invalid response) reports `unavailable`; missing catalogs or unsupported approval shapes can report `not_applicable`, and model advice can report `disabled`. An otherwise authorized Agent task or provider approval follows its normal path. Fail-open means only missing advice, never widened permissions or automatic approval. Configuration validation is separate and can fail closed.

Use the selected owner's runbook to verify private configuration and readiness. Inspect task preflight/routing and approval advice events or model advice results in authorized test tasks; do not print the API key. Record actual provider/network acceptance separately from source support.

Historical evaluation records: [Task Preflight](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/jev-agent-preflight-experiment.md), [Runtime Router](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/jev-runtime-router-experiment.md), [Approval Advisor](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/jev-approval-advisor-experiment.md). They are not current named-instance acceptance. See [Agent Bridge](../agent-bridge/) for task operations and [TypeSafe model documentation](https://docs.typesafe.ai/models) for upstream model limits.
