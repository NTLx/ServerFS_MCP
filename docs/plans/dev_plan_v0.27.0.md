# ServerFS MCP v0.27.0 — Platform Independence & Agent Reliability

> Status: **APPROVED PLANNING BASELINE — IMPLEMENTATION NOT STARTED / NOT RELEASE-AUTHORIZED**. 2026-11-28. The maintainer approved platform independence, the remaining mandatory reliability improvements, and removal of NFS work from this release. This is an implementation/acceptance plan, not release evidence.
>
> Observed VPS repository baseline: clean `main`, `f1c94cea12e98771af37b8f637ec278e4369bfb2`, ServerFS v0.26.0, root MCP Python SDK `mcp==2.3.0` in manifest and lock. Recheck remote/main and each host's actual checkout before work; no live runtime version or new test execution is implied.
>
> Binding architecture: [Platform Independence ADR](../architecture/v0.27.0-platform-independence.md). Read it with [AGENTS.md](../../AGENTS.md), [invariants](../maintainers/invariants.md), [workflow](../maintainers/workflow.md), v0.26.0 [release notes](../releases/v0.26.0.md) and [evidence](../releases/v0.26.0-evidence.md). Older plans are historical; the approved target deltas below override only the explicitly identified baseline restrictions for v0.27.0 development.

## 1. Scope and release gates

| ID | Outcome | Release requirement |
| --- | --- | --- |
| P0-01 | Independent Linux/macOS/Windows product implementations | Mandatory; first implementation gate |
| P0-02 | Signed-v2 scoped native session continuation and retry idempotency | Mandatory |
| P0-03 | Durable partial task results, readable after timeout/interruption | Mandatory |
| P0-04 | Bounded reusable approval for verified read-only inventory | Mandatory |
| P1-01 | MCP Apps: task monitor, approval/question, continuation, workdir browser, file preview, Git diff, runtime/model selection | Planned delivery, Host-tested; prioritize task/approval/continuation |
| P1-02 | Elicitation / MRTR | Planned delivery where verified; fallback required |
| P1-03 | Protocol/auth/cache/privacy review | Required security review |
| P1-04 | Optional OpenTelemetry integration | Planned, reuse existing observability; integrate only with demonstrated value |
| P2-01 | MCP Events | Compatibility experiment; opt-in production only after security/Host acceptance |
| P2-02 | MCP Tasks | SDK/Host compatibility and thin adapter if supported |
| P2-03 | Subscriptions | Document need/support; no general filesystem watching |

All P0 outcomes block publication if incomplete on an applicable platform. P1 is planned delivery, not permission to silently drop a feature. For an upstream limitation, record evidence, exact supported scope, fallback and maintainer-approved disposition; do not advertise unverified support. P2 requires a documented compatibility decision, not production implementation at any cost. P1/P2 do not override P0 security. No NFS work or NFS-specific release gate. Do not weaken existing filesystem behavior.

## 2. Phase 0 — freeze v0.26.0 contracts and split inventory

Capture exact source baseline, actual platform and mode matrix, package/lock/build inputs, tool names and input/output schemas, error and auth contracts, resources, native lifecycle and deployment acceptance, state locations and data migration constraints. Record provenance and gaps; do not infer parity. Current source has root Core, shared Bridge, platform seams, per-platform deployment and shared tests. Inventory imports, packaging force-includes, Docker build contexts, Compose paths, installer asset paths, root-based scripts, workflow filters, version validators, site/skills and Registry distribution links before moving them.

Define `contracts/` as public schemas/specs/test vectors and external contract testing only. Agree P0 API/semantic changes before independent feature work. The historical v0.26.0 signed-v2 refusal remains required on old deployments; enabling continuation on new code requires the complete authorization contract below. Preserve version-matched documentation, credentials and private state.

## 3. Phase 1 — P0-01: platform isolation before feature development

