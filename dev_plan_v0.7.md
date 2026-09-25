# ServerFS MCP v0.7.0 Development Plan

Status: frozen implementation plan for v0.7.0  
Theme: **Runtime Reliability & Observability**  
Baseline: v0.6.0 on `main` at `d556c3265bd225470e21d83c738d1c3ef28bda42`

> **Release-state note:** this file is the frozen historical plan for v0.7.0. v0.7.0 and v0.7.1 have already been published; the current maintenance line is v0.7.2. v0.7.2 keeps the v0.7.0 public/runtime contract frozen while adding only the bounded-FD filesystem fix, proven-inactive Agent task state reconciliation, and bounded read-only runtime-readiness verification described by `dev_plan_v0.7.2.md`, the current README, tests and implementation. Historical pre-release stop instructions below are retained as audit evidence, not as the current release state.

## 1. Product boundary

ServerFS remains:

- a controlled Workspace Access Plane;
- an Agent Runtime Gateway / Harness bridge;
- a minimal durable control plane for delegated execution.

A ServerFS Agent task is an execution/delegation run, not a business workflow.

The following remain out of scope and must not be added to ServerFS itself:

- Memory/RAG/Skills;
- provider sandboxing or a new isolation layer;
- scheduler/cron;
- workflow or multi-agent orchestration;
- a generic model gateway;
- a general evaluation platform.

v0.7.0 adds reliability and evidence around the existing provider-neutral Agent Bridge. Existing authorization, provider approval semantics, writer leases and the v0.6 Jev advisory-only contract remain authoritative.

## 2. Frozen v0.7.0 scope

### 2.1 Event envelope v1 and structured audit correlation

Normalized task events use envelope schema version 1:

```text
schema_version
event_id
task_id
correlation_id
event_type
created_at
payload
```

`schema_version=1` is stable for additive event types.

`correlation_id` is optional and opaque:

- UTF-8 string;
- at most 256 encoded bytes;
- no ASCII control characters;
- stored on the task;
- returned by task reads;
- copied into event envelopes, immutable task manifest and metadata-only audit logs;
- never parsed, routed, authorized, made unique, used for idempotency or used as a continuation key;
- never forwarded to a provider as instructions.

Audit remains metadata-only. Prompts, final responses, credentials and host paths must never be added to audit records.

### 2.2 Task lifetime, interaction expiry and retention

Defaults:

- task timeout: `86400` seconds (24 h);
- terminal retention: `168 h` (7 d).

`deadline_at` is frozen when the task is created.

Approval/question requests carry `expires_at` and may never outlive the task deadline. There is no independent longer interaction lifetime.

At task deadline:

1. request provider cancellation best-effort;
2. stale any pending approval/question;
3. mark the task `interrupted`;
4. use `AGENT_TASK_TIMED_OUT`.

At interaction expiry:

- request becomes `stale`;
- waiter fails with `AGENT_INTERACTION_EXPIRED`;
- a late human response is rejected as `REQUEST_STALE`.

GC removes the complete retained task unit: task row, events, requests and spooled result. GC runs at startup, submission and terminal completion/cancellation. It never deletes nonterminal tasks.

### 2.3 Provider-aware restart reconciliation and persistent active-slot guard

The existing flock remains the live cross-process writer lease. v0.7.0 adds a Bridge-owned persistent recovery guard beside the shared lease files so the read-only MCP lock mount can observe stale recovery state:

`runtime/locks/active/<slot>`

The Bridge is the only writer of this guard. The MCP container only reads it through the existing read-only lock-directory bind mount.

For a workspace-write run:

1. acquire flock;
2. atomically publish the active-slot guard;
3. start the provider;
4. on a clean terminal path confirm the provider run has stopped/returned;
5. remove the guard;
6. release flock.

A Bridge crash can release flock while leaving the persistent guard. Container-side filesystem mutations must then fail closed with:

`WORKDIR_RECOVERY_REQUIRED`

The guard is cleared only after startup reconciliation can establish that the old provider is no longer active or that a safe takeover/reattachment succeeded. There is no blind rerun.

Reconciliation classifications are provider-neutral:

- `REATTACHED`
- `SESSION_RESUMABLE`
- `NOT_RECOVERABLE`
- `UNKNOWN`

Codex may be classified `REATTACHED` only when there is live proof that the exact native turn still exists, belongs to the persisted task/session, and event consumption can continue safely. Otherwise its persistent native session/thread may only support a later explicit continuation.

Claude is conservative: a persisted session id can make a task `SESSION_RESUMABLE`; ServerFS does not assume the prior subprocess survived a Bridge crash.

