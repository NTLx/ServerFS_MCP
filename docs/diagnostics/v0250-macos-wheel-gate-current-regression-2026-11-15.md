# macOS v0.25.0 root and Bridge source regression after static wheel gate — 2026-11-15

## Scope and provenance

Verification-only regression for the new static wheel archive CLI and its negative tests. No product source or test files were edited, no dependencies were synchronized or installed, no wheel was rebuilt, and no wheel was extracted, imported, or executed. No network, service registration, production operation, Windows operation, Swift build, Git commit, push, tag, or release publication was performed. Existing scratch and both wheel output directories were left intact.

The checkout was already extensively dirty at `da9c12e` (`da9c12e600d3b336f1fd7a3d5232702e9abbc19f`). Root and Bridge project metadata remain **0.24.2**. This regression does not establish a v0.25.0 release artifact or exact-SHA acceptance. The seven `SERVERFS_ENABLE_*` opt-ins were explicitly unset for every Python validation process.

## Results

| Gate | Result |
| --- | --- |
| Root `uv run --offline --frozen ruff check .` | PASS |
| Root `uv run --offline --frozen ruff format --check .` | PASS, 668 files already formatted |
| Root `uv run --offline --frozen pytest -p no:cacheprovider -o addopts='' -q` | **2,421 passed, 417 skipped, 0 failed**, 116.92 s |
| Bridge existing `.venv/bin/ruff check .` | PASS |
| Bridge existing `.venv/bin/ruff format --check .` | PASS, 120 files already formatted |
| Bridge existing `.venv/bin/pytest -p no:cacheprovider -o addopts='' -q` | **538 passed, 134 skipped, 0 failed**, 54.89 s |
| `ServiceManager/main.swift` arm64 macOS 27 typecheck | PASS |
| `ServiceLauncher/main.swift` + `SharedManifestGate.swift` arm64 macOS 27 typecheck | PASS |
| `git diff --check` | PASS |
| Standalone whitespace scan of the two new Python files | PASS |

The full suites emitted two warnings: Starlette's `anyio.abc.BlockingPortal` deprecation and the intentional duplicate ZIP member created by `test_duplicate_member_fails`.

## Existing wheel archive CLI

The CLI read the already-built archives under the existing two output directories. It inspected ZIP members and RECORD hashes/sizes without extraction, import, or execution.

- Expected version `0.24.2`: **PASS**. Root wheel: 113 members, 1,439,364 uncompressed bytes, SHA-256 `de6ea0f188a608d3bc0e02b89a9e8716ecf5d37f490220c61536cedba9a06d58`. Bridge wheel: 54 members, 767,102 uncompressed bytes, SHA-256 `19e06bf9de5c2a298335eeb8fb1affdd1acd0319438efd9fe757e28a96ee199f`.
- Expected version `0.25.0` against those same wheels: **expected rejection confirmed**, exit code 2, `distribution metadata path mismatch`. This confirms these 0.24.2 archives are not accepted as 0.25.0 wheels.

## Safety and remaining gates

The prior separate test evidence recorded four protected production PIDs unchanged. That is historical evidence from those separate tests, not a new process observation or claim in this regression.

Still **NOT VERIFIED**: registering two Shared Cores through native `SMAppService`; public ChatGPT/ingress E2E; extracted-wheel import or installation; cleanup of prior scratch and the retained wheel output directories; and cross-platform exact-SHA release gates. The existing static archive evidence and this source regression do not satisfy those gates.

This report is the only file added by this regression. The pre-existing dirty worktree remains otherwise untouched.
