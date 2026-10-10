# ServerFS v0.9.0 Release Record — Runtime Model Discovery, Advisory & Per-Task Selection

Status: released / frozen historical record; v0.9.0 release complete; repository current stable: v0.13.0
Baseline: v0.8.0 / main
Scope: add provider-neutral model discovery, optional Jev-backed pre-submit model advice, and an optional per-task model override without adding model defaults to ServerFS configuration or modifying users' native Agent configuration; also make `SERVERFS_MAX_BINARY_TRANSFER_BYTES` the single public global size limit for both native binary transfer and file-ingress fetches.

## 1. Problem statement

ServerFS v0.8.0 intentionally leaves model selection entirely to each native Agent runtime. That preserves provider-native behavior, but it creates three practical gaps:

1. ChatGPT/the user cannot ask ServerFS which model identifiers a configured runtime currently exposes before choosing one.
2. Even when a runtime can enumerate its models, ChatGPT has no provider-neutral way to ask the already-enabled Jev advisor which currently available model best fits the concrete task it is about to delegate.
3. `submit_agent_task` cannot request a specific model for one task/session turn; the only supported behavior is to inherit the runtime's native default/session configuration.

There is also one avoidable deployment inconsistency: the MCP binary channel uses `SERVERFS_MAX_BINARY_TRANSFER_BYTES`, while the isolated file-ingress sidecar separately uses `SERVERFS_FILE_INGRESS_MAX_BYTES`. A user must currently keep two global size limits aligned for one end-to-end binary-transfer feature.

v0.9.0 closes these gaps while keeping ServerFS out of persistent model policy and keeping the file-ingress sidecar workdir-unaware.

## 2. Goals

v0.9.0 adds exactly two model-facing public capabilities plus one configuration simplification:

1. A new provider-neutral MCP tool, `list_agent_models`, backed by a new Bridge RPC `runtime.models`. The tool can optionally include the concrete task context; when Jev is enabled and the selected runtime exposes a usable model catalog, the same call returns a Jev model recommendation before any Agent task is submitted.
2. An optional `model` argument on `submit_agent_task` / `task.submit`.
3. `SERVERFS_MAX_BINARY_TRANSFER_BYTES` becomes the single public global byte limit for native binary upload/download and file-parameter ingress; `SERVERFS_FILE_INGRESS_MAX_BYTES` is removed from the documented `.env` surface and retained only as a backwards-compatible sidecar alias where needed.

Core semantics:

- `model=null` / omitted means **no ServerFS override**. The native runtime remains authoritative for its default model or resumed-session model.
- An explicit `model` applies only to that submitted task/session operation.
- ServerFS never writes the selected model into `.env`, Bridge config, workdir config, or the provider's user/project/local configuration.
- ServerFS never silently falls back to another model when an explicit model is rejected.
- Model discovery is advisory evidence for selection, not an entitlement pre-check.
- Jev model advice is **advisory only**: it never fills, rewrites, or overrides `submit_agent_task.model`. ChatGPT remains the decision-maker and may accept, ignore, or override the recommendation.
- If the user explicitly requested a model, ChatGPT should normally honor that request directly rather than asking Jev to substitute a different model unless the user specifically asks for a comparison/recommendation.

## 3. Invariants and non-goals

The following existing architecture remains authoritative:

- ServerFS MCP stays provider-neutral.
- Provider SDK/CLI integration remains inside Agent Bridge.
- Runtime/workdir authorization and writer leases are unchanged.
- Provider credentials remain on the host and are not copied into the MCP container.
- Native user/project/local Agent settings remain authoritative unless the caller explicitly supplies `model` for the current submission.

v0.9.0 does **not** add:

- `SERVERFS_*_MODEL` or any other model variable to `.env` / `.env.example`;
- persistent per-runtime, per-workdir, or per-user model defaults in ServerFS;
- `runtime=auto` or ServerFS-owned automatic model routing;
- automatic model selection, model fallback, or policy-enforced acceptance of Jev recommendations;
- a ServerFS-authored static cost/quality ranking; Jev may reason only over the task plus model metadata actually exposed by the runtime;
- reasoning-effort, context-window, service-tier, BYOK credential, or provider selection overrides;
- automatic fallback to another model after a model error;
- provider-specific public MCP tools;
- model probing by submitting inference prompts;
- undocumented/private provider APIs merely to make all runtimes look symmetric;
- a claim that a discovered catalog entry guarantees the account can successfully run that model.

