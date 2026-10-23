# ServerFS MCP v0.22.1 Maintenance Release Plan

Status: **released as stable v0.22.1**; see the [release checklist](docs/v0.22.1-release-checklist-2026-10-23.md) for tag, GitHub Release, Windows wheel workflow, and asset digest evidence.
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
6. When Tunnel+Bridge restart is requested, a launchd-owned one-shot child completes the strict restart and health closure independently of the Bridge-owned caller; final evidence is written only after verified cleanup.

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
- run the supported-Mac positive-path lifecycle acceptance for the changed self-restart boundaries: Bridge-only hand-over and Tunnel+Agent-Bridge hand-over;
- verify old managed PIDs are replaced where applicable, Bridge health is usable after reconnect, no duplicate managed/unmanaged Tunnel tree remains, and final hand-over success is published only after temporary job/plist cleanup;
- keep the unchanged Tunnel-only synchronous branch under deterministic regression coverage rather than modifying an active Bridge-enabled installation solely to manufacture a Tunnel-only manifest;
- cover readiness/health failure propagation deterministically; do not intentionally destabilize the active management path solely for live fault injection when the same fail-closed contract is already exercised by source-level tests.

Linux/VPS evidence must not be presented as real macOS `SMAppService` acceptance.

## 5. Fix C — macOS temporary launchd maintenance safety rule

### Problem

A one-shot delayed maintenance handoff was mistakenly implemented with a `launchctl submit` job that had keepalive behavior and repeatedly executed `serverfs service restart`. The product's normal lifecycle code does not use this mechanism; this is a maintainer-operation safety gap.

### Documentation changes

- Add a durable macOS invariant: normal maintenance must not use persistent/keepalive `launchctl submit` jobs as one-shot delayed actions.
- In maintainer workflow guidance, require bounded one-shot mechanisms for temporary handoff work and explicit verification that any temporary launchd job/artifact is gone after completion.
- Preserve the rule that direct operator/maintainer `launchctl` mutation is not an ordinary service-ownership path; project-owned launchd mutation is limited to the bounded one-shot migration/self-restart hand-over implemented by ServerFS.
- Keep `AGENTS.md` concise; add only a short repository-wide pointer/rule if needed so delegated maintainers see the hazard without duplicating the detailed procedure.

Acceptance:

- Maintainer docs are internally consistent with the existing macOS platform invariant.
- No product code is added solely to support temporary `launchctl submit` jobs.

## 6. Fix D — Tunnel+Bridge restart handover

### Live acceptance evidence

During supported-Mac v0.22.1 live acceptance, `serverfs service restart` with the managed Agent Bridge registered interrupted its Bridge-owned caller while unregistering the Bridge. The CLI never completed its synchronous restart/readiness/Bridge-health closure. Tunnel-only restart remains synchronous and is not implicated.

### Implementation contract

- Reuse the existing `KeepAlive=false`, `RunAtLoad=false` launchd handover job for Tunnel+Bridge restart. Its arguments are only `serverfs service restart --_handover-child`; it reads the installed private manifest and never guesses config or env paths.
- Apply the same handover to `serverfs agent-bridge restart` after any requested policy/config synchronization has finished. That child restarts only the Agent Bridge; it does not restart the Tunnel. Its working directory is the product-owned service-manager directory.
- The child skips handover scheduling, performs the existing strict unregister/register/readiness/Bridge-health closure, and records a private pending result (`complete=false`). A failed operation also records its failure before cleanup.
- An independent detached cleanup process waits for the operation child, boots out the temporary job, verifies `launchctl print` fails and removes/verifies the temporary plist. Only then may it publish the final result (`complete=true`). Cleanup failure produces final failure evidence while preserving the operation failure. Failure to start cleanup must never produce success.
- Tunnel-only restart remains direct and synchronous. The temporary job remains one-shot; no `launchctl submit`, `nohup`, root operation, repeating job or recursive handover is introduced.
- Central child finalization schedules cleanup on every normal child exit, including validation/configuration early returns.
- Native `serverfs service status` exposes the bounded final handover evidence and whether the temporary job/plist remain; common status projection stays unchanged.

### Regression tests

Cover Bridge restart scheduling without config/env argv, one-shot plist flags, child closure and pending evidence, Tunnel-only direct restart, early-failure cleanup scheduling, operation-failure preservation, final-evidence ordering, and launchd/plist cleanup verification.

### Acceptance boundary