1. Copy/move the necessary v0.26.0 implementation and tests into linux/, macos/, windows/ as self-contained subprojects; preserve known working behavior first. No new Agent feature or broad refactor during this migration.
2. Isolate manifests, lockfiles, Core and Bridge, adapters, native resources, packaging, installers, Compose, configs, tests and service operations. Remove irrelevant platform code/dependencies and obsolete root runtime/build paths only after proving replacements. No common runtime source, shared private-wire requirement or inter-platform import. Keep local reuse and standard upstream libraries.
3. Make each build/test runnable from its platform-only checkout; contract tests may run externally with common fixtures. Verify artifact contents do not package other implementations. Retain existing artifact/entry-point compatibility or document/test required migration.
4. Update CI path ownership, release version checks and all moved source/runbook/skill/site links; keep public release metadata coherent. Do not leave workflow instructions targeting deleted root commands. Shared release tools must not become runtime dependencies.
5. Validate v0.26.0 behavior/security equivalence, independent lifecycle, supported native OS/filesystem and ingress combinations, file-only and owned/shared modes, multi-workdir/Repo Ops/binary/auth scope and provider behavior. Check existing state, identities, grants, retained tasks and config migration/rollback with isolated fixtures. No production cutover as part of the split.

**Gate A:** three independently built/tested product implementations; no cross-platform runtime dependency; actual parity and migration evidence per platform; common contract and CI operational. Complete and integrate the split baseline before parallel feature implementation. Do not claim Gate A from source movement alone. Commit/push/merge require separate explicit authorization.

## 4. Phase 2 — P0-02: signed-v2 continuation and idempotency

### Baseline and design

v0.26.0 supports scoped task discovery, not continuation/idempotency in signed-v2. Pre-split `agent_bridge/src/serverfs_agent_bridge/core_grants.py` and `service.py` explicitly refuse these fields; platform adapters/bindings also restrict them. Legacy v1 already checks runtime/workdir/profile/native session and stores continuation lineage. The Bridge store has task ownership records. Trace each platform's actual complete call chain after the split; do not just remove the refusals.

Reuse `submit_agent_task(continue_from_task_id=..., idempotency_key=...)` and scoped discovery. No duplicate resume tool or raw native session ID supplied by caller. Continuation creates a new task linked to the retained predecessor and provider session; it is not in-flight process reattachment.

- Authenticate the signed request and validate active grant, nonce, Bridge/Core/key identity, method and workdir policy before any task lookup that could leak information. Derive ownership from verified authorization, not caller-provided identity.
- Require predecessor ownership by the same Core, same exact authorized workdir and stable root identity (not alias alone), compatible provider/runtime/account/configuration identity and session, currently valid permissions, no profile/permission escalation. Revalidate current grant and policy at admission; fail closed for revoked/rebound roots or unverifiable historical identity. Do not infer ownership for old v1/unowned records or backfill from aliases. Preserve opaque IDs/privacy and indistinguishable unauthorized lookup errors.
- Only a terminal predecessor with verified provider quiescence and a resumable native session is eligible. Retain writer leases/recovery guards; terminal state alone does not prove stop. Prevent simultaneous resumes of one native session (including races/restarts) and ensure continuation never widens the previous authorized context. Provider-specific restrictions and model semantics remain explicit.
- Persist lineage and ownership atomically. Scope idempotency to Core + workdir/identity + key under current authorization and canonical request fingerprint, including predecessor and all execution-relevant fields. Same key/same request returns original retained task without a duplicate turn; mismatch fails. Authorization is rechecked on retry; define retention and ambiguous-response behavior. Durable atomic claim/lookup prevents concurrent duplicates and survives restart. Nonce replay protection is separate; legitimate retry uses a fresh signed request. No blind retry until support is established.
- Retain v1 behavior; support old Bridge/Core combinations only as explicitly tested, with clear unsupported-capability errors, never v1 fallback. Define safe migrations of task/ownership/lineage/index state; no automatic grant expansion or authority inferred from old records.