## 4. Public MCP contract

### 4.1 New tool: `list_agent_models`

Proposed signature:

```text
list_agent_models(
    runtime: "codex" | "claude" | "qoder",
    workdir: string | null = null,
    task_prompt: string | null = null,
    path: string = "",
    profile: "review" | "workspace-write" = "workspace-write"
)
```

The tool is available only when Agent Bridge is enabled. As with `list_agent_runtimes`, a runtime must be allowlisted by at least one configured ServerFS workdir before its model surface is exposed.

Two call modes are valid:

- **Discovery only**: provide only `runtime`.
- **Discovery + pre-submit advice**: provide both `workdir` and `task_prompt`; `path` and `profile` describe the task ChatGPT is considering. The task is not submitted and no writer lease is acquired.

Supplying only one of `workdir` / `task_prompt`, or supplying task-only fields without a task prompt, is an `INVALID_REQUEST` contract error. Advice-mode authorization validates the same runtime/workdir/profile/path relationship that a later `submit_agent_task` would use, but performs no mutation.

The tool calls:

```json
{
  "method": "runtime.models",
  "params": {"runtime": "qoder"}
}
```

Normalized response:

```json
{
  "runtime": "qoder",
  "status": "ok",
  "scope": "current_account",
  "source": "qoder_agent_sdk",
  "models": [
    {
      "id": "qfmodel",
      "display_name": "Qwen3.8-Flash",
      "description": "...",
      "enabled": true,
      "is_default": null,
      "hidden": null,
      "input_modalities": null,
      "is_free": true,
      "price_factor": 0,
      "context": null,
      "reasoning": null,
      "service_tiers": null
    }
  ],
  "model_advice": {
    "status": "completed",
    "advisor_model": "jev-1.13.0",
    "recommended_model": "qfmodel",
    "confidence": 0.82,
    "probabilities": {
      "qfmodel": 0.82,
      "qmodel_38max": 0.18
    },
    "automatic": false
  }
}
```

Required top-level fields:

- `runtime`
- `status`: `ok | unsupported | unavailable`
- `scope`: `runtime_catalog | current_account | none`
- `source`
- `models`

`model_advice` is included only when advice mode was requested. Its status is one of `completed | disabled | unavailable | not_applicable`. `automatic` is always `false`.

Optional model fields are omitted or `null` when the provider does not expose them. `id` is always the exact identifier that callers should pass back as `submit_agent_task.model`.

Do not expose an unbounded raw provider payload. Normalize only stable, useful selection metadata:

- exact selectable model ID;
- display name and description;
- enabled/hidden/default flags when known;
- input modalities when known;
- reasoning-effort choices/default when known;
- context-window choices/default when known;
- free/price-factor metadata when explicitly provided by the runtime;
- service-tier metadata when explicitly provided by the runtime.

### 4.2 Runtime capability advertisement

Extend `RuntimeInfo` / `list_agent_runtimes` additively with:

```json
{
  "model_override": true,
  "model_discovery": "runtime_catalog"
}
```

Allowed `model_discovery` values:

- `runtime_catalog`
- `current_account`
- `unsupported`

Expected production values:

- Codex: `model_override=true`, `model_discovery="runtime_catalog"`
- Claude: `model_override=true`, `model_discovery="unsupported"`
- Qoder: `model_override=true`, `model_discovery="current_account"`

This lets ChatGPT decide whether it can discover models before submitting a task without guessing from the runtime name.

### 4.3 Jev Model Advisor

Reuse the existing opt-in Jev client already used by Task Preflight, Runtime Router and Approval Advisor. Do not introduce a second Jev credential, client, model, or configuration switch.

