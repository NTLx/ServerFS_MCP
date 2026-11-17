# macOS v0.25.0 Final Source Quality Gates — 2026-11-15

## Scope

The final source quality gate permitted Ruff-compatible mechanical formatting only in these three files:

- `src/serverfs_mcp/darwin_instance_shared_lifecycle.py`
- `tests/test_darwin_v0250_shared_lifecycle.py`
- `docs/diagnostics/v0250-macos-exact-commit-real-offline-install-2026-11-15.md`

Ruff formatting changed only whitespace, line wrapping, and parentheses, including Python in the Markdown code fence. The two E501 findings were resolved by those line wraps. No behavior, string contents, assertions, historical evidence/data, or other pre-existing dirty changes were intentionally changed. No files outside the three authorized paths and this requested report were edited by this gate.

## Executed checks

- `uv run --offline --frozen ruff check .` — passed.
- `uv run --offline --frozen ruff format --check .` — passed; 658 files already formatted, including Markdown snippets.
- `git diff --check` — passed.
- Targeted synthetic Shared lifecycle, runtime verification, and staging tests — 85 passed.
- Root safe pytest with `SERVERFS_ENABLE_*` opt-in flags unset — 2,356 passed, 417 skipped, 1 warning in 112.94 seconds. The warning is Starlette's `anyio.abc.BlockingPortal` deprecation warning.

## Remaining acceptance gates

Native registered macOS `SMAppService` end-to-end acceptance remains outstanding. Cloudflare end-to-end acceptance also remains outstanding. These checks were not run as part of this source quality gate; no installed runtime, production registration, `launchctl`, or Windows/Agent Bridge state was modified.
