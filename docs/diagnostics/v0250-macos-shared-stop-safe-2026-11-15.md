# macOS v0.25.0 Shared Core stop safety — 2026-11-15

## Finding

`stop_shared_core` previously treated every registration response other than
`not-registered`/`not-found` as permission to unregister. An unknown or malformed
manager response could therefore trigger a destructive lifecycle operation.

## Change

- Only `enabled` and `requires-approval` authorize explicit Core unregister.
- Status query errors and indeterminate statuses fail closed before unregister.
- The indeterminate-status error reports only the exact Core launchd label,
  PID evidence (or `unavailable`), and a normalized status type.
- An absent registration with a live Core PID is reported as inconsistent.
- After scoped unregister, the existing bounded Core stop wait runs before the
  terminal registration and PID checks. Only `not-registered`/`not-found` plus
  an absent Core PID is accepted as stopped.
- Operations remain scoped to the named Core's `http-origin` registration and
  `core` launchd label. No Bridge lifecycle or grants are consulted or changed.

## Verification

- `uv run pytest tests/test_darwin_v0250_shared_lifecycle.py` on final source:
  55 passed.
- `uv run ruff check` and `uv run ruff format --check` on the lifecycle module
  and its test: passed.
- `git diff --check` on the two Python files: passed.
- Root `uv run pytest` before the final invalid-response type guards: 2370
  passed, 417 skipped, one Starlette deprecation warning, in 125.29 seconds.
- After the final type guards, the targeted lifecycle suite passed again. A
  second root-suite run was stopped before completion and provides no result.
- Live acceptance environment flags were unset for pytest. These synthetic
  tests do not establish native macOS launchd behavior, ServiceManager behavior,
  approval UI behavior, or a real concurrent startup race.