### Required acceptance

Linux/macOS/Windows applicable signed-v2 owned/shared/named modes: same Core/workdir successful multi-turn native session continuity with supported real providers, fresh MCP client and Core restart, discover retained task and resume. Test peer Core, cross-workdir, root rebind, revoked/stale grant, profile escalation, legacy/unowned, missing/expired session, active or unproven-stopped task, unsupported runtime, concurrent resume and restart. Idempotency tests include identical duplicate, different fingerprint, cross-scope collision, concurrent duplicate, network response loss, crash/restart and expired retention. Verify no unauthorized disclosure and no extra provider turns. Record unsupported provider combinations based on actual capability, not fake success. Explicitly verify no regression to task discovery, v1 or Repo Ops.

## 5. Phase 2 — P0-03: partial results and checkpoint persistence

Current Bridge persists task state/events and final inline/spooled result, but normal completion is the main result persistence path. Timeout transitions to INTERRUPTED / AGENT_TASK_TIMED_OUT. Existing normalized provider-visible message events and spool are useful inputs, not proof of recoverable complete partial results.

Extend platform-local existing store/event/spool mechanisms rather than add a database or broker. Separate execution status from result completeness (none/partial/complete, exact field names selected in common contract). Capture bounded user-visible output progressively and flush durable checkpoints during execution, not merely at timeout. Checkpoints carry monotonic sequence/snapshot identity, timestamp, size/digest and source/coverage. Use normalized provider events with explicit delta vs snapshot semantics; avoid duplicate chunks and avoid claiming a semantic milestone not actually supplied. A checkpoint may optionally reference policy-verified workdir-relative artifacts; reference is not a guarantee the file stays unchanged. No hidden reasoning, raw credentials, unredacted private paths or unlimited provider logs.

Extend `get_agent_task`, `read_agent_task_result`, and existing event API with compatible fields and snapshot-bound pagination. Keep existing complete-result behavior. Expose latest committed partial output while running, and after timeout, failure, cancel or interruption. Identify partial/complete unambiguously; no false success. Define atomic metadata/spool publication, crash recovery, integrity validation, bounded storage/retention/GC and handling of orphan files; keep prior committed checkpoint if a later write fails. Never discard already committed output merely because the task is terminal. Result read and retention follow task ownership/current authorization. Do not loosen provider cancellation, writer lease or recovery guards.

**Acceptance:** live partial read before completion; real deadline timeout after emitted output; failed/cancelled/interrupted tasks preserve durable output; Core and Bridge restart/recovery; injected crash between spool/metadata commits, concurrent reads with new checkpoint, Unicode and bounded pagination, retention/quota/GC, cross-Core and cross-workdir denial, privacy/secret redaction, successful final result replacement without duplication. Test actual supported providers on all three platforms. Guarantee only committed checkpoints, not unflushed output or complete task recovery.

## 6. Phase 2 — P0-04: bounded read-only inventory approval

v0.26.0 has structured write-target screening and private approval identity/advice-cache isolation; it cannot establish arbitrary command side effects from names or text. Jev is advisory only. Preserve these boundaries.

Introduce explicit task-scoped read-only approval reuse for operations whose actual read scope and absence of side effects can be established through structured operations and provider-enforced constraints. Prefer deterministic read tools where appropriate; use native provider permission semantics where they can be bounded. User chooses approve once, approve this task's same-scope verified reads, or deny, where supported; default remains single approval. Bind reusable permission to verified Core/task/workdir/root/policy/runtime/profile, operation class, exact allowed roots and bounded lifetime; revoke on scope change, task terminal/restart if not safely recoverable, or user revocation. No automatic inheritance by a continuation task. Revalidate each use and record original private approval identity, scope, explicit consent and per-use audit with safe public redaction.