Add one narrow method to the existing advisor abstraction, conceptually:

```python
async def advise_model(
    *,
    runtime: str,
    workdir: str,
    path: str,
    profile: str,
    prompt: str,
    models: list[dict[str, Any]],
) -> dict[str, Any]
```

Invocation rules:

- call it only when advice mode was explicitly requested through `list_agent_models`;
- call it only when the runtime discovery result is `status="ok"` and contains selectable candidates;
- if Jev is not configured, return `model_advice.status="disabled"` and make no Jev request;
- if discovery is unsupported (for example Claude in the v0.9.0 baseline), return `not_applicable` and make no Jev request;
- if discovery or Jev fails, return bounded `unavailable` advice while preserving the successful model catalog if one exists;
- never call an Agent model or submit a provider task merely to generate advice.

Jev receives only sanitized task context plus normalized runtime model metadata. Reuse the existing secret-redaction helpers. Never send provider credentials, raw provider payloads, filesystem contents not already present in the task prompt, or native session state.

The recommendation question is a single dynamic choice over the currently selectable model IDs. The implementation may internally map long/provider-specific IDs to short deterministic choice labels, but the public response must map the result back to the exact model ID. Consider only models that are not explicitly disabled and not explicitly hidden. If the eligible set exceeds a bounded advisor-candidate limit (target: 32), return `model_advice.status="unavailable"` rather than silently dropping arbitrary candidates.

Recommendation criteria:

- fit to the concrete task and requested runtime;
- reasoning/context/modalities exposed by the runtime;
- latency/free/price metadata only when explicitly provided by the runtime;
- no assumptions from model names alone when metadata is absent;
- no presumption that the largest/most expensive model is best.

The normalized result includes the exact `recommended_model`, Jev confidence/probabilities when available, `advisor_model`, and `automatic=false`. ServerFS never copies the recommendation into `submit_agent_task.model`; ChatGPT must make the separate submission decision.

This pre-submit advice is intentionally distinct from the existing submit-time Task Preflight. A caller that first requests model advice and later submits a task may therefore cause two Jev requests with different purposes. v0.9.0 does not add cross-RPC advisory caching or advisor tokens solely to avoid that small, explicit cost.

## 5. Provider discovery implementations

### 5.1 Codex

Use the existing Codex App Server connection and native `model/list` RPC.

Discovery behavior:

- page through `model/list` using the native cursor;
- normalize `model` as the public `id`;
- preserve useful stable fields such as `displayName`, `description`, `isDefault`, `hidden`, input modalities, supported/default reasoning effort, and service tiers when present;
- bound the total result count and Bridge response size;
- never start an inference turn.

Important semantic boundary: Codex `model/list` is a runtime catalog suitable for a selector, not an entitlement guarantee. ServerFS must not reject an explicit submission merely because the chosen ID was absent from a previous catalog response.

### 5.2 Qoder

Prefer the structured SDK API already present in the pinned `qoder-agent-sdk==1.0.15`:

```python
await client.get_available_models()
```

The installed SDK exposes `ModelInfo` with the useful fields needed for detailed discovery, including:

- `value`
- `displayName`
- `description`
- `isEnabled`
- optional `isFree`, `priceFactor`, `isNew`
- optional `context_config`
- optional `thinking_config`
- optional promotion/server model metadata

Normalize `value` as the public `id`.

The discovery path must use a short-lived authenticated native Qoder SDK/CLI control session with **no user prompt and no inference request**. The SDK may create transient auth/session files and a qodercli subprocess as part of native control-session lifecycle; this is acceptable for v0.9.0 only if validation proves that user/project/local configuration files are not modified. Always disconnect/cleanup on success, error, cancellation, and timeout.

Do not use `qodercli --list-models` as the primary detailed source: the installed CLI returns a human table and truncates long custom model identifiers. It remains useful as validation/fallback evidence only.

### 5.3 Claude Code

The installed Claude Code / `claude-agent-sdk` supports explicit model selection, but currently exposes no documented stable, scriptable API that enumerates the current Claude Code account's selectable models without issuing inference.

