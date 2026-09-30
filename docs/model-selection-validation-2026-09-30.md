# v0.9.0 Model Selection & Advisor Validation — 2026-09-30

Status: historical pre-release acceptance evidence for the **published v0.9.0 stable release**. This validation record itself predates tag publication; the final documentation-aligned commit was subsequently tagged and released as `v0.9.0`.

## Scope

This record validates the v0.9.0 additions around:

- provider-neutral Agent model discovery;
- optional request-scoped `submit_agent_task.model`;
- advisory-only Jev Model Advisor;
- durable `requested_model` / manifest schema v2 evidence;
- native-default preservation when no model override is supplied;
- provider configuration integrity during live validation.

The existing runtime authorization, provider approval, workdir lease, recovery and provider-native configuration boundaries remain unchanged.

## Deterministic evidence

The v0.9 implementation passed the focused and cross-boundary gates before final release closure:

- Agent Bridge focused regression: **97 passed**;
- root focused MCP/file-ingress regression: **56 passed**;
- real UDS + MCP E2E: **56 / 56 checks passed**;
- Agent Bridge full suite: **147 passed**;
- root full suite: **824 passed**;
- site: `npm ci` + static build **19 pages**;
- Compose rendering, Docker scratch build, deployment shell syntax and `git diff --check`: passed.

The final post-format repository gate subsequently passed in full: Ruff check/format, **824 root tests**, Compose rendering, development Docker image build, deployment shell syntax, and `git diff --check` all passed. The release commit therefore uses post-rerun evidence.

## Native model discovery

Discovery was executed against the installed providers without starting an inference turn.

### Codex

- status: `ok`
- normalized models: **10**
- visible default: `gpt-6.1-sol`
- source: Codex App Server `model/list`

The returned catalog included visible and hidden entries; hidden entries remain metadata only and are not selected by the live-smoke default-model rule.

### Qoder

- status: `ok`
- normalized models: **17**
- enabled/free models:
  - `qmodel_38max` — Qwen3.8-Max
  - `qfmodel` — Qwen3.8-Flash
- source: structured current-account Qoder Agent SDK model catalog

### Claude

- status: `unsupported`
- scope: `none`
- models: `[]`

This is intentional. The installed Claude Code / Agent SDK does not expose an equivalent stable native-account enumeration API, so ServerFS does not fabricate a static catalog.

## Jev Model Advisor

With the configured Jev client, pre-submit advice was requested for Qoder using a concrete no-tool task.

Result:

- advisor model: `jev-1.13.0`
- status: `completed`
- `automatic=false`
- recommended model: `qfmodel`
- confidence: **0.88**
- probabilities:
  - `qfmodel`: **0.94**
  - `qmodel_38max`: **0.06**

The recommendation belonged to the currently eligible runtime catalog. ServerFS did not copy it into a task submission automatically.

## Provider live validation

All live validation used disposable task state and provider-native authenticated environments. No provider settings were rewritten.

### Codex — PASS

The live smoke selected only the visible catalog model marked `is_default=true`: `gpt-6.1-sol`.

Verified:

- explicit request-scoped model task: PASS;
- explicit-model continuation: PASS;
- fresh submission with no model override: PASS;
- manifest schema v2 / requested-model evidence: PASS;
- workspace-write smoke: PASS;
- `~/.codex/config.toml` hash/mtime unchanged;
- repository status unchanged by validation.

### Claude — PASS

Installed `claude --help` explicitly documents `sonnet` as a valid `--model` alias example.

Verified:

- explicit `model=sonnet` task: PASS;
- explicit-model continuation: PASS;
- fresh submission with no model override: PASS;
- manifest schema v2 / requested-model evidence: PASS;
- `AskUserQuestion` round-trip: PASS;
- workspace-write smoke: PASS;
- existing Claude user/local settings hash/mtime metadata unchanged;
- repository status unchanged by validation.

### Qoder — explicit model / continuation PASS; native-default environment limitation recorded

The live validation first re-discovered **17** models and required `qfmodel` to be both enabled and explicitly free before using it.

Verified:

- explicit `model=qfmodel` task: PASS;
- continuation with the same requested model: PASS;
- persisted native session continuation: PASS;
- Qoder user settings hash/mtime unchanged;
- repository status unchanged by validation.

A separate submission with no ServerFS model override returned a provider/account access error for the account's native default model service. ServerFS did **not** silently fall back to `qfmodel` or another catalog entry. Deterministic adapter/service tests verify that omission forwards no ServerFS model override and persists `requested_model=null` / `manifest.model.requested=null`.

This is recorded as an environment-specific native-default limitation, not an explicit-model failure. The standard Qoder live smoke remains pinned to the discovered free `qfmodel`; native-default checking is an explicit optional validation path rather than a prerequisite for that pinned smoke.

## Configuration integrity

Live validation compared the relevant provider settings metadata before and after execution.

- Codex configuration: unchanged.
- Claude user/local settings: unchanged.
- Qoder user settings: unchanged.
- No ServerFS provider model default was written.
- No per-workdir model default was introduced.
- No provider credential or raw secret-bearing model payload was exposed through the MCP surface or Jev result.

## Acceptance conclusion

The v0.9.0 model-control design remains request-scoped:

- discovery is read-only;
- Jev advice is advisory-only;
- explicit model selection is per submission;
- omission keeps provider-native default behavior;
- unavailable/invalid provider choices fail rather than silently falling back;
- ServerFS does not become the owner of provider model defaults.

The final repository-wide deterministic gates passed, and this evidence was included in the v0.9.0 release line. The subsequent documentation-aligned commit was tagged and published as the stable `v0.9.0` release.