VPS mocked/source-level tests prove orchestration contracts only. Repeat live acceptance on the supported Mac for the changed Bridge-only and Tunnel+Bridge hand-over paths; verify final result is complete only after the temporary launchd job and plist are absent. Keep the unchanged Tunnel-only synchronous branch under deterministic regression coverage unless a Tunnel-only installation is naturally available. Do not claim the interrupted acceptance as a pass.

## 7. Implementation order

1. Freeze this plan and confirm clean baseline.
2. Implement Fix A with focused regression tests; run the Linux installer gates.
3. Implement Fix B with mocked/source-level lifecycle regression tests; run focused Python gates on the VPS.
4. Implement Fix C in maintainer docs/AGENTS with the smallest wording necessary.
5. Run the combined affected validation set and `git diff --check`.
6. Review final diff for scope creep and unresolved dirty state.
7. Hand off only the real native acceptance portion to the Mac environment.
8. Implement Fix D after the newly observed live-acceptance interruption; rerun focused source-level checks, then repeat the Tunnel+Bridge native acceptance before release.

Each phase stops on a failing gate. Diagnose the demonstrated failure before proceeding; do not weaken tests or invariants to obtain green status.

## 8. Validation gates

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

## 9. VPS implementation evidence

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

These results were the pre-Fix-D Linux/VPS evidence. The supported-Mac implementation and acceptance evidence is recorded below.

## 10. Supported-Mac implementation and acceptance evidence

Fix D was implemented and validated on the supported Apple Silicon/macOS 27 environment on 2026-10-23.

Source/build gates:

- macOS ServiceManagement companion build and signature validation — PASS.
- `uv build --wheel --out-dir /tmp/serverfs-wheel-check` — PASS; produced `serverfs_mcp-0.22.0-py3-none-any.whl` for source validation without changing release metadata.
- Final Darwin-focused suite (`test_darwin_lifecycle.py`, `test_darwin_service.py`, `test_darwin_service_lifecycle.py`, `test_darwin_cli.py`) — **65 passed**.
- Root `uv run ruff check .` — PASS.
- Root `uv run ruff format --check .` — PASS; **452 files already formatted**.
- Root `uv run pytest` after repairing the macOS test-isolation boundary — **1566 passed, 395 skipped, 1 warning** in 65.18s.
- `git diff --check` — PASS.

The first full-root pytest attempt exposed a test-isolation defect: two legacy `agent-bridge restart` tests still mocked the old inline `restart_service()` boundary, so the new parent path could schedule a real hand-over on the developer Mac. `tests/test_darwin_lifecycle.py` was updated to mock `schedule_handover()` and explicitly fail if the parent attempts an inline Bridge restart. The subsequent full-root suite reached 100% without another Bridge shutdown.

Live hand-over evidence:

- A Bridge-only hand-over completed with native status evidence `complete=true`, `ok=true`, `job_loaded=false`, `plist_exists=false`, detail `agent-bridge restarted`; the Bridge was usable again through a new Agent task after replacement.
- Before the explicit Tunnel+Bridge restart, managed PIDs were Tunnel **70792** and Agent Bridge **77811**.
- `serverfs service restart` intentionally interrupted its Bridge-owned caller while the launchd-owned one-shot child continued the operation.
- After reconnection, managed PIDs were Tunnel **79300** and Agent Bridge **79297**; both registration states were `enabled`, `unmanaged_tunnel_pids=[]`, and a new Agent task executed successfully.
- Final native hand-over evidence was `complete=true`, `ok=true`, detail `restarted`, `job_loaded=false`, `plist_exists=false`. The hand-over introduced no additional repository files.

This proves the positive-path self-restart hand-over and verified cleanup on the supported Mac. Deliberately withholding live Bridge readiness/health was not performed because that would intentionally destabilize the active management path; deterministic regression tests cover readiness/health failure propagation and cleanup-failure evidence instead.

## 11. Completion contract

The VPS implementation phase is complete only when:

- every changed line is traceable to Fix A, B, C, or D;
- focused tests and applicable lint/format/shell checks pass;
- final diff contains no unrelated cleanup;
- documentation explicitly distinguishes VPS source/test evidence from unexecuted Mac native acceptance;
- the remaining Mac acceptance steps are concrete and reproducible.

The implementation and supported-Mac acceptance described in this plan preceded publication. The v0.22.1 release was subsequently published; exact release evidence is recorded in the release checklist.