Therefore v0.9.0 deliberately returns:

```json
{
  "runtime": "claude",
  "status": "unsupported",
  "scope": "none",
  "source": "claude_code",
  "models": []
}
```

Do not substitute Anthropic's general Models REST API: that is a different authentication/account surface and would violate the native-runtime boundary. Do not scrape the interactive `/model` UI or hard-code a static model list.

Users/ChatGPT may still provide a documented Claude model ID or alias explicitly to `submit_agent_task.model`.

## 6. Per-task model override

Extend the public tool and Bridge RPC additively:

```text
submit_agent_task(
    runtime,
    workdir,
    prompt,
    ...,
    model: string | null = null
)
```

Tool description:

> Optional provider-native model identifier for this task/session request only. Omit it to send no model override and preserve the runtime's native default or resumed-session model. This does not modify Agent configuration.

Validation:

- `null` is accepted and means omitted/no override;
- non-null value must be a non-empty UTF-8 string;
- reject leading/trailing whitespace, NUL/newline/control characters, and an excessive byte length;
- do not impose provider-specific model-name syntax in ServerFS;
- do not prevalidate against `list_agent_models`.

A conservative maximum of 512 UTF-8 bytes is sufficient for normal and custom model identifiers while avoiding unbounded metadata.

## 7. Adapter mapping

### 7.1 Codex

Add `requested_model` to `TaskContext`.

For a new session:

- when `requested_model is None`, omit `model` from `thread/start`;
- otherwise pass `model=requested_model`.

For continuation:

- when omitted, resume the native thread without a model override;
- when explicit, pass `model=requested_model` to `thread/resume`.

The installed App Server protocol supports model overrides on both start and resume.

### 7.2 Claude

Construct `ClaudeAgentOptions` with the request-scoped model:

- omitted -> `model=None` / no effective ServerFS override;
- explicit -> `model=requested_model`.

Keep `setting_sources=["user", "project", "local"]` and all existing permission/system-prompt behavior unchanged.

### 7.3 Qoder

Remove the production/test-only constructor-level `model_override` special path and move model selection into `TaskContext`.

Construct `QoderAgentOptions(model=context.requested_model, ...)`.

Update the Qoder live-smoke path to exercise the same public/service request-scoped model mechanism used in production instead of injecting a special adapter constructor override.

## 8. Continuation semantics

The model belongs to the **new ServerFS submission**, not to ServerFS persistent configuration.

Rules:

- continuation with `model` omitted: do not send a new model override; let the provider resume its existing/native model state;
- continuation with explicit `model`: request that model through the provider's native resume/session mechanism;
- if a provider rejects changing the model on a resumed session, surface the native failure; do not silently start a new session or fall back.

The prior task's requested model is not automatically copied into a new ServerFS task. This preserves the meaning of omission: “provider-native behavior”.

## 9. Persistence, idempotency and observability

A requested model changes task identity and must be durable evidence.

Add an additive SQLite task column:

```text
requested_model TEXT NULL
```

Update:

- `TaskRecord`
- task creation/read migration paths
- `get_agent_task` result
- task submission fingerprint
- idempotency conflict detection
- immutable execution manifest

Consequences:

- same `idempotency_key` + same request + same model -> same logical submission;
- same `idempotency_key` + different model -> `AGENT_IDEMPOTENCY_CONFLICT`.

Bump execution manifest schema from 1 to 2 and record:

```json
"model": {
  "requested": "qfmodel"
}
```

Do **not** add a cross-provider `effective_model` claim in v0.9.0. Providers may route internal subagents, compaction, title generation, or other secondary calls differently; ServerFS can reliably attest to what it requested, not every model a provider ultimately used.

## 10. Bridge protocol compatibility

Add:

- RPC method `runtime.models`;
- optional `model` field on `task.submit`;
- additive model-capability fields on runtime info.

Keep UDS `PROTOCOL_VERSION=1` for this release. The project has already treated additive RPC methods/optional fields as compatible extensions; bumping the protocol would unnecessarily break all existing calls between mixed versions rather than only failing the new capability.