No generic shell allowlist based on names, prompt intent or string matching. Arbitrary shell/PowerShell, external MCP tools, network, writes, sensitive/denied paths, outside-root access and unknown effects do not qualify. Prevent symlink/reparse and scope escapes; don't convert a Bridge lease into a sandbox or silently rewrite provider settings. If native runtime cannot enforce the narrow scope, retain single approvals for its unverifiable actions; provide a safe structured inventory route and document measured limitations. No blanket or permanent approval, no global bypass and no automatic advisory approval.

**Acceptance:** on each platform, a representative 20-operation same-scope verified read-only inventory requires at most one explicit user approval (or zero redundant prompts when the workdir already authorizes deterministic reads). Demonstrate actual reduction in applicable Agent workflows with real provider evidence, not just a fake adapter or counting unrelated tool calls. Test mutation, shell metacharacter/unknown effects, outside-root/sensitive targets, changed scope, different task/Core/workdir/provider, continuation, revoked/expired policy, race and redaction/audit negatives. A provider incapable of safe reuse must fail closed; unsafe auto-approval is never a fix. Record precise runtime limitations and require maintainer review if a target workflow cannot meet the goal.

## 7. Phase 3 — MCP native interaction (P1)

Use official SDK/extension APIs, negotiate capabilities and verify current upstream requirements at implementation time. Keep 2025-11-25 and 2026-07-28 support on the existing stdio/Streamable HTTP surfaces; no second endpoint simply for a revision. Baseline SDK 2.3.0 is a pin, not a mandate to upgrade or fork.

**Apps:** platform-local optional UI resources with lightweight static HTML/CSS/JS and Host bridge; no separate backend or central multi-host UI. Task monitor (scoped task list, status, events, partial/final result), approval/question with bounded grants, session continuation, workdir browser/file preview, Git status/diff/staged diff and authorized runtime/model selection. Start with task/approval/continuation. Reuse ordinary authorized tools, enforce Host UI isolation/CSP and safe rendering of untrusted output; no arbitrary shell, online editor or new permissions. Test real data and actions, desktop Host and mobile where available, read-only/denied actions, stale task state and unsupported-client fallback. Never present simulated data as live.

**Elicitation/MRTR:** map supported pending questions and approvals to standard structured input and multi-round request continuation with old-client fallback. Reuse one backend and original pending request identity, verify authenticated task/scope/expiry and explicitly authorized decision; Elicitation is not authorization. Do not duplicate submission, approval or provider actions on retry. No secrets via inappropriate form inputs. Verify actual Host capability, decline/cancel, replay, expiry, stale requests, and unsupported clients continuing to use existing tools.

**Security:** review actual current OAuth issuer/resource binding/scopes, key/auth/Host/Origin behavior, private cache scope and headers, version routing, structured schema/annotations and UI/interaction authorization. No broad shared cache for private data, no weakening existing controls. Preserve conformance tests and relevant official pinned requirement sets, record applicable scenarios and raw sanitized wire evidence.

**Observability:** reuse platform-local existing diagnostic/metrics facilities; optional OTel instrumentation for MCP/RPC latency, execution time, checkpoint persistence, conflicts and approval reuse when useful. Bounded labels, no secrets/private paths/prompts/task contents or high-cardinality identifiers in ordinary telemetry. No mandatory collector, Prometheus or Grafana. Verify disabled/default behavior and opt-in instrumentation.

## 8. Phase 4 — conditional protocol integrations (P2)

**Events:** verify official current draft and actual target ChatGPT Work/Dots or other Host capabilities. Candidate business events: task completed/failed/interrupted/cancelled, checkpoint available, approval required, question required. These are proposed ServerFS event types, not official names. Use existing durable task events as source; avoid a second task system. Explicit opt-in, authorized per-Core/workdir subscriptions, validated permitted callback destinations, TLS, SSRF/DNS/redirect controls, bounded payloads/retries, signature/replay protection, deduplication, retention/revocation and restart recovery. Only opaque authorized references/minimal status, no result text or approval details; subsequent tool reads require authorization. No arbitrary network egress. Host event delivery does not imply automatic ordinary chat resumption or exactly-once delivery. Test actual subscription/event/Host behavior and negative cases; otherwise document why disabled.

