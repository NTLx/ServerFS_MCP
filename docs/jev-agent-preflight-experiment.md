# Jev Agent Task Preflight experiment

Status: experimental branch `experiment/jev-agent-preflight`

This experiment evaluates whether TypeSafe Jev can improve the quality and routing signal of
ServerFS Agent delegation without becoming part of the authorization or safety boundary.

## Invariants

The experiment does **not** change:

- the ServerFS MCP tool surface or the `submit_agent_task` input contract;
- workdir/path authorization;
- runtime allowlists;
- writer-lease semantics;
- Codex/Claude adapters or native provider settings;
- approval/question brokerage;
- the MCP container's no-Internet-egress invariant.

Jev runs only in the host-side Agent Bridge. It is advisory-only and fail-open.

## Opt-in configuration

The only required experiment setting is:

```env
SERVERFS_JEV_API_KEY=
```

An empty or absent value disables all Jev functionality:

- the Bridge config omits the `jev` block;
- no `JevTaskPreflight` is constructed;
- no TypeSafe client is constructed;
- no Jev network request is made;
- `submit_agent_task` behaves as before and has no `preflight` result.

When a non-empty key is present, `deployment/agent-bridge/render_config.py` writes it only
to the user-owned `~/.config/serverfs-agent-bridge/config.json`, mode `0600`. The key is
held in `JevSettings.api_key` with `repr=False` and is never returned through Bridge RPC,
MCP results, task events, or logs.

## Model and SDK

The experiment pins:

- TypeSafe Python SDK: `typesafe-sdk==0.7.1`
- Jev model: `jev-1.13.0`

The model is pinned rather than using `jev-latest` so repeated evaluations can be compared
against one stable model version.

Official API documentation:

- https://docs.typesafe.ai/introduction
- https://docs.typesafe.ai/api
- https://docs.typesafe.ai/models
- https://docs.typesafe.ai/sdk/python

## Preflight questions

Each task is sent as structured state containing only:

- runtime name;
- workdir alias;
- relative path;
- execution profile;
- whether it is a continuation;
- the submitted prompt.

No host path or file content is added by ServerFS.

Jev evaluates four independent Noul questions:

1. `single_objective` — whether the task is one narrow objective rather than a bundle;
2. `mutation_boundary_explicit` — whether allowed mutations, or explicit no-mutation intent,
   are bounded;
3. `stop_condition_explicit` — whether completion/failure stop conditions are clear;
4. `verification_evidence_explicit` — whether the task asks for concrete observable evidence.

It also evaluates one Choice question:

- `structured_serverfs` — bounded ServerFS filesystem primitives are sufficient;
- `native_agent` — shell/build/test/Git/deployment/provider-native or broader coding-Agent
  capability is required;
- `unclear` — the task is too ambiguous or mixed to choose confidently.

No numeric threshold currently changes execution.

## Runtime behavior

With Jev enabled, `submit_agent_task` performs preflight after deterministic authorization
and continuation validation, but before acquiring the writer lease.

A successful submission may return:

```json
{
  "task_id": "agt_...",
  "status": "queued",
  "preflight": {
    "status": "completed",
    "model": "jev-1.13.0",
    "answers": {
      "single_objective": 0.93,
      "mutation_boundary_explicit": 0.91,
      "stop_condition_explicit": 0.88,
      "verification_evidence_explicit": 0.86,
      "execution_fit": {
        "choice": "native_agent",
        "confidence": 0.82,
        "probabilities": {
          "structured_serverfs": 0.12,
          "native_agent": 0.82,
          "unclear": 0.06
        }
      }
    },
    "usage": {
      "input_tokens": 0
    }
  }
}
```

The same normalized payload is persisted as a `task.preflight` event.

If TypeSafe is unavailable, times out, rate-limits, rejects the request, or returns an
unexpected response, the Bridge returns and records:

```json
{"status": "unavailable"}
```

and continues the already-authorized Agent task. The external model never decides whether
the task is authorized.

## Live evaluation protocol

Do not put a real API key into tracked files.

1. Set `SERVERFS_JEV_API_KEY=<key>` in the repository's existing untracked `.env`.
2. Before updating the Bridge, confirm no Agent task is active.
3. Run the normal user-scoped Bridge installer/update path.
4. Verify the host deployment and the existing Compose overlay.
5. Submit a small corpus containing:
   - clearly atomic Agent tasks;
   - intentionally bundled tasks;
   - direct ServerFS file-operation tasks;
   - tasks requiring tests/Git/build/deployment;
   - English prompts;
   - Chinese prompts.
6. Record the raw probabilities/confidences and compare them with human labels.
7. Do not add blocking thresholds or automatic routing until the corpus demonstrates that a
   threshold is useful and its false-positive/false-negative behavior is understood.

The first live test should verify only that the configured path works and that disabling the
key restores the exact baseline behavior. It should not change production routing policy.

The first real-key activation and sample results are recorded in
[`jev-agent-preflight-live-validation-2026-09-22.md`](jev-agent-preflight-live-validation-2026-09-22.md).

The same task-submission Jev request also carries the second experiment, the advisory Runtime
Router; see [`jev-runtime-router-experiment.md`](jev-runtime-router-experiment.md). The router
adds no second network request and does not alter the explicit runtime contract. A third
experiment, [`jev-approval-advisor-experiment.md`](jev-approval-advisor-experiment.md), runs
only when a native provider actually creates an approval request.