Strict key/type validation remains in force.

## 11. Configuration and deployment

### 11.1 Model configuration

There is intentionally **no model configuration surface** in:

- repository `.env`;
- `.env.example`;
- `agent_bridge/config.example.json`;
- Compose environment rendering;
- systemd/provider environment;
- per-workdir ServerFS policy.

Existing runtime enablement/binary/probe settings remain unchanged.

Documentation should explicitly state:

> ServerFS configures whether a runtime may be used, not which model that runtime should use by default.

### 11.2 One global binary-transfer size setting

Make `SERVERFS_MAX_BINARY_TRANSFER_BYTES` the single documented global byte ceiling for the complete binary-transfer path.

Current v0.8.0 behavior is split:

- `serverfs-mcp` reads `SERVERFS_MAX_BINARY_TRANSFER_BYTES`;
- `serverfs-file-ingress` reads `SERVERFS_FILE_INGRESS_MAX_BYTES`.

v0.9.0 behavior:

- `serverfs-mcp` continues to read `SERVERFS_MAX_BINARY_TRANSFER_BYTES` unchanged;
- `serverfs-file-ingress` reads `SERVERFS_MAX_BINARY_TRANSFER_BYTES` first;
- the old `SERVERFS_FILE_INGRESS_MAX_BYTES` remains accepted only as a backwards-compatible sidecar alias when the new variable is absent/empty;
- if both are present, `SERVERFS_MAX_BINARY_TRANSFER_BYTES` wins;
- `.env.example` removes `SERVERFS_FILE_INGRESS_MAX_BYTES` and documents that the ingress fetch ceiling inherits `SERVERFS_MAX_BINARY_TRANSFER_BYTES`;
- `compose.yml` passes the new variable into the sidecar. To preserve legacy `.env` deployments during the transition, it may also pass the old alias as an empty/optional compatibility input, but only the new variable is public/documented.

Default remains 8 MiB when neither value is configured.

Per-workdir `WORKDIR_XX_MAX_BINARY_TRANSFER_BYTES` remains unchanged and is still enforced by the MCP publication path. The isolated ingress sidecar stays deliberately workdir-unaware: it enforces only the global fetch ceiling. Therefore a stricter workdir limit may reject the final publication after a globally permitted fetch, and a workdir override does not increase the sidecar's global fetch ceiling. This preserves the existing isolation boundary instead of leaking workdir policy into the Internet-facing sidecar.

No new environment variable is introduced.

## 12. Errors and failure behavior

Model discovery:

- runtime not authorized/exposed -> existing runtime authorization failure;
- authorized runtime unavailable -> return `status="unavailable"`, `models=[]`, and a bounded normalized `detail`;
- runtime has no supported discovery API -> return `status="unsupported"`, not a fabricated empty “supported” catalog;
- provider discovery timeout/protocol failure -> return `status="unavailable"`, `models=[]`, and a bounded normalized `detail`;
- an empty Qoder result is treated as unavailable unless live validation proves an empty account catalog is a distinct reliable state;
- never cache an old list as if it were a fresh successful result.

Invalid protocol shapes, unknown runtimes, and authorization failures remain RPC/tool errors. Once an authorized runtime reaches the discovery operation, normal provider absence/failure is represented by the response status above so callers can distinguish `unsupported` from temporarily `unavailable` without parsing exception text.

Model submission:

- invalid ServerFS input shape -> `INVALID_REQUEST`;
- native provider rejects unknown/unavailable model -> preserve a normalized provider failure;
- no automatic fallback;
- no persistent setting mutation.

A dedicated `AGENT_MODEL_UNAVAILABLE` code should be added only if all three adapters expose sufficiently reliable structured evidence. Otherwise keep provider rejection under the existing provider-error contract rather than guessing from error text.

## 13. Test plan

### Root MCP tests

Add/extend tests proving:

