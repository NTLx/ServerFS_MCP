# macOS v0.25.0 — shared lifecycle lock and wheel gates (2026-11-15)

## Scope

This is a current-state development evidence addendum. Existing historical diagnostics remain unchanged. No release, Git commit/push, production service registration, Tunnel change, credential change or public ingress operation was performed.

## Cooperative Shared Core start/unstage safety

The named Shared Core `unstage_shared_core()` now acquires the same private `services.json` advisory nonblocking `flock` used by `start_shared_core()`, before trust/registration checks, and holds it through exact candidate removal. After slow signed-App validation and final inactive-status verification, it checks that `services.json` still points to the locked private inode. Lock contention and inode mismatch fail closed as `DarwinSharedUnstageError`. This does not introduce Bridge ownership or grant rotation into Core operations.

Changed implementation: `src/serverfs_mcp/darwin_instance_shared_lifecycle.py`, `src/serverfs_mcp/darwin_instance_shared_unstage.py`; regression tests: `tests/test_darwin_v0250_shared_unstage.py` (plus existing lifecycle tests). Targeted and `tests/test_darwin*.py` regressions passed. **Residual:** only cooperative start/unstage calls are serialized; direct Manager calls and noncooperative processes remain outside the lock. No real Shared Core `SMAppService` Login Item registration has been accepted by this change.

### Post-change complete source gates

The independently executed verification-only gate reported:

| Check | Result |
| --- | --- |
| Root offline frozen Ruff lint | PASS (exit 0) |
| Root offline frozen Ruff format | PASS (672 formatted files; exit 0) |
| Root full pytest, cache disabled, opt-in live flags unset | **2455 passed, 417 skipped, 0 failed** (exit 0; 117.03 s) |
| Bridge Ruff lint and format | PASS (120 formatted files; exit 0) |
| Bridge full pytest, cache disabled | **538 passed, 134 skipped, 0 failed** (exit 0; 54.55 s) |
| Native Swift ServiceManager typecheck | PASS (exit 0) |
| Native Swift ServiceLauncher + SharedManifestGate typecheck | PASS (exit 0) |
| git diff --check | PASS (exit 0) |

Root pytest produced two non-failure warnings: Starlette/AnyIO deprecation and the synthetic duplicate ZIP member fixture. The verification task did not alter the worktree.

## Wheel import gate — still blocked

Existing offline-built wheels `serverfs_mcp-0.24.2-py3-none-any.whl` (113 archive members) and `serverfs_agent_bridge-0.24.2-py3-none-any.whl` (54 members) passed the static wheel archive gate again, including checks against the earlier recorded wheel identities. Full isolated offline installation failed during dependency resolution because the local cache lacks required `mcp==2.3.0`. No installed-wheel import or package origin path was verified. The experiment retained its own workspace scratch under `deployment/macos/.serverfs-v0250-wheel-import-uNINb4/`; unknown or older residuals were left untouched. Do not count static archive integrity or checkout imports as release-wheel runtime acceptance.

Both wheels still carry 0.24.2 metadata from a dirty checkout and are **not** release-matched v0.25.0 artifacts.

## Remaining native/release boundaries

1. Complete dependency-locked wheel installation and isolated module-origin/import verification; later repeat against exact v0.25.0 release SHA.
2. Perform isolated native `SMAppService` registration, start/stop and recovery of two signed Shared Cores against one separate owner, under explicit safe opt-in and without changing production jobs.
3. Perform Cloudflare/ChatGPT public MCP ingress, authentication, SSE/session and recovery checks in a separately isolated environment.
4. Reconcile Linux/macOS/Windows same-SHA release gates and release metadata before any authorized publication.

This record does not claim cleanup of known `shpf-*` residuals or any other unverified scratch state.
