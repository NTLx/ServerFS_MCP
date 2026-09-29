# ServerFS MCP v0.7.3 Development Plan

Status: active development plan; **v0.7.2 remains the current stable release**
Theme: **Runtime Lifecycle Reliability**
Previous stable baseline: v0.7.2
Target release line: **v0.7.3**

> v0.7.3 is a narrow reliability release. It addresses task-lifecycle defects exposed when
> ChatGPT/MCP callers time out or disappear after Agent submission. It must not turn ServerFS
> into a scheduler, workflow engine, provider supervisor, or generic task registry.

## 1. Problem statement

The v0.7.2 runtime contract correctly decouples a submitted Agent task from one short-lived
MCP RPC connection, but that creates two demonstrated lifecycle gaps in the ChatGPT usage
model.

### 1.1 Ambiguous submit outcome

The current submission path is:

```text
ChatGPT / MCP caller
  -> serverfs-mcp AgentBridgeClient
  -> Bridge task.submit
  -> persist task + acquire writer lease/guard + schedule provider task
  -> return task_id
```

If the task is committed but the response is lost or the caller times out before receiving
`task_id`, provider work may already be running while the caller has no stable handle with
which to poll or cancel it.

The current `correlation_id` cannot solve this: the frozen v0.7 contract explicitly keeps
it opaque, non-unique, and unavailable for idempotency. The public MCP surface also has no
task-list/search API.

This is an unknown-outcome / ambiguous-commit defect.

### 1.2 Caller disappearance can retain resources too long

A successful `task.submit` starts Bridge-owned background execution. Closing the submitting
RPC, stopping polling, or losing a ChatGPT conversation does not cancel the task.

Current defaults are:

- task deadline: 86,400 seconds / 24 hours;
- terminal retention: 168 hours / 7 days;
- maximum active tasks: 4;
- approval/question expiry: task deadline;
- provider event-idle timeout: optional and intentionally disabled while waiting for human
  interaction.

For `workspace-write` tasks this can retain a writer lease and persistent recovery guard for
far longer than a normal ChatGPT interaction. Four abandoned non-terminal tasks can also
consume the global active-task budget.

The Bridge already cleans up correctly on normal completion, explicit cancellation, task
deadline, and orderly shutdown. v0.7.3 therefore focuses on bounded lifetime and
recoverability, not replacing the existing cleanup model.

## 2. Design principles and compatibility boundary

v0.7.3 MUST preserve:

- provider-native Codex and Claude execution;
- the existing writer `flock` plus persistent recovery-guard authority model;
- fail-closed recovery when provider state cannot be proven;
- Bridge protocol version 1 and event/manifest schema version 1 where additive fields suffice;
- existing authorization, workdir policy, approval/question decisions, and Jev advisory-only
  authority boundaries;
- explicit Agent deployment through `compose.agent.yml`;
- user-scoped deployment with no root/sudo/system service;
- base `compose.yml` remaining Agent-unaware;
- `correlation_id` remaining opaque metadata only and never becoming an idempotency key.

v0.7.3 MUST NOT:

- infer task abandonment from one UDS/MCP connection closing;
- automatically cancel a healthy task merely because it has not been polled;
- add a generic task-list/search MCP API solely to repair submission ambiguity;
- blindly clear a recovery guard when provider stop is unproven;
- auto-start/restart/kill provider daemons as a recovery strategy;
- introduce a workflow scheduler, heartbeat protocol, distributed lock service, or background
  orchestration subsystem.

## 3. Retry-safe Agent submission

### 3.1 New idempotency key

Add one optional caller-supplied field:

```text
idempotency_key
```

Contract:

- UTF-8 string;
- at most 256 encoded bytes;
- no ASCII control characters;
- opaque to ServerFS business logic;
- never forwarded to the provider as instructions;
- persisted on the task and immutable;
- copied into the immutable execution manifest;
- returned by task submission/read metadata where appropriate;
- unique while the owning task record remains retained.

This field is distinct from `correlation_id`.