- Agent tool surface grows from 9 to 10 tools only when Agent Bridge is enabled;
- `list_agent_models` schema and runtime allowlist filtering;
- `runtime.models` RPC mapping;
- `submit_agent_task.model` omitted -> field omitted/no override;
- explicit model -> exact string forwarded;
- model validation boundaries;
- audit records contain the requested model only where safe/needed, never credentials;
- discovery-only and discovery+advice argument-shape validation;
- advice mode never submits an Agent task or acquires a writer lease;
- `model_advice` remains optional and `automatic=false`.

### Jev Model Advisor tests

Prove:

- no Jev API key -> no Jev request and `model_advice.status="disabled"`;
- unsupported discovery -> no Jev request and `not_applicable`;
- normalized model catalog + task context are sanitized before Jev;
- disabled/hidden models are excluded from candidates;
- dynamic provider model IDs round-trip exactly through internal choice labels;
- completed advice returns exact model ID, confidence/probabilities and `automatic=false`;
- Jev failure is fail-open for discovery and never blocks the later Agent submission path;
- too many eligible candidates returns bounded `unavailable` rather than silently truncating the decision set;
- the advisor never mutates task/model configuration or auto-populates `submit_agent_task.model`.

### Binary ingress configuration tests

Prove:

- `SERVERFS_MAX_BINARY_TRANSFER_BYTES` controls the MCP binary limit as before;
- the same variable controls `serverfs-file-ingress` max bytes;
- legacy `SERVERFS_FILE_INGRESS_MAX_BYTES` is honored only when the new variable is absent/empty;
- new variable wins if both are present;
- neither present -> 8 MiB default;
- rendered Compose sidecar environment receives the unified setting;
- `.env.example` exposes only `SERVERFS_MAX_BINARY_TRANSFER_BYTES` for size control.

### Bridge protocol/service tests

Prove:

- strict `runtime.models` parameter contract;
- optional `model` accepted by `task.submit`;
- unknown fields still rejected;
- requested model reaches `TaskContext`;
- model participates in request fingerprint/idempotency;
- manifest schema v2 records requested model;
- continuation omitted/explicit behavior is distinct.

### Store migration tests

Start from the previous schema and prove:

- `requested_model` is added safely;
- old rows read as `None`;
- new rows round-trip exact model IDs;
- restart/recovery behavior remains unchanged.

### Codex adapter tests

Prove:

- `model/list` pagination and normalization;
- bounded result handling;
- `thread/start` receives explicit model and omits it otherwise;
- `thread/resume` receives explicit model and omits it otherwise;
- discovery never starts a turn.

### Claude adapter tests

Prove:

- explicit model reaches `ClaudeAgentOptions.model`;
- omission preserves current `None` behavior;
- discovery reports `unsupported` without starting inference or mutating settings.

### Qoder adapter tests

Prove:

- structured `get_available_models()` normalization;
- discovery lifecycle always disconnects/cleans up;
- no query/prompt is issued during discovery;
- explicit model reaches `QoderAgentOptions.model`;
- omission preserves native default;
- continuation behavior remains correct;
- live-smoke no longer depends on constructor-only `model_override`.

## 14. Live validation

Provider-specific release validation was completed without modifying native Agent configuration. The 2026-09-30 acceptance evidence is frozen in `docs/v0.9.0-release-validation-2026-09-30.md` and `docs/model-selection-validation-2026-09-30.md`.

### Codex

1. call `list_agent_models(runtime="codex")`;
2. verify normalized catalog against raw App Server `model/list`;
3. submit one disposable task with an explicitly selected known-safe model;
4. verify task manifest/requested model;
5. submit without `model` and prove prior default behavior is unchanged.

### Qoder

1. snapshot relevant user/project/local Qoder configuration file hashes/mtimes;
2. call structured model discovery;
3. verify returned IDs against native `qodercli --list-models` where possible;
4. explicitly select provider ID `qfmodel` (display name `Qwen3.8-Flash`) for the disposable smoke unless the current account catalog proves that identifier is no longer available/free;
5. verify a task and continuation; the 2026-09-30 live smoke passed explicit `qfmodel` new-session + continuation, while the omission task correctly recorded null requested-model evidence but the provider-native default route failed with an account-permission error; ServerFS did not fall back;
6. verify Qoder configuration hashes are unchanged after discovery/task execution; the release-candidate comparison passed;
7. clean disposable task/session artifacts created by validation where safe and owned by the test.

