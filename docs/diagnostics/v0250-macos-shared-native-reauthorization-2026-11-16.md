# ServerFS MCP v0.25.0 — macOS Shared Core re-authorization acceptance

**Date:** 2026-11-16, macOS local. **Status: PASS** for this isolated native acceptance scenario on the current v0.25.0 candidate; this is not a tagged release.

## Evidence

The real `SMAppService` test `tests.test_darwin_v0250_shared_smappservice_live_macos::test_two_real_smappservice_shared_cores_keep_owner_alive` passed once after adding a test-only revoke-then-restore of the original two Core grants. The run reported pytest exit 0 and 58.296 seconds. Its JUnit suite independently records `tests=1`, `failures=0`, `errors=0`, `skipped=0`, timestamp `2026-11-16T09:39:59.554327+08:00`; no stdout/stderr capture was present. The JUnit file was verified as a current-UID-owned regular non-symlink with no detected secret markers, then deleted by its exact path after verification.

The re-authorization behavior re-grants the **original keys** after the test-only revocation; it does not test key rotation. The run also reported 20 targeted tests passed with 1 default skip, Ruff and diff checks passed, all four protected production PIDs unchanged, and no remaining `shsm` scratch or jobs.

## Boundary

This is isolated acceptance evidence only. No production Agent Bridge was modified. It does not establish key rotation, logout/login or reboot recovery, public ingress acceptance, cross-platform acceptance, or v0.25.0 publication.
