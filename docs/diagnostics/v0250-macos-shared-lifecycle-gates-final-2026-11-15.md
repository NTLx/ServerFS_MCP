# macOS v0.25.0 Shared lifecycle source gates — 2026-11-15

## Scope and disposition

This is a bounded, release-blocking source-quality gate for the current dirty
worktree after the Shared Core start/rollback and unstage recheck fixes. It is
not a release, deployment, native-registration, or public-ingress acceptance.
All source-quality gates below passed. This verification added only this audit
record; it made no source or Git changes.

## Environment and validation

The seven repository `SERVERFS_ENABLE_*` opt-in flags were explicitly unset in
every test, lint, format, and Swift compiler subprocess. Root commands used the
existing frozen environment with offline mode; Agent Bridge used its existing
virtual environment without sync. No external network, service action,
Cloudflare/OpenAI ingress, restart, or production permission was used.

From the repository root:

```text
uv run --offline --frozen ruff check .                         PASS
uv run --offline --frozen ruff format --check .                PASS (662 files already formatted)
uv run --offline --frozen pytest -p no:cacheprovider -o addopts='' -q
  PASS: 2408 passed, 417 skipped, 0 failed, 1 warning (115.95s)
```

The warning is the installed Starlette test client's `anyio.abc.BlockingPortal`
deprecation warning.

From `agent_bridge/`, using its existing `.venv`:

```text
.venv/bin/ruff check .                                         PASS
.venv/bin/ruff format --check .                                PASS (120 files already formatted)
.venv/bin/pytest -p no:cacheprovider -o addopts='' -q
  PASS: 538 passed, 134 skipped, 0 failed (51.78s)
```

Installed Xcode toolchain typechecks passed for arm64 macOS 27.0 without a
build or ServiceManagement invocation:

```text
xcrun swiftc -typecheck -target arm64-apple-macosx27.0 \
  native/macos/service_manager/Sources/ServiceManager/main.swift           PASS
xcrun swiftc -typecheck -target arm64-apple-macosx27.0 \
  native/macos/service_manager/Sources/ServiceLauncher/main.swift \
  native/macos/service_manager/Sources/ServiceLauncher/SharedManifestGate.swift PASS
```

`git diff --check` passed. The two untracked source/test pairs for Shared Core
lifecycle and unstage were independently checked for trailing whitespace and
final newlines; all four files were clean. The requested audit record exists.

## Fixes and test coverage

- Shared Core start rollback is scoped to the newly registered Core. After
  unregistering, it waits for that Core to stop, then checks terminal
  ServiceManager status and the exact Core launchd PID; any uncertain result
  fails closed. Synthetic lifecycle tests cover readiness failure, delayed
  process exit, terminal-state verification, and rollback failure. This does
  not make the unregister/wait/status sequence atomic against another process.
- Shared Core unstage rechecks registration status and the exact Core PID after
  signed-app validation, immediately before deleting the pristine staged
  files. Synthetic tests cover registration/PID drift during validation and
  malformed or unavailable state. A cross-process lifecycle can still race
  after the final check; the checks narrow that window but cannot eliminate it.

## Worktree and release limits

The stable HEAD remains
`da9c12e600d3b336f1fd7a3d5232702e9abbc19f`
(`feat(macos): checkpoint v0.25 independent Core and Cloudflare ingress`),
with project metadata still at v0.24.2. The worktree was already extensively
dirty before this gate and remains dirty; this report preserves that state.
No source was modified, and no commit, push, tag, or publication was made.

The following remain release blockers: native acceptance of two independent
Shared Core `SMAppService` registrations; a runtime/package tied to the eventual
v0.25.0 release SHA; v0.25.0 public Cloudflare-to-ChatGPT ingress acceptance;
and isolated wheel import plus task-owned cleanup acceptance. These limits do
not reopen or replace earlier, separately scoped acceptance that was already
closed; passing this source gate does not satisfy those outstanding gates.

The full suites here ran only synthetic and ordinary unit tests. Optional live
tests were skipped; there were no real native service tests or registrations,
no Cloudflare/OpenAI ingress, no service restart, and no production permission
changes. This is source-quality evidence for the dirty current worktree only.