The public `submit_agent_task` tool and Bridge `task.submit` RPC gain the field additively.
Existing callers that omit it retain v0.7.2 at-most-one-attempt-per-call behavior, but do not
gain retry recovery.

ServerFS/ChatGPT usage guidance MUST recommend supplying a fresh idempotency key for every
logical Agent objective and reusing exactly that key when retrying an uncertain submission.

### 3.2 Submission fingerprint

A task with an idempotency key stores an immutable request fingerprint derived from the
semantic submission inputs:

- runtime;
- workdir;
- normalized relative path;
- profile;
- SHA-256 of the exact UTF-8 prompt bytes;
- `continue_from_task_id`;
- `correlation_id`.

The raw prompt is not duplicated into idempotency metadata.

### 3.3 Retry semantics

When `task.submit` receives an idempotency key:

1. look up an existing retained task with that key before any writer lease is acquired;
2. if the stored fingerprint matches, return the existing task handle/state;
3. do not repeat Jev preflight, acquire another writer lease, publish another guard, create a
   second task row, or start another provider turn;
4. if the key exists but the fingerprint differs, fail with
   `AGENT_IDEMPOTENCY_CONFLICT`;
5. if no task exists, continue through normal authorization/preflight/submission;
6. re-check the key under the existing submission critical section before lease acquisition
   and task creation so concurrent retries cannot create duplicate work;
7. enforce uniqueness in SQLite as a final integrity boundary.

A retry after terminal completion returns the same retained task. Once retention GC removes
the task, the key may be reused because ServerFS no longer has evidence that the old logical
submission exists.

### 3.4 Required regression evidence

Cover at least:

- response-loss simulation: task is persisted, caller times out, retry with the same key
  returns the original `task_id`;
- retry does not create a second provider turn;
- retry does not create a second writer lease or recovery guard;
- retry does not repeat Jev preflight once a matching task already exists;
- concurrent same-key submissions converge on one task;
- same key + changed prompt/runtime/workdir/path/profile/continuation/correlation fails with
  `AGENT_IDEMPOTENCY_CONFLICT`;
- submission without an idempotency key preserves current behavior.

The root MCP client test suite MUST include the previously missing case where the server has
accepted a submission but the client does not receive the successful response.

## 4. Configurable bounded task lifetime

The hard-coded lifecycle values needed for resource control become explicit Bridge
configuration rendered from the existing repository-root `.env`.

Add:

```text
SERVERFS_AGENT_TASK_TIMEOUT_SECONDS=7200
SERVERFS_AGENT_INTERACTION_TIMEOUT_SECONDS=1800
SERVERFS_AGENT_MAX_ACTIVE_TASKS=4
SERVERFS_AGENT_TASK_RETENTION_HOURS=168
```

Initial v0.7.3 defaults:

- task timeout: **7200 seconds / 2 hours**;
- approval/question timeout: **1800 seconds / 30 minutes**;
- max active tasks: **4**;
- terminal retention: **168 hours / 7 days**.

Rationale:

- ServerFS delegates atomic objectives rather than unattended day-long workflows;
- two hours leaves substantial margin for builds/tests and native Agent execution while
  bounding an abandoned writer lease;
- thirty minutes is long enough for an ordinary human approval/question round-trip while
  preventing a lost ChatGPT conversation from holding a write slot for a full task lifetime;
- retention and active-task count keep their existing defaults.

All values MUST be strict positive integers and validated at configuration render/load time.
The immutable task manifest freezes the effective values used by that task.

No lifecycle limit is controlled by the MCP caller; these remain administrator policy.

## 5. Independent interaction timeout

Approval/question expiry is separated from the overall task deadline.

For every pending interaction:

```text
expires_at = min(task.deadline_at, now + interaction_timeout)
```

If the interaction is still pending at `expires_at`:

1. atomically mark the request stale;
2. best-effort cancel/interrupt the provider turn;
3. stop the Bridge background task;
4. transition the ServerFS task to `interrupted`;
5. use error code `AGENT_INTERACTION_TIMED_OUT`;
6. emit `task.interrupted` evidence;
7. release the live writer lease;
8. clear the persistent recovery guard only when the existing provider-stop/reconciliation
   rules permit it.

