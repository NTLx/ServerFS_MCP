# macOS v0.25.0 release-readiness documentation reconciliation — 2026-11-15

> Historical checklist for the 2026-11-15 checkpoint, superseded as a readiness summary by [the 2026-11-16 acceptance record](v0250-macos-release-readiness-2026-11-16.md) and the [current integrated release checklist](../v0.25.0-release-checklist-2026-11-21.md). The dated status, version, blocked operations and unchecked boxes remain evidence, not present instructions. The later [cooperative start/unstage lock](v0250-macos-shared-lock-and-wheel-gates-2026-11-15.md) closes the earlier missing shared lock, but not noncooperative lifecycle races. Do not infer cleanup, exact-SHA artifacts or release authorization from later unrelated passes.

This checklist reconciles current-state documentation against the available
macOS v0.25.0 evidence. It does not authorize registration, deployment,
credentials, production ingress, or release publication. The current stable
release remains v0.24.2.

## Completed evidence

- [x] Named file-only and owned Core lifecycles passed native acceptance;
  isolated multi-Connector-ID lifecycle passed in both modes with real signed
  `SMAppService` and synthetic no-network Connectors. See
  [multi-Connector-ID acceptance](v0250-macos-connector-native-acceptance-2026-11-14.md)
  and [architecture status](../architecture/v0.25.0-macos-multiple-connector-ids.md).
- [x] Shared Bridge v2 three-process evidence covers one independent Bridge
  owner and two Core processes, IPC/HTTP task isolation, owner restart, grant
  rotation and revocation. See
  [three-process acceptance](v0250-macos-shared-three-process-acceptance-2026-11-15.md).
- [x] v3 managed runtime site-packages integrity implementation and its
  offline acceptance were recorded. A separate exact-HEAD offline runtime
  install used commit `da9c12e600d3b336f1fd7a3d5232702e9abbc19f` and the
  archived project version **0.24.2**, not a v0.25.0 release commit. See
  [site-packages integrity](v0250-macos-shared-runtime-site-integrity-2026-11-15.md)
  and [exact-commit install](v0250-macos-exact-commit-real-offline-install-2026-11-15.md).
- [x] Two real signed Shared Core Apps passed non-registering `shared-preflight`
  against one live independent Bridge owner. This proves authorization and
  native validation gates only. See
  [signed owner preflight](v0250-macos-v3-signed-owner-preflight-2026-11-15.md)
  and [provenance preflight](v0250-macos-shared-preflight-provenance-2026-11-15.md).
- [x] The two signed Shared Core Apps also passed real Launcher `--validate-only`
  and Manager `register-preflight` rejection for four test-owned mutations:
  TOML SHA mismatch, environment allowed-host drift, manifest owner mismatch,
  and signed Python executable mismatch. Test result **1 passed**, with restored
  inputs, unchanged protected production PIDs and verified candidate cleanup;
  no `SMAppService.register()` call occurred. See
  [signed Launcher negative preflight](v0250-macos-signed-launcher-negative-preflight-2026-11-15.md).
- [x] Synthetic Shared Core start/rollback, stop, preflight and unstage now
  include malformed Manager state, loaded launchd Jobs without a PID, strict
  rc113 absence diagnosis, post-unregister terminal presence checks and a
  non-blocking inode lock across **cooperative same-Core shared-start** calls.
  Unstage checks exact Job absence twice before deletion; it does not share
  the start lock, so full cross-process atomicity is NOT established. See
  [lifecycle batch](v0250-macos-shared-core-lifecycle-batch-2026-11-15.md),
  [stop-safety diagnostic](v0250-macos-shared-stop-safe-2026-11-15.md) and
  [post-presence source gate](v0250-macos-post-presence-full-source-regression-2026-11-15.md).
- [x] **Signed non-registration repeat acceptance: PASS.** After fixing a
  native rc113 stderr variant (`Bad request.` followed by `Could not find
  service`), the same real two-signed-Core/one-owner opt-in test passed
  **1 passed / 0 failed**, and its **new** candidate dirs and scratch were
  cleaned. Production PIDs and start times remained unchanged; no native
  `SMAppService.register`/stop/unregister was invoked. The initial failed
  run is preserved as an incident, not silently rewritten. See
  [signed retry](v0250-macos-shared-presence-signed-preflight-retry-2026-11-15.md).
