# ServerFS MCP v0.7.2 Development Plan

Status: implementation plan in progress  
Theme: **Runtime State Reconciliation & Filesystem Reliability**  
Released baseline: v0.7.1 tag `7c73d27a6a4aa31073a4e4fd82767b545dd9664f`  
Current main baseline: `987011fbc3bee2c125da52c7bed3056741fd16f9`

## 1. Release intent

v0.7.2 is a maintenance release on the frozen v0.7 runtime contract.

It fixes demonstrated reliability defects and tightens deployment verification. It does not add Agent orchestration, new MCP tools, new Bridge RPC methods, provider lifecycle ownership, a new sandbox, or any new authorization path.

The four maintenance objectives are:

1. publish the already-validated bounded-FD `find_files` fix that landed on `main` after v0.7.1;
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

v0.7.2 documentation must make the canonical Agent-enabled upgrade sequence copy/paste safe and include immediate post-deploy verification.

At minimum, the runbook must:

- use `--env-file .env`;
- include both `-f compose.yml -f compose.agent.yml`;
- warn that recreating `serverfs-mcp` with base Compose alone removes Agent mounts even when Agent settings remain in `.env`;
- run host verification with `--require-runtimes` for release acceptance;
- verify container-to-Bridge `runtime.list` after recreation.

Do not solve this by adding `COMPOSE_FILE` to `.env` or by making the base Compose Agent-aware.

## 7. Version and documentation consistency

Only after runtime/filesystem/deployment targeted tests are green:

- bump root package version to 0.7.2;
- bump Agent Bridge package/client version to 0.7.2;
- refresh lockfiles through the project-standard `uv` workflow;
- update README, `.env.example`, AGENTS, deployment docs, Agent Bridge docs, site English/Chinese content and release references;
- regenerate site output through the normal site build rather than hand-editing `site/dist`;
- describe v0.7.2 strictly as a maintenance/reliability release.

## 8. Verification sequence

Run in this order so failures remain cheap and localized:

1. targeted Agent reconciliation tests;
2. targeted deployment-verifier tests;
3. targeted filesystem regression tests;
4. full `agent_bridge/` gate:
   - `uv sync --frozen`
   - `uv run ruff check .`
   - `uv run ruff format --check .`
   - `uv run pytest`
5. deployment shell syntax gate:
   - `bash -n deployment/agent-bridge/*.sh`
6. full root gate:
   - `uv sync --frozen`
   - `uv run ruff check .`
   - `uv run ruff format --check .`
   - `uv run pytest`
   - `docker compose config`
   - `SERVERFS_IMAGE=serverfs-mcp:dev docker compose build`
7. site gate after documentation/version updates:
   - `cd site && npm ci && npm run build`
   - `git diff --check`
8. build/publish the normal `edge` image from the final commit;
9. deploy Edge while preserving the Agent overlay;
10. verify:
    - Bridge package version 0.7.2;
    - user service active/running with no restart loop;
    - `verify_host.py --require-runtimes`;
    - container-to-Bridge `runtime.list`;
    - Codex and Claude both available;
    - 20/22-tool Agent surface as configured;
    - bounded concurrent `find_files` smoke;
    - no stale non-terminal task remains after an inactive-provider recovery scenario.

## 9. Delegation policy for this development pass

Prefer direct ServerFS file primitives for inspection and edits.

Delegate only capability gaps that require a host shell or external runtime, such as:

- executing pytest/ruff/uv/npm/docker/systemd commands;
- regenerating lockfiles;
- live deployment and provider/runtime verification;
- Git commit/push operations when requested.

Each delegated task should have one objective, a narrow mutation boundary, explicit stop conditions, and minimal commands so it can complete quickly.

## 10. Release stop condition

Prepare `main` to a fully verified, clean, release-ready v0.7.2 state.

**Do not create the v0.7.2 tag or GitHub Release until the maintainer explicitly authorizes release.**
