# ServerFS MCP v0.7.2 Development Plan

Status: frozen release plan; **v0.7.2 is the current stable release**
Theme: **Runtime State Reconciliation & Filesystem Reliability**
Previous stable baseline: v0.7.1 tag `7c73d27a6a4aa31073a4e4fd82767b545dd9664f`
Current release line: **v0.7.2**

> This document is retained as the frozen v0.7.2 design and acceptance record. Requirement language in the design sections describes the contract satisfied by the current release; it is not an outstanding release gate.

## 1. Release summary

v0.7.2 is the current stable maintenance release on the frozen v0.7 runtime contract.

It fixes demonstrated reliability defects and tightens deployment verification. It does not add Agent orchestration, new MCP tools, new Bridge RPC methods, provider lifecycle ownership, a new sandbox, or any new authorization path.

The four maintenance objectives are:

1. include the already-validated bounded-FD `find_files` fix that landed on `main` after v0.7.1;
2. close the Agent recovery state gap where reconciliation can prove a provider is inactive while the persisted ServerFS task remains non-terminal;
3. make post-deployment native-runtime readiness an explicit bounded verification option without starting, restarting, bootstrapping, killing, or otherwise managing provider daemons;
4. make the Agent-enabled upgrade path and post-deploy verification harder to execute incorrectly while preserving the explicit `compose.agent.yml` architecture.

## 2. Frozen compatibility boundary

v0.7.2 MUST preserve:

- the 11 / 13 / 20 / 22 MCP tool surfaces;
- Bridge protocol version 1;
- event envelope schema version 1;
- manifest schema version 1;
- existing workdir policy and shared writer-lease semantics;
- explicit opt-in Agent deployment through `compose.agent.yml`;
- the provider-native Codex/Claude execution model;
- the v0.6 Jev advisory-only authority boundary;
- base `compose.yml` remaining Agent-unaware;
- user-scoped deployment with no root/sudo/system service.

No change in this release may turn an uncertain recovery state into an automatic success. `provider_active=None` remains fail-closed.

## 3. Filesystem reliability

The post-v0.7.1 commit `987011f` is part of the v0.7.2 release baseline.

Required behavior:

- `find_files` must not retain open directory descriptors for every unvisited sibling;
- peak open FDs must be bounded by active traversal work rather than directory width;
- `EMFILE` / `ENFILE` must surface as `RESOURCE_EXHAUSTED`, not `ACCESS_DENIED`;
- symlink, deny-policy, hidden-path and traversal-budget behavior remains unchanged.

Required regression evidence:

- wide-tree traversal under a simulated low-FD ceiling succeeds with bounded peak descriptors;
- lower-level open failures map `EMFILE` / `ENFILE` to the resource-exhaustion error;
- the public `find_files` tool surface reports `RESOURCE_EXHAUSTED`.

## 4. Agent reconciliation state hygiene

### 4.1 Demonstrated defect

A persisted task can remain `running` or `waiting_*` after lazy recovery reconciliation has already produced:

```text
provider_active = false
```

The active-slot guard may then clear while the task row remains non-terminal. This leaves stale active-task accounting and requires manual cancellation to repair state.

### 4.2 Required semantics

When guard reconciliation proves `provider_active is False`:

1. if the task is already terminal, keep its terminal state;
2. if the task is non-terminal, transition it to `interrupted`;
3. use error code `AGENT_PROVIDER_INACTIVE`;
4. use a non-sensitive message stating that the provider is no longer active during recovery;
5. any pending approval/question becomes stale through the normal terminal transition path;
6. only after the task-state transition succeeds may the active-slot recovery guard be removed;
7. emit normal reconciliation evidence and a `task.interrupted` event.

When `provider_active is True` or `None`, the existing fail-closed behavior remains authoritative and the guard is not cleared by this path.

No blind rerun is introduced.

### 4.3 Required regression evidence

Cover at least:

- `running + provider_active=false -> interrupted + guard cleared`;
- `waiting_for_approval + provider_active=false -> interrupted + request stale + guard cleared`;
- unknown provider state continues to retain the guard and block a new writer.

## 5. Deployment and runtime-readiness verification

### 5.1 Boundary

