# ServerFS MCP v0.22.1 Maintenance Release Plan

Status: VPS implementation complete; supported-Mac native acceptance pending; not released
Baseline: current clean `main` after v0.22.0
Scope: Linux Agent Bridge installer compatibility, macOS native restart reliability, and maintainer safety guidance

## 1. Objective

v0.22.1 is a narrow maintenance release for two defects observed during real v0.22.0 upgrades and one operational safety gap exposed by the macOS recovery procedure.

Success means:

1. Linux Agent Bridge installation no longer depends on the host's ambient `python3` version after the release virtual environment has been created; helper execution uses the staged Bridge environment that already satisfies `agent_bridge/pyproject.toml` (`Python >=3.12`).
2. macOS `serverfs service restart` does not report success until the requested managed services have completed the stop/re-register/readiness closure; an enabled Agent Bridge must additionally pass its existing RPC health probe.
3. Restart failures fail closed instead of being converted into a superficially successful lifecycle result.
4. Maintainer guidance explicitly forbids using persistent/keepalive `launchctl submit` jobs as one-shot delayed maintenance handoffs and requires bounded one-shot mechanisms plus post-operation cleanup verification.
5. Linux regression tests and source-level macOS lifecycle tests pass on the VPS; real `SMAppService`/launchd acceptance is performed separately on the supported Mac before release.

## 2. Non-goals

- No MCP tool/schema changes.
- No Agent Bridge RPC/provider/runtime behavior changes.
- No system Python replacement, PATH persistence, root operation, or host-wide Python configuration change.
- No redesign of the macOS `SMAppService`/LaunchAgent architecture.
- No new generic launchd helper framework.
- No Windows lifecycle changes.
- No website/product-copy expansion unless implementation changes create a release-facing behavior statement that must be updated.
- No commit, push, tag, GitHub Release, or deployment as part of implementation unless separately requested.

## 3. Fix A — Linux Agent Bridge installer Python selection

### Problem

`agent_bridge/pyproject.toml` requires Python `>=3.12`, and `uv sync --frozen` creates the staged release virtual environment accordingly. The installer then invokes `deployment/agent-bridge/render_config.py` with ambient `python3`, which can still be Python 3.11 and fail before activation. This was observed on the VPS where system `python3` was 3.11.15 while Python 3.12.13 was already available.

### Implementation

- After the release environment is created, execute Python deployment helpers that require the Bridge code/runtime contract with `"$RELEASE_DIR/.venv/bin/python"`, not ambient `python3`.
- Keep the installer entirely current-user scoped and preserve existing transactional rollback behavior.
- Avoid adding a separate `python3.12` lookup or permanently modifying PATH; the release venv is the canonical interpreter after `uv sync` succeeds.
- Make the installer's post-install validation guidance use the compatible interpreter path where appropriate so the documented next step does not reintroduce the same ambient-Python hazard.

### Regression tests

- Lock the installer contract so config rendering uses the staged release interpreter.
- Add a subprocess regression proving installation can proceed when ambient `python3` is absent/incompatible while the staged venv interpreter is usable.
- Preserve existing non-root, current-user, transactional and `--no-start` tests.

Acceptance:

- `bash -n deployment/agent-bridge/*.sh` passes.
- `uv run pytest tests/test_agent_deployment.py` passes.

## 4. Fix B — macOS `service restart` stop/readiness/health closure

### Problem

Current macOS `restart()` is effectively `stop(); start()`. `stop()` waits for unregister completion and known managed-process exit, but `start()` registers services and returns immediately. Therefore restart can report success before replacement processes are active, and before an enabled Agent Bridge has passed the health probe already used during installation.

### Implementation

- Keep normal lifecycle on `SMAppService` + app-associated per-user LaunchAgents.
- Refactor only as much as necessary to share a narrow "register and verify" lifecycle primitive between ordinary start/restart and existing install activation where semantics match.
- For every service represented in the installed manifest:
  - registration must return `enabled`; `requires-approval` and unexpected states fail closed with an operator-facing error;
  - wait for the managed process to become active with the existing bounded `_wait_managed_job()` mechanism;
  - when the service is Agent Bridge, run existing bounded `_wait_bridge_health()` after the process is active.
- `restart()` must finish the full stop phase before registration begins, preserving the existing `_unregister_and_wait()` process-exit contract.
- Do not treat an Agent Bridge absent from the manifest as a restart failure.
- Do not copy install-only legacy migration or rollback semantics into ordinary restart.
- `restart_service(service)` should provide the same readiness/health guarantees for the selected service.

### Regression tests

Add source-level Darwin lifecycle tests covering:

- restart ordering: unregister/wait/old PID exit precedes re-register/readiness;
- Tunnel waits for managed-process readiness;
- enabled Agent Bridge waits for managed process and then RPC health;
- Bridge-absent manifest does not create a false failure;
- `requires-approval`, missing process/readiness timeout, or Bridge health failure propagates as failure rather than success;
- selected-service restart follows the same readiness/health contract.

Acceptance on VPS:

- focused Darwin lifecycle tests pass where platform-independent mocked contracts are collectible;
- root Ruff/format checks relevant to changed Python source pass.

Required final macOS acceptance before release:

- build/validate the ServiceManagement companion;
- run the supported-Mac native lifecycle acceptance for `service restart` with Tunnel-only and Tunnel+Agent-Bridge configurations;
- verify old PIDs terminate, replacement PIDs appear, Bridge health is usable, no duplicate managed/unmanaged Tunnel tree remains, and failure is surfaced when readiness/health is deliberately withheld.