Unresolved recovery preserves native IDs and the active-slot guard, interrupts the old ServerFS task instead of rerunning it, and blocks new writes for that slot until the provider is proven stopped or a safe takeover is implemented.

Required reconciliation events include runtime/task reconciliation start and finish evidence; exact event type additions are additive under envelope v1.

### 2.4 Immutable execution manifest

Every task receives exactly one immutable manifest at creation. It is persisted as:

- `manifest_json`
- `manifest_sha256`

The canonical JSON is UTF-8, sorted keys, compact separators, no NaN; hash is SHA-256.

Manifest schema version is 1 and captures the execution facts needed to interpret a result later:

- `schema_version`
- `bridge_version`
- `protocol_version`
- runtime: name, measured version, declared `in_flight_recovery`
- workspace: alias, slot, relative cwd, profile
- policy: read-only, agent mode, `policy_sha256`
- limits
- advisor state
- continuation
- optional `correlation_id`
- `deadline_at`

There is no manifest update API. A later runtime/config change must not rewrite old task evidence.

### 2.5 Large final-result spool and exact retrieval

The existing `final_response > 256 KiB => AGENT_RESULT_TOO_LARGE` behavior changes:

- result <= 256 KiB: inline, unchanged success path;
- result > 256 KiB and <= 8 MiB: atomically spool to private Bridge state and mark task `succeeded`;
- result > 8 MiB: `AGENT_RESULT_TOO_LARGE`.

Private spool path:

`~/.local/state/serverfs-agent-bridge/results/<task_id>.txt`

under the configured `state_dir`; results directory mode 0700 and result file mode 0600.

SQLite records:

- `result_storage = inline | spool`
- `result_size_bytes`
- `result_sha256`

For spooled results, `get_agent_task` returns:

- a bounded `final_response` preview;
- `final_response_truncated: true`;
- `result.size_bytes`;
- `result.sha256`;
- `result.retrievable: true`.

A new read-only MCP tool / Bridge RPC reads the exact spooled UTF-8 result:

`read_agent_task_result(task_id, offset_bytes=0, max_bytes=65536)`

Return fields:

- `text`
- `offset_bytes`
- `next_offset_bytes`
- `eof`

`max_bytes` is capped at 64 KiB. Offsets are byte offsets. Chunks must end on a valid UTF-8 boundary so concatenating the returned UTF-8 bytes reproduces the stored bytes exactly. The spool is removed with task retention GC.

A `task.result_spooled` event records metadata only.

## 3. Compatibility rules

- Bridge protocol remains version 1; new optional params/RPC method are additive.
- Existing eight Agent tools retain their semantics; v0.7.0 adds exactly one read-only result retrieval tool.
- Existing inline results remain readable through `get_agent_task`.
- Existing SQLite databases are migrated additively at Bridge startup.
- Existing tasks created before v0.7 migration receive safe legacy/default values; no historical evidence is fabricated.
- Existing Jev behavior remains optional, fail-open and advisory only.
- Base filesystem-only deployments remain Agent-unaware unless Agent mode is enabled.

## 4. Verification contract

v0.7.0 is not releasable until executed evidence covers:

1. event envelope `schema_version=1` and correlation propagation;
2. correlation validation and opaque behavior;
3. immutable manifest creation and exact SHA-256 verification;
4. task timeout and stale HITL behavior;
5. 168 h retention GC including result-spool deletion;
6. active-slot guard creation, crash persistence and mutation-side `WORKDIR_RECOVERY_REQUIRED`;
7. provider-aware reconciliation with no blind rerun;
8. inline <=256 KiB, spooled >256 KiB to <=8 MiB, fail >8 MiB;
9. exact multi-chunk UTF-8 result reconstruction and SHA-256;
10. protocol/MCP schema tests for the new result reader;
11. full Agent Bridge gate;
12. full root gate;
13. deployment/config syntax gates where touched;
14. documentation/version consistency.

## 5. Integration sequence

1. Freeze this plan on the v0.7 development branch.
2. Implement persistence/models + manifest + correlation/event envelope.
3. Implement lifetime/expiry/retention.
4. Implement spool/retrieval.
5. Implement persistent guard + provider-aware startup reconciliation.
6. Wire MCP surface and mutation-side recovery blocking.
7. Add regression/unit/integration tests.
8. Update README, Agent Bridge docs, config/deployment docs, AGENTS and versions.
9. Run targeted gates, then all applicable release gates.
10. Commit and push the development branch.
11. Merge to `main`, remove the development branch, and verify a clean single-branch release-ready repository.

**Stop before creating the `v0.7.0` tag or GitHub Release.** The user must confirm the final `main` commit first.