- [x] **Latest post-lifecycle-source regression:** root pytest **2,453 passed /
  417 skipped**, independent Agent Bridge pytest **538 passed / 134 skipped**,
  both projects' Ruff lint, Bridge Ruff format, two Swift typechecks and
  `git diff --check` passed. Live opt-ins were disabled. **Root Ruff format
  is NOT ACCEPTED**: `src/serverfs_mcp/darwin_service.py` has approximately
  11 pre-existing mechanical formatting hunks; all other 669 Python files
  already conform. No unrelated whole-file reformat was applied. See
  [post-presence source gate](v0250-macos-post-presence-full-source-regression-2026-11-15.md).
  Earlier [post-wheel-gate source regression](v0250-macos-wheel-gate-current-regression-2026-11-15.md)
  recorded 2,421 passed; [native-negative regression](v0250-macos-current-source-gates-after-native-negative-2026-11-15.md)
  2,408 passed; and [earlier source baseline](v0250-macos-current-head-source-gates-2026-11-15.md)
  2,372 passed. These are historical, not current, results.
- [x] Two additional locked offline-wheel Shared Core HTTP/Bridge v2 test nodes
  exited with **2 passed / 0 failed**; four protected production launchd PIDs
  remained unchanged. This is process-level E2E, not SMAppService registration,
  release-SHA runtime acceptance or proof of scratch cleanup.
- [x] **Offline isolated wheel build + static RECORD verification: PASS.**
  Standard `uv build --offline --wheel` (with PEP 517 build isolation)
  successfully built both root and Bridge wheel from the current unchanged
  dirty worktree. Static ZIP member, SHA-256/size RECORD and required macOS
  Swift/Shared Core and Bridge v2 module checks passed (113/54 members).
  Neither archive was extracted, imported, or executed. Both carry version
  **0.24.2**, not a v0.25.0 release SHA. Dedicated 0700 output directories
  remain and are intentionally not claimed cleaned. See
  [isolated wheel RECORD audit](v0250-macos-offline-isolated-wheel-record-2026-11-15.md).
- [x] **Replayable static wheel gate: PASS.** Added a stdlib-only
  `deployment/macos/v0250_wheel_archive_gate.py` CLI with 13 passing synthetic
  positive/negative tests, plus 21 passing existing wheel release tests.
  The CLI independently accepted both real **0.24.2** archives and correctly
  rejected them when `--expected-version 0.25.0` (exit code 2). It neither
  extracts nor executes wheel contents; see
  [post-wheel-gate source regression](v0250-macos-wheel-gate-current-regression-2026-11-15.md).
- [x] Documentation now distinguishes these results from native registered
  Shared Cores and public ingress. The 2026-11-14 historical sections 1–4 of
  the [Shared v2 gate](../architecture/v0.25.0-macos-shared-v2-integration-gates.md)
  remain intact.

## Incomplete or not accepted

- [ ] **Wheel packaging: STATIC PASS / NOT FULLY ACCEPTED.** The latest
  standard offline PEP 517 build and full static ZIP/RECORD verification passed
  for both archives. Extracted-wheel import/installation remains NOT VERIFIED:
  an earlier execution was cancelled after an outside-workdir approval was not
  granted. These dirty-tree **0.24.2** wheels are not release-matched v0.25.0
  artifacts. Static integrity does not establish runtime import integrity.
- [x] **Earlier static wheel build failure diagnosed and overcome.** The
  aborted `ModuleNotFoundError: hatchling` attempt lacked an importable
  backend in the build process; the standard offline *isolated* PEP 517 builder
  successfully resolved locally cached Hatchling without any network install
  or change to project `.venv`. The earlier failure remains historical
  evidence, not the latest wheel-build status.
