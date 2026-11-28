# v0.27.0 common acceptance and API deltas

Status: **target contract draft for implementation review**. Based on the approved [ADR](../../docs/architecture/v0.27.0-platform-independence.md) and [Plan](../../docs/plans/dev_plan_v0.27.0.md). Not implemented and not a deployed capability claim. During Phase 1, preserve the [v0.26.0 baseline](../v0.26.0/README.md) unchanged. Review and freeze the additive wire fields below before Gate A is closed and platform feature streams begin; no platform may independently choose incompatible public semantics.

## Gate A — behavior-preserving platform split

Every row applies to Linux, macOS and Windows within its existing supported topology. Record exact integrated SHA, fixture and artifact identity, commands, result counts, skips/reasons and scope. Use native host tests for native behavior and isolated platform-only build inputs; shared tests may only consume public contracts, not implementation imports.

| Case | Positive evidence | Negative / isolation evidence |
| --- | --- | --- |
| A-01 independent source/build | Build Core/Bridge/native artifacts from platform-only checkout, local manifests/locks and local tests; inspect wheel/image contents | No runtime/build input from another platform or root shared runtime; remove old root only after replacement validated |
| A-02 public MCP contract | Compare applicable tool registration/input/output schemas and annotations to frozen 12-variant snapshot; list/read resource template, stdio and both HTTP wire revisions | Missing optional feature stays absent or fails closed; no authority based on metadata; unsupported protocol/Host/Origin rejected |
| A-03 file and binary | Workdir enumeration, paginated text, search/find, stat, atomic create/edit/delete, directory operations, bounded binary transfer and ingress where supported | Read-only, deny/reserved/hidden rules, symlink/reparse escape, stale revision, collision, non-UTF8 and size limits; ingress isolation |
| A-04 authentication | Configured API key/OAuth path, loopback/direct/Tunnel as applicable | Wrong/missing credentials, issuer/resource mismatch, revoked grant, Host/Origin protection, no credential logging |
| A-05 Agent/Repo Ops | Mode-specific tool combinations, task discovery/result/event/approval/message/cancel, existing real provider and native resume only where already supported; fixed Repo Ops actions | Foreign Core/workdir, unowned legacy rows, v2 refusal of continuation/idempotency, replay, stale grants, outside-root writes, writer lease/recovery guard, no generic executor |
| A-06 topology | File-only; owned and shared Bridge where supported; multiple instances/aliases and independent Connector/Core/Bridge restart | Owner survives shared Core changes, other instances intact, revoked/foreign tasks inaccessible, no production services touched |
| A-07 upgrade/rollback | Isolated existing version fixture: identity/config/grants/retained results survive documented migration, old artifacts recover via documented rollback | Fail closed on incompatible state or mixed versions; no automatic authority enlargement/rotation, no unsafe state downgrade |
| A-08 distribution and CI | Independent platform commands and path-scoped checks; required check behavior; version/Registry/Plugin/site metadata validated | No false passes due to skipped jobs, stale root manifests or mismatched artifacts; release-wide same-SHA gates remain |

Do not demand NFS-specific tests or fix NFS behavior. Preserve existing supported local filesystem semantics. Existing scoped live evidence is a baseline, not replacement for affected migration checks. Document unavailable combinations and never infer support from a different platform.

## P0-02 — scoped continuation and retry

Existing `submit_agent_task` already exposes optional `continue_from_task_id` and `idempotency_key`; no new submission/resume tool. Preserve old v1 behavior and null/default semantics. Update descriptions and runtime/attachment capability reporting to distinguish provider-native persistence from signed-v2 support. A client must not infer support from provider persistence alone.

- Verify signed authority first and reuse the existing indistinguishable `CORE_TASK_NOT_FOUND` public denial for missing/foreign/unowned predecessor where appropriate. Current v0.26.0 store checks Core + alias; new continuation must additionally bind the trusted root and compatible execution identity, current grant and previous context. Never infer legacy provenance.
- Accepted continuation returns a new task ID, preserves predecessor lineage and reuses a proven resumable native session. Do not expose raw native session IDs. Terminal state alone is insufficient; require provider quiescence and durable coordination.
- Explicit same-key retry under current authorized Core/workdir scope with same fingerprint returns the original retained task. Different fingerprint is rejected. A fresh nonce is independent of idempotency; key/nonce cannot widen authority. Define stable conflict/expired-retention semantics before freeze.
- Additive capability indicator should describe signed-v2 attachment support, separately from existing runtime provider fields. Exact placement/names to be finalized with platform adapters; legacy absence means unsupported, not enabled.

## P0-03 — snapshot-bound partial results

Baseline `get_agent_task.result` has storage, size_bytes, sha256 and retrievable; final_response remains the normal inline value or final spool preview. Baseline read_result supports only spooled final results, with offset_bytes/max_bytes. Preserve old complete-result reads and error semantics.

Proposed additive public fields: `result.completeness` (none/partial/complete), `result.snapshot_id` (opaque, nullable), and optional checkpoint sequence/time metadata. New partial results should be reachable through `read_agent_task_result`; add optional `snapshot_id` request parameter and return actual snapshot ID and completeness alongside existing chunk fields. New clients bind every subsequent page to the returned ID; do not silently substitute another snapshot when a requested ID is stale or evicted. An unbound nonzero offset on a changing partial result must fail with an explicit retry instruction; retain legacy complete spool pagination. Keep `final_response` semantics and terminal status independent of partial availability. Return no hidden provider reasoning or private paths.

Define snapshot retention/eviction, stale snapshot error, durability ordering, provider delta vs replacement normalization and bounded result sizes before freeze. Never require a new database or broker.

## P0-04 — bounded read-only consent

The existing `respond_agent_approval.decision` includes approve_once, approve_session, deny, cancel_task, plus ACP option_id and granted_permission_ids. In v0.26.0 approve_session can apply provider session suggestions; do **not** silently reinterpret it as a verified read-only grant.

Proposed new explicit choice `approve_task_readonly` offered only on a pending request with a verified bounded read-only scope. The existing approval decision channel is reused; preserve provider-specific choice validation. Public pending request includes a redacted scope summary and offered choice, not host paths, permission secrets or raw policy data. Bridge validates original request identity and exact allowed scope for each use; audit all uses. Prefer task-lifetime grants, with termination and explicit revocation; fail closed on uncertain provider enforcement. Exact revocation surface and fields to be finalized with no unnecessary new tools. Continue ordinary native approval for unknown effects; no shell name allowlist or provider-wide grants.

## Common negative vectors for the new features

- Continuation: foreign Core/alias, changed root, revoked grant, legacy/unowned row, incompatible runtime/account/profile, active or unproven-stopped prior task, concurrent resume, replay, retry after restart, idempotency collision.
- Partial result: no checkpoint, live read, timeout/cancel/failure with committed result, restart, partial/final transition, snapshot paging during concurrent publication, Unicode byte boundaries, quota/GC, crash at durability boundaries, foreign/expired snapshot, private content.
- Approval: representative 20 verified read operations per task, at most one explicit approval; scope expansion, unknown shell side effects, denied/sensitive path, network/write, provider switch, policy revoke, task restart/continuation, private identity audit. Real provider evidence is mandatory for supported workflow claims.

Keep tests local to each implementation and conformance inputs at this boundary. Final field/error naming remains a review item; this draft is not authorization to introduce unrelated APIs or implement new features before Gate A.
