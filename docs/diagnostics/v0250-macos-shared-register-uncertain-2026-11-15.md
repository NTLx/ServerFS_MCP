# macOS Shared Core uncertain registration lifecycle fix — 2026-11-15

## Scope

This source-only change hardens `start_shared_core()` when ServiceManager
registration has an uncertain outcome. A thrown register call is followed only
by read-only inspection of the exact named `http-origin` registration and Core
launchd label. Because status and PID cannot establish which concurrent caller
created a registration, the lifecycle preserves all state and requires operator
inspection; it never unregisters on this exception path.

When registration returns explicitly, `requires-approval` and readiness failure
may roll back only this instance's `http-origin`. Rollback verifies the manager
returned `not-registered` or `not-found` before waiting for the Core to stop.
The shared Bridge, grants, peer Cores and default service are not touched.

## Synthetic evidence

- `tests/test_darwin_v0250_shared_lifecycle.py` covers preflight, successful
  startup, register exceptions before/after simulated effects, concurrent
  registration state, failed status/PID inspection, approval denial, readiness
  failure, rollback failures, signed identity and Bridge-scoped operation checks.
- Focused and adjacent tests are synthetic; no real SMAppService registration
  or launchd mutation was invoked.
- Validation results are recorded in the accompanying task report; no live
  macOS acceptance is claimed.