### Claude

1. prove `list_agent_models(runtime="claude")` returns the explicit unsupported contract;
2. use a maintainer-approved known model ID/alias for one disposable explicit-model task;
3. verify Claude user/project/local configuration is unchanged; the release-candidate comparison passed;
4. submit without `model` and prove native default behavior is unchanged. The 2026-09-30 smoke passed explicit `sonnet`, continuation, and omission/default behavior.

No release test should pick an arbitrary paid model merely because it appears in a catalog.

## 15. Documentation/release alignment

The released v0.9.0 line updates:

- `README.md`
- `AGENTS.md`
- `agent_bridge/README.md`
- MCP tool documentation/examples
- website/docs in both English and Simplified Chinese
- version metadata to 0.9.0
- release validation evidence

`.env.example` should receive **no model variable**. It may only receive wording clarifying that model selection is request-scoped, if such wording is useful.

## 16. Implementation order

1. **Contract first** — models/types, `TaskContext.requested_model`, normalized discovery/advice result, protocol shapes.
2. **Persistence** — schema migration, fingerprint/idempotency, manifest v2, task inspection.
3. **Provider selection** — Codex/Claude/Qoder explicit model mapping with omission regression tests.
4. **Provider discovery** — Codex and Qoder; Claude explicit unsupported implementation.
5. **Jev Model Advisor** — optional pre-submit advice on the discovered candidate set; prove advisory/fail-open behavior.
6. **MCP surface** — `list_agent_models`, optional advice context, `submit_agent_task.model`, audit/error mapping.
7. **Unified binary limit** — make `SERVERFS_MAX_BINARY_TRANSFER_BYTES` authoritative for file ingress, retain the old sidecar name only as a compatibility fallback, and update Compose/tests.
8. **Deterministic gates** — Bridge suite + root suite + lint/format + Compose rendering checks.
9. **Live provider validation** — read-only/config-preserving discovery/advice and explicit-model smokes.
10. **Docs/site/version alignment** — only after behavior is frozen.
11. **Release publication** — final clean-tree/full gates completed; the release commit was published to `main`, then the immutable `v0.9.0` tag and GitHub Release were published from the final documentation-aligned commit.

## 17. Acceptance criteria

v0.9.0 is acceptable only when all of the following are true:

- `list_agent_models` exists as the only new public MCP tool and supports both discovery-only and optional pre-submit advice mode.
- when Jev is enabled and a runtime exposes models, ChatGPT can request a task-specific Jev recommendation before submission; the recommendation is always advisory and never auto-populates/overrides `model`.
- when Jev is disabled, model discovery still works and no Jev request is made.
- Codex returns normalized App Server catalog data without inference.
- Qoder returns structured current-account model metadata without modifying user/project/local configuration.
- Claude truthfully reports discovery unsupported rather than returning a fabricated/static list.
- all three production runtimes accept the optional per-task `model` path.
- omitting `model` preserves v0.8.0 native-default behavior exactly.
- explicit model selection never persists as a ServerFS/provider **default or configuration**; only the request-scoped `requested_model` evidence is stored with the ServerFS task/manifest.
- explicit invalid/unavailable models fail rather than silently falling back.
- requested model is durable in task state, idempotency identity, and manifest evidence.
- no provider credential or raw secret-bearing model payload crosses into the MCP container/tool response or Jev advisory request.
- `SERVERFS_MAX_BINARY_TRANSFER_BYTES` alone changes the public global ceiling for both MCP binary transfer and file-ingress fetches; the old ingress-specific variable is no longer required in `.env`.
- legacy ingress-specific size configuration remains a compatibility fallback only and never overrides the new unified setting.
- per-workdir binary limits and the ingress sidecar's workdir isolation remain intact.
- Bridge protocol remains backwards compatible for existing v1 methods.
- root and Agent Bridge deterministic gates pass.
- provider live validation records concrete evidence and configuration-integrity checks.