A late answer remains rejected as `REQUEST_STALE`.

Human-wait time is still not treated as provider event-idle time. The new interaction
deadline is the authoritative bound instead.

Implementation should use one bounded timer/watchdog per pending interaction and cancel that
timer immediately when the interaction resolves, is abandoned, the task terminates, or the
Bridge closes. Do not add a polling loop.

## 6. Resource cleanup invariants

For every task terminalization path, v0.7.3 must prove the following ownership rules.

### 6.1 Normal success/failure

- provider connection/client closes;
- request-handler tasks are cancelled/drained;
- pending interaction waiter/timer is removed;
- approval-advice cache and per-task cancellation markers are removed;
- result is inline/spooled according to the existing bounded contract;
- writer lease releases;
- recovery guard clears only after a clean provider stop/return.

### 6.2 Explicit user cancellation

- provider cancellation is attempted once;
- Bridge background/provider tasks are cancelled/drained;
- pending interaction becomes stale;
- interaction timer is cancelled;
- task becomes `cancelled`;
- writer lease releases;
- recovery guard follows the existing proof rule.

Repeated cancellation remains safe.

### 6.3 Task deadline

- provider cancellation is best-effort;
- pending interaction becomes stale;
- interaction timer is cancelled;
- task becomes `interrupted` with `AGENT_TASK_TIMED_OUT`;
- writer lease releases;
- guard clears only when provider stop can be established.

### 6.4 Interaction deadline

- semantics are those in section 5;
- this is an interruption, not a synthetic deny/approval decision.

### 6.5 Bridge shutdown/crash/provider disconnect

Existing v0.7.2 shutdown and recovery behavior remains authoritative:

- orderly Bridge shutdown interrupts active tasks and releases in-process resources;
- process death naturally releases `flock`;
- the persistent guard survives abnormal death;
- restart/lazy reconciliation may clear it only after provider inactivity is proven;
- unknown provider state remains fail-closed with `WORKDIR_RECOVERY_REQUIRED`.

## 7. Retention and garbage collection

Terminal GC remains a complete task-unit deletion:

- task row;
- events;
- pending-request history;
- spooled result;
- idempotency key/fingerprint because they live with the retained task.

GC continues to run at startup, submission, and terminal completion/cancellation. It never
deletes a non-terminal task merely because the caller disappeared.

No additional daemon, cron job, or periodic GC loop is required for v0.7.3.

## 8. Persistence and migration

SQLite migration is additive and in-place.

Existing task rows receive NULL for new idempotency fields. Historical tasks must not be
assigned fabricated keys or fingerprints.

The migration must be safe across repeated Bridge starts.

The unique constraint/index must allow multiple NULL idempotency keys while rejecting two
retained tasks with the same non-NULL key.

No event or manifest schema-version bump is required if the new fields are additive and
older records remain readable. If implementation proves that this cannot be done cleanly,
stop and reassess before changing a schema version.

## 9. Public surface and documentation

Expected public-surface change:

- no new MCP tool;
- no removed MCP tool;
- `submit_agent_task` gains optional `idempotency_key`;
- Bridge `task.submit` gains the matching optional field;
- task result metadata may expose the key so a recovered handle is inspectable.

The documented Agent tool counts therefore remain unchanged.

Update at least:

- root README;
- `.env.example`;
- AGENTS.md;
- Agent Bridge README;
- deployment Agent Bridge README/config rendering docs;
- English/Chinese Agent Bridge site docs;
- release/version references after implementation is accepted.

Documentation MUST distinguish:

- MCP/Bridge RPC timeout;
- Agent task timeout;
- human interaction timeout;
- terminal retention;
- provider event-idle timeout.

These are different mechanisms and must not be described interchangeably.

## 10. Implementation sequence

### Phase A — freeze regression tests first

Add failing tests for:

