# macOS v0.25.0 current source gates after signed Launcher negative preflight — 2026-11-15

## Scope and result

The current dirty worktree passed this bounded, verification-only source gate
after the opt-in signed Launcher negative preflight edits. This record is the
only file added by this gate. No source, test, dependency, build, wheel, or
native registration was changed by the gate. No commit, push, tag, or release
publication was made.

The separately accepted signed Launcher negative preflight completed with
**1 passed, 0 failed** under its explicit opt-in. It preserved the protected
native PID baseline and left no test candidate residual; details are in
[`v0250-macos-signed-launcher-negative-preflight-2026-11-15.md`](v0250-macos-signed-launcher-negative-preflight-2026-11-15.md).
All commands in the source gates below explicitly unset the seven
`SERVERFS_ENABLE_*` opt-in flags, so the source gate did not enable native
registration, live tests, or HTTP ingress.

## Executed gates

From the repository root, using the existing frozen environment offline. The
following `env -u` prefix was prepended to each command shown below:

```text
env -u SERVERFS_ENABLE_EPHEMERAL_SIGNED_CORE_TEST \
  -u SERVERFS_ENABLE_SHARED_HTTP_E2E \
  -u SERVERFS_ENABLE_SHARED_LOCKED_HTTP_E2E \
  -u SERVERFS_ENABLE_SHARED_LOCKED_OWNER_E2E \
  -u SERVERFS_ENABLE_SHARED_RUNTIME_INSTALL_TEST \
  -u SERVERFS_ENABLE_SHARED_SIGNED_LIVE_PREFLIGHT_TEST \
  -u SERVERFS_ENABLE_SHARED_SMAPP_LIVE_TEST
```

```text
uv run --offline --frozen ruff check .                         PASS
uv run --offline --frozen ruff format --check .                PASS (664 files already formatted)
uv run --offline --frozen pytest -p no:cacheprovider -o addopts='' -q
  PASS: 2408 passed, 417 skipped, 0 failed, 1 warning (120.62s)
```

The one warning is the installed Starlette test client's `anyio.abc.BlockingPortal`
deprecation warning.

From `agent_bridge/`, using its existing virtual environment without sync and
with the same `env -u` prefix:

```text
.venv/bin/ruff check .                                         PASS
.venv/bin/ruff format --check .                                PASS (120 files already formatted)
.venv/bin/pytest -p no:cacheprovider -o addopts='' -q
  PASS: 538 passed, 134 skipped, 0 failed (53.82s)
```

The seven flags explicitly unset for every command above were
`SERVERFS_ENABLE_EPHEMERAL_SIGNED_CORE_TEST`,
`SERVERFS_ENABLE_SHARED_HTTP_E2E`,
`SERVERFS_ENABLE_SHARED_LOCKED_HTTP_E2E`,
`SERVERFS_ENABLE_SHARED_LOCKED_OWNER_E2E`,
`SERVERFS_ENABLE_SHARED_RUNTIME_INSTALL_TEST`,
`SERVERFS_ENABLE_SHARED_SIGNED_LIVE_PREFLIGHT_TEST`, and
`SERVERFS_ENABLE_SHARED_SMAPP_LIVE_TEST`.

Installed Xcode tools passed typechecking for arm64 macOS 27.0, with the same
`env -u` prefix. These were typecheck-only commands; no build or
ServiceManagement API was invoked:

```text
xcrun swiftc -typecheck -target arm64-apple-macosx27.0 \
  native/macos/service_manager/Sources/ServiceManager/main.swift           PASS
xcrun swiftc -typecheck -target arm64-apple-macosx27.0 \
  native/macos/service_manager/Sources/ServiceLauncher/main.swift \
  native/macos/service_manager/Sources/ServiceLauncher/SharedManifestGate.swift PASS
```

`git diff --check` passed. The untracked
`tests/test_darwin_v0250_shared_native_live_preflight_macos.py` was manually
inspected around the negative mutation and restoration logic; a full-file
whitespace scan found zero trailing-whitespace lines and confirmed a final
newline.

## Worktree and remaining gates

HEAD remains `da9c12e600d3b336f1fd7a3d5232702e9abbc19f` (v0.24.2), and the
worktree remains extensively dirty. The earlier static wheel task failed with
`ModuleNotFoundError: hatchling` and was cancelled; no dependency was installed
and no build was retried. The observed
`.serverfs-v0250-wheel-static-dUpY22im/root/.gitignore` scratch artifact remains
untouched, and its cleanup is **NOT VERIFIED**.

This source gate does not close the remaining release blockers: native
registration and acceptance of two Shared Cores; v0.25.0 public ingress;
isolated extracted-wheel import acceptance; and the exact-SHA cross-platform
release gate. No claim is made that two Cores were natively registered or that
v0.25.0 was published.
