# macOS v0.25.0 post-presence full source regression — 2026-11-15

## Scope and baseline

Verification-only regression of the current working tree after the Shared Core
launchd job parser fix, unstage guard, cooperative same-Core start lock,
preflight/rollback/stop terminal presence checks, and the accepted signed
two-Core non-registering preflight. No source or test files were edited. The
only file added by this verification is this report.

The repository was at HEAD `da9c12e` (`0.24.2`) with a heavily dirty working
tree containing pre-existing tracked and untracked v0.25.0 source, tests,
deployment material, architecture notes, and diagnostics. Root and Bridge
`pyproject.toml` both still declare `0.24.2`. No commit, push, tag, or release
operation was performed.

All seven live opt-in variables were unset before testing:
`SERVERFS_ENABLE_EPHEMERAL_SIGNED_CORE_TEST`,
`SERVERFS_ENABLE_SHARED_HTTP_E2E`,
`SERVERFS_ENABLE_SHARED_LOCKED_HTTP_E2E`,
`SERVERFS_ENABLE_SHARED_LOCKED_OWNER_E2E`,
`SERVERFS_ENABLE_SHARED_SIGNED_LIVE_PREFLIGHT_TEST`,
`SERVERFS_ENABLE_SHARED_RUNTIME_INSTALL_TEST`, and
`SERVERFS_ENABLE_SHARED_SMAPP_LIVE_TEST`.

No actual `SMAppService` or Tunnel operation, internet access, dependency
installation/synchronization, wheel extraction, or cleanup/deletion was
performed. Existing opt-in tests remained gated/skipped.

## Validation results

| Boundary | Command | Result |
| --- | --- | --- |
| Root Ruff lint | `uv run --offline --frozen ruff check .` | PASS — `All checks passed!` |
| Root Ruff format | `uv run --offline --frozen ruff format --check .` | **FAIL** — one existing offender, `src/serverfs_mcp/darwin_service.py`; 669 files already formatted and approximately 11 formatting hunks reported. The formatter was not run. |
| Root full pytest | `uv run --offline --frozen pytest -p no:cacheprovider -o addopts='' -q` | PASS — exact summary: `2453 passed, 417 skipped, 2 warnings in 129.47s (0:02:09)` |
| Bridge Ruff lint | `agent_bridge/.venv/bin/ruff check .` (run from `agent_bridge/`) | PASS — `All checks passed!` |
| Bridge Ruff format | `agent_bridge/.venv/bin/ruff format --check .` (run from `agent_bridge/`) | PASS — `120 files already formatted` |
| Bridge full pytest | `agent_bridge/.venv/bin/pytest -p no:cacheprovider -o addopts='' -q` (run from `agent_bridge/`) | PASS — exact summary: `538 passed, 134 skipped in 59.76s` |
| Swift ServiceManager | `xcrun swiftc -typecheck -target arm64-apple-macosx27.0 native/macos/service_manager/Sources/ServiceManager/main.swift` | PASS — exit 0 |
| Swift ServiceLauncher + SharedManifestGate | `xcrun swiftc -typecheck -target arm64-apple-macosx27.0 native/macos/service_manager/Sources/ServiceLauncher/main.swift native/macos/service_manager/Sources/ServiceLauncher/SharedManifestGate.swift` | PASS — exit 0 |
| Tracked diff whitespace | `git diff --check` | PASS — exit 0 |
| Untracked shared lifecycle/unstage files | Manual trailing-whitespace and final-newline scan of `darwin_instance_shared_lifecycle.py`, `darwin_instance_shared_unstage.py`, `test_darwin_v0250_shared_lifecycle.py`, and `test_darwin_v0250_shared_unstage.py` | PASS — no trailing whitespace or missing final newline |

The root pytest warnings were a Starlette test-client deprecation warning for
`anyio.abc.BlockingPortal` and a `zipfile` duplicate-member `UserWarning` from
`tests/test_darwin_v0250_wheel_archive_gate.py::test_duplicate_member_fails`.

**Overall source verification: PARTIAL, not all green.** Both full test suites,
both Swift typechecks, both Bridge Ruff gates, root Ruff lint, and whitespace
checks passed. Root Ruff format did not pass because of the existing
`darwin_service.py` formatting differences. No formatter was run and no source
or test correction was made.

## Operational acceptance boundaries

The latest signed-preflight retry record reports one passing test in 100.70
seconds for two real signed Shared Core candidates using Launcher
`--validate-only` and Manager `register-preflight` against one synthetic
Bridge owner, without native registration. It is preflight evidence only.
That record also identifies unresolved earlier residuals
`~/Library/Application Support/ServerFS/instances/shpf-a-0958931b80` and
`/tmp/shpf-0958931b80-0cs3xq79`; this verification did not inspect or remove
them.

The existing release-readiness record says an extracted-wheel import attempt
was cancelled when outside-workdir approval was not granted, and a proposed
opaque scratch-cleanup command also required extra approval and was cancelled.
Those constraints remain unresolved. This report does not claim acceptance for
real `SMAppService` registration/Login Items approval or recovery, public
ChatGPT/Cloudflare E2E, an exact-release-SHA runtime, extracted-wheel import or
installation, cleanup, or a v0.25.0 release. Current stable/repository version
metadata remains v0.24.2.