Linux/VPS evidence must not be presented as real macOS `SMAppService` acceptance.

## 5. Fix C — macOS temporary launchd maintenance safety rule

### Problem

A one-shot delayed maintenance handoff was mistakenly implemented with a `launchctl submit` job that had keepalive behavior and repeatedly executed `serverfs service restart`. The product's normal lifecycle code does not use this mechanism; this is a maintainer-operation safety gap.

### Documentation changes

- Add a durable macOS invariant: normal maintenance must not use persistent/keepalive `launchctl submit` jobs as one-shot delayed actions.
- In maintainer workflow guidance, require bounded one-shot mechanisms for temporary handoff work and explicit verification that any temporary launchd job/artifact is gone after completion.
- Preserve the existing rule that raw `launchctl` mutation is limited to narrowly implemented migration/diagnostic code paths rather than ordinary service ownership.
- Keep `AGENTS.md` concise; add only a short repository-wide pointer/rule if needed so delegated maintainers see the hazard without duplicating the detailed procedure.

Acceptance:

- Maintainer docs are internally consistent with the existing macOS platform invariant.
- No product code is added solely to support temporary `launchctl submit` jobs.

## 6. Implementation order

1. Freeze this plan and confirm clean baseline.
2. Implement Fix A with focused regression tests; run the Linux installer gates.
3. Implement Fix B with mocked/source-level lifecycle regression tests; run focused Python gates on the VPS.
4. Implement Fix C in maintainer docs/AGENTS with the smallest wording necessary.
5. Run the combined affected validation set and `git diff --check`.
6. Review final diff for scope creep and unresolved dirty state.
7. Hand off only the real native acceptance portion to the Mac environment.

Each phase stops on a failing gate. Diagnose the demonstrated failure before proceeding; do not weaken tests or invariants to obtain green status.

## 7. Validation gates

During implementation use focused tests first. Before declaring the VPS portion complete, run the smallest complete affected set:

```bash
bash -n deployment/agent-bridge/*.sh
uv run pytest tests/test_agent_deployment.py
uv run pytest tests/test_darwin_service_lifecycle.py tests/test_darwin_service.py
uv run ruff check deployment/agent-bridge tests/test_agent_deployment.py src/serverfs_mcp/darwin_service.py tests/test_darwin_service_lifecycle.py tests/test_darwin_service.py
uv run ruff format --check deployment/agent-bridge tests/test_agent_deployment.py src/serverfs_mcp/darwin_service.py tests/test_darwin_service_lifecycle.py tests/test_darwin_service.py
git diff --check
```

Because the change crosses base deployment/native lifecycle boundaries, run the broader root gate if focused checks are green and practical in the current VPS environment:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

For the Agent Bridge package itself, run its package-local gate only if files under `agent_bridge/` are changed; this plan does not currently require such a change.

For the final supported-Mac gate, follow `docs/maintainers/workflow.md`, including:

```bash
bash native/macos/service_manager/build.sh /tmp/serverfs-service-manager-check
uv build --wheel --out-dir /tmp/serverfs-wheel-check
```

and live current-user `SMAppService` lifecycle acceptance.

## 8. VPS implementation evidence

Implementation and Linux/VPS validation completed on 2026-10-23 with the following measured evidence:

- Fix A: `bash -n deployment/agent-bridge/*.sh` — PASS.
- Fix A: `uv run pytest tests/test_agent_deployment.py` — **79 passed**.
- Fix B source-level lifecycle tests: `uv run pytest tests/test_darwin_service_lifecycle.py tests/test_darwin_service.py` — **11 passed, 21 skipped**; the 21 existing Darwin-only tests are intentionally skipped on Linux.
- Root dependency sync: `uv sync --frozen` — PASS; 37 packages checked and no new repository changes introduced.
- Root lint: `uv run ruff check .` — PASS.
- Root format check: `uv run ruff format --check .` — PASS; **452 files already formatted**.
- Root test suite: `uv run pytest` — **1747 passed, 317 skipped, 1 warning** in 65.54s. The warning is an existing Starlette test-client deprecation warning involving `anyio.abc.BlockingPortal`, not a v0.22.1 failure.
- `git diff --check` — PASS.
- Final working-tree review found exactly seven v0.22.1 files and no staged or unrelated dirty state: `dev_plan_v0.22.1.md`, `deployment/agent-bridge/install.sh`, `tests/test_agent_deployment.py`, `src/serverfs_mcp/darwin_service.py`, `tests/test_darwin_service_lifecycle.py`, `docs/maintainers/invariants.md`, and `docs/maintainers/workflow.md`.

These results prove the Linux installer regression fix and source-level macOS lifecycle contract only. Real macOS `SMAppService`, Login Items, launchd process replacement and live Tunnel/Bridge behavior remain **Not verified** until the supported-Mac acceptance above is executed.

## 9. Completion contract

The VPS implementation phase is complete only when:

- every changed line is traceable to Fix A, B, or C;
- focused tests and applicable lint/format/shell checks pass;
- final diff contains no unrelated cleanup;
- documentation explicitly distinguishes VPS source/test evidence from unexecuted Mac native acceptance;
- the remaining Mac acceptance steps are concrete and reproducible.

Release status remains **not released** until the separate Mac acceptance and normal release workflow are explicitly authorized and completed.