**Tasks:** recheck official Tasks extension revision, SDK and Host implementation; map current Agent tasks/results/approval/question/cancel to standard interfaces only where semantic and authorization equivalence is demonstrated. Retain native event/result/steer/discovery tools as needed. No SDK fork, fake standard support, or Bridge rewrite. If blocked, document upstream version/compatibility evidence and adapter design.

**Subscriptions:** assess demand and capability; keep disabled absent a justified implementation. No generalized file monitoring or NFS polling in this release.

## 9. Parallel development and integration discipline

Phase 0 -> Phase 1 -> Gate A -> three isolated platform feature streams (P0-02/03/04, then P1, P2) -> Gate B -> release. After Gate A, suggested branches feat/v0.27.0-linux, feat/v0.27.0-macos, feat/v0.27.0-windows, each confined to its subtree. Within each platform coordinate shared local store/service changes before parallel edits. ChatGPT leads design, direct inspection, review, contract coordination and acceptance; host Agents execute necessary host commands or scoped implementation tasks. Never give a verification-only task write authority or use Agent fallback for deterministic Repo Ops. One platform's failure should not prevent unrelated platform development/CI; common contract and final release remain coordinated. Review all three for analogous security bugs.

Record per-phase task scope, changed paths, tests, evidence SHA, actual supported mode/provider matrix, residual issues, migration/rollback and next decision. Test paths and build commands must match the split tree. Keep AGENTS/indexes concise and authoritative; do not rewrite historical records or imply planned features are released. Update current maintainer rules, runbooks, public schemas, skills, website and distribution validators at implementation milestones. Keep documentation English unless localized; maintain existing bilingual product documentation.

## 10. Gate B and publication checklist

All applicable gates must have observable evidence, not just a green unit test:
- [ ] Gate A satisfied and independent source/build/test/CI boundaries proven.
- [ ] P0-02 scoped signed-v2 continuation/idempotency, live provider and negative security tests on all applicable platforms.
- [ ] P0-03 durable partial results and timeout/failure/cancel/restart evidence, isolation and storage/privacy limits.
- [ ] P0-04 measurable scoped approval reduction, real-provider evidence and fail-closed negative tests.
- [ ] P1 Apps and Elicitation/MRTR actual Host interoperability and fallback, security review and OTel disposition; any unavailable scope explicitly reviewed.
- [ ] P2 Events/Tasks/Subscriptions compatibility decisions and opt-in evidence for any enabled extension.
- [ ] Independent Linux Docker/host Bridge, native macOS and Windows build, native registration/lifecycle, isolated upgrade and rollback; no changes to other instances; preserve existing topology/provider support.
- [ ] Authenticated public ingress and direct HTTP as applicable, both MCP revisions and stdio; official applicable conformance and wire regressions, filesystem/Repo Ops/binary/auth tests.
- [ ] Existing identities, grants, private state and retained task migration/recovery; no privilege widening or unapproved credential rotation.
- [ ] Three platform artifacts and full relevant CI at same integrated SHA; versioned packaging, Registry/skills/site and release documentation checks per current workflow.
- [ ] Update release checklist/evidence ledger, disclose unsupported/unverified combinations; no unresolved P0 failure. Explicit authorization for commit/push/tag/publication/deployment as applicable.

NFS-specific support and Revision consistency remediation are explicitly deferred; this checklist includes only existing supported filesystem behavior. No NFS test is a release prerequisite. No central management service, common runtime library, language rewrite, arbitrary executor, forced provider parity, new storage service or mandatory monitoring stack. All implementation and live acceptance gates are currently pending.