1. accepted-submit / lost-response / same-key retry;
2. same-key conflict;
3. concurrent retry convergence;
4. interaction timeout terminalization and stale late response;
5. interaction timeout resource release;
6. timeout with unproven provider stop retaining the recovery guard;
7. configuration parsing/rendering/defaults.

Do not change implementation until the new tests demonstrate the current defects.

### Phase B — persistence and idempotent submit

Implement:

- task-store fields/index/migration;
- fingerprint helper;
- preflight fast-path lookup;
- under-submit-lock second lookup;
- exact retry/conflict semantics;
- MCP/RPC field validation and propagation.

Run targeted store/service/protocol/MCP tests.

### Phase C — lifecycle configuration

Implement the four lifecycle configuration values through:

```text
.env
  -> deployment render_config.py
  -> private Bridge JSON config
  -> BridgeConfig
  -> BridgeLimits
  -> immutable task manifest
```

No lifecycle policy value is passed through the MCP container as provider authority.

Run config/render/deployment tests.

### Phase D — interaction watchdog and cleanup

Implement independent interaction expiry using bounded asyncio timer tasks with explicit
ownership and cancellation.

Do not implement a polling sweeper.

Run targeted service + Codex + Claude adapter tests, including cancellation races.

### Phase E — two-process E2E

Extend `tests/e2e/run_e2e.py` to prove through the published MCP surface:

- same idempotency key returns the same task after an uncertain first submission;
- only one provider execution occurs;
- a waiting interaction expires;
- the task becomes interrupted with the expected code;
- late answer is stale;
- the shared writer lease becomes available after cleanup;
- unresolved provider state still leaves the recovery guard fail-closed.

### Phase F — full verification and documentation

Run all applicable gates:

Agent Bridge:

```bash
cd agent_bridge
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Root:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
docker compose --env-file .env -f compose.yml -f compose.agent.yml config
SERVERFS_IMAGE=serverfs-mcp:dev docker compose build
```

Deployment:

```bash
bash -n deployment/agent-bridge/*.sh
```

E2E:

```bash
uv run python tests/e2e/run_e2e.py
```

Site/documentation:

```bash
cd site
npm ci
npm run build
```

Then `git diff --check`.

Live acceptance must use the normal user-scoped deployment flow and verify both native
runtimes without starting/restarting provider daemons merely for the test.

## 11. Acceptance criteria

v0.7.3 is implementation-complete only when all of the following are demonstrated:

1. a lost submission response can be recovered by retrying the same idempotency key;
2. recovery returns the original task and never starts duplicate provider work;
3. conflicting reuse of a key fails deterministically;
4. ChatGPT/client disconnection alone still does not falsely cancel a healthy asynchronous
   task;
5. every task has an administrator-configured bounded deadline;
6. every approval/question has a shorter independent bounded deadline;
7. interaction expiry terminalizes the task and attempts provider cancellation;
8. writer leases are released on all clean terminal paths;
9. recovery guards are removed only with the existing proof standard;
10. unknown provider state remains fail-closed;
11. active-task accounting cannot remain stale after proven provider inactivity;
12. lifecycle configuration is validated, rendered, documented, and frozen into manifests;
13. existing v0.7.2 filesystem/reconciliation regressions remain green;
14. full root, Bridge, deployment, E2E and documentation/site gates pass.

## 12. Explicit non-goals

Not in v0.7.3:

- task scheduling/queues beyond the existing active-task limit;
- periodic heartbeats from ChatGPT;
- cancelling tasks merely because polling stops;
- task ownership tied to one HTTP/UDS connection;
- a public task browser/list API;
- automatic continuation/retry of failed provider turns;
- provider daemon lifecycle management;
- changes to provider authorization or sandbox semantics;
- changes to Jev authority;
- new filesystem capabilities.

## 13. Release rule

Until all acceptance gates pass, v0.7.2 remains the stable release and v0.7.3 is development
state only.

Do not tag, publish, or deploy v0.7.3 as stable until the implementation, regression tests,
full gates, documentation/version alignment, and live acceptance are complete.