ServerFS must not take new ownership of Codex or Claude daemon/process lifecycle.

The deployment verifier may observe runtime readiness but must not:

- invoke Codex `daemon start`, `bootstrap`, `update`, `restart`, or kill commands;
- delete or rewrite provider sockets;
- start Claude processes;
- modify provider authentication or settings.

### 5.2 Strict verification mode

`deployment/agent-bridge/verify_host.py` gains an explicit opt-in mode:

```bash
python3 deployment/agent-bridge/verify_host.py --require-runtimes
```

Default invocation preserves the previous structural/user-scope verification contract.

With `--require-runtimes`:

- read the enabled runtime set from the existing Bridge config;
- query the existing `runtime.list` RPC;
- retry only the read-only readiness probe for a bounded interval;
- require every enabled runtime to report `available=true`;
- fail with the names of runtimes still unavailable at the deadline;
- perform no provider lifecycle action.

The initial bound is 10 seconds. This is an acceptance/readiness check, not an auto-healing mechanism.

## 6. Agent-enabled upgrade hygiene

The explicit deployment split remains:

```text
compose.yml
  -> filesystem-only / filesystem+binary

compose.yml + compose.agent.yml
  -> Agent-enabled surfaces
```

The v0.7.2 documentation makes the canonical Agent-enabled upgrade sequence copy/paste safe and includes immediate post-deploy verification.

The published runbook:

- uses `--env-file .env`;
- includes both `-f compose.yml -f compose.agent.yml`;
- warns that recreating `serverfs-mcp` with base Compose alone removes Agent mounts even when Agent settings remain in `.env`;
- uses host verification with `--require-runtimes` for deployment acceptance;
- verifies container-to-Bridge `runtime.list` after recreation.

This was completed without adding `COMPOSE_FILE` to `.env` or making the base Compose Agent-aware.

## 7. Version and documentation consistency

Release consistency is complete:

- the root package version is 0.7.2;
- the Agent Bridge package/client version is 0.7.2;
- both lockfiles were refreshed through the project-standard `uv` workflow;
- README, `.env.example`, AGENTS, deployment docs, Agent Bridge docs, release references, and English/Chinese site content describe v0.7.2 as the current stable maintenance/reliability release;
- current MCP capability surfaces are documented consistently as 11 / 13 / 20 / 22 tools;
- site output is generated through the normal site build rather than hand-editing `site/dist`.

## 8. Verification evidence

The release gates were executed and passed:

1. targeted Agent reconciliation, deployment-verifier, and filesystem regression suites passed;
2. the full `agent_bridge/` gate passed, including frozen sync, Ruff check/format, and **115 tests**;
3. deployment shell syntax and Agent-overlay Compose rendering passed;
4. the full root gate passed, including frozen sync, Ruff check/format, **811 tests**, Compose config, and scratch-tag Docker source build;
5. the two-process Agent Bridge E2E harness passed **47 / 47** scenarios;
6. the documentation/site gate built **19 pages** and passed `git diff --check`;
7. GitHub Container and Pages workflows for the final release line completed successfully;
8. Edge was deployed with `compose.agent.yml` preserved and the MCP container reported healthy;
9. live acceptance proved Bridge package **0.7.2**, user service active/running with no restart loop, `verify_host.py --require-runtimes` passing, and container-to-Bridge `runtime.list` healthy;
10. Codex **0.157.0** and Claude Code **2.1.282** were both available, and bounded concurrent `find_files` smoke checks completed without `EMFILE` recurrence.

## 9. Delegation policy used for this development pass

During v0.7.2 development, direct ServerFS file primitives were preferred for inspection and edits. Delegation was reserved for capability gaps requiring a host shell or external runtime, including pytest/Ruff/uv/npm/Docker/systemd execution, lockfile regeneration, live deployment/runtime verification, and Git operations.

Delegated tasks used one objective, a narrow mutation boundary, explicit stop conditions, and minimal commands so each operation remained independently auditable.

## 10. Release state

v0.7.2 is the current stable ServerFS MCP release on the frozen v0.7 contract. The release is represented by the immutable `v0.7.2` tag and matching GitHub Release; both identify the final verified release commit and must not be moved or recreated.