- [ ] **Scratch cleanup: NOT VERIFIED** for the cancelled wheel tasks. The
  earlier aborted static-build attempt left the task-created
  `.serverfs-v0250-wheel-static-dUpY22im/root/.gitignore` under the repository;
  a proposed opaque cleanup command required extra approval and was cancelled.
  Read-only inventory confirmed the file, but did not authorize deletion.
  Do not infer cleanup from other diagnostics' independent temporary-directory
  checks. The subsequently validated wheel archives are deliberately retained
  in two separately named private output directories under `deployment/macos/`;
  their cleanup is NOT CLAIMED, and neither was published.
- [ ] **Failed signed-preflight residual cleanup: NOT VERIFIED.** The initial
  rc113-parser failure left test-owned candidate `shpf-a-0958931b80` and
  scratch `shpf-0958931b80-0cs3xq79`. Strict read-only provenance checks
  confirmed an inactive, unregistered Core and unchanged production services,
  but the full ancestor permissions gate was not established and a symlink
  exists inside scratch. Cleanup stopped **before any mutation**; both paths
  are preserved. See [residual safety audit](v0250-macos-signed-preflight-residual-safe-cleanup-2026-11-15.md).
- [ ] **Locked Shared HTTP E2E cleanup: NOT VERIFIED.** The two test nodes
  passed and no test Core/Bridge child was observed afterwards. Three residual
  `/private/tmp/sfsh3*` directories were independently dated several hours
  before this test, so they cannot be attributed to it, but their actual owners
  remain unidentified. They were retained rather than deleted. The specific
  task-created scratch roots were not independently traced to removal.
- [ ] **Shared native lifecycle:** no two Shared Cores have been registered
  and started as macOS Login Items. Registration, stop, restart/recovery and
  system approval behavior remain unaccepted.
- [ ] **Release-matched runtime:** the exact-HEAD offline install provenance is
  v0.24.2. A production-grade runtime tied to the eventual v0.25.0 release SHA
  is not verified.
- [ ] **Public ingress:** no v0.25.0 Cloudflare/ChatGPT MCP public E2E, session,
  SSE or recovery acceptance is recorded.
- [ ] **Cross-platform release gate:** no same-SHA Linux, Windows and macOS
  final regression/publication gate is complete. Root `pyproject.toml`,
  `agent_bridge/pyproject.toml`, and `plugin.json` all still declare `0.24.2`;
  this is current repository metadata evidence, not a release candidate.
- [ ] **Repository state:** the working tree contains extensive pre-existing
  source, test, diagnostic and deployment changes. This reconciliation adds
  no source/test/version changes and makes no commit, push or tag. Do not
  attribute the entire dirty tree to this documentation task.

## Exact next gates

0. Resolve the remaining **root Ruff format** gate by reviewing the existing
   `darwin_service.py` format-only hunks without altering Bridge semantics.
   Separately resolve ownership/ancestor/symlink blockers for the two known
   `shpf-*` test residuals before attempting any precise cleanup; do not
   weaken the conservative product unstage or bypass approval.
1. With offline isolated wheel build and static ZIP/RECORD checks already
   passed, complete the remaining **extracted-wheel import/installation** gate
   only under approved execution boundaries. Independently prove cleanup of
   the exact task-owned disposable paths; preserve any unidentified residual
   directories. Until then, static PASS is not full packaging acceptance.
2. Build a release-matched v0.25.0 runtime from the eventual exact release SHA
   using the locked offline requirements; verify provenance and isolated
   imports from installed site-packages.
3. Under a separately authorized isolated native acceptance, register two
   uniquely named signed Shared Cores against one owner-managed Bridge; verify
   independent start/stop, Core and owner restart behavior, grants/rotation/
   revocation, approval handling, and cleanup while proving default production
   services remain unchanged.
4. In a separately authorized isolated public environment, complete Cloudflare
   to ChatGPT MCP ingress, authentication, session/SSE and recovery checks.
5. Run the repository's exact-SHA Linux, Windows and macOS release gates,
   reconcile release-facing metadata/docs, and use the dispatch-first stable
   publication workflow only when explicitly authorized.

No credentials, secret values, or private credential paths are recorded here.
