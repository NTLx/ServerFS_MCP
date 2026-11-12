# ServerFS v0.25.0 — macOS parallel M0/M1 checkpoint (2026-11-12)

Status: **Initial source increment complete; full M1/M2 not accepted; unreleased.**

## Baseline and scope
- User approved Windows W2/W3 + macOS M0/M1 parallel development. WorkPC's canonical `dev_plan_v0.25.0.md` was updated accordingly. Shared Core/Bridge/HTTP/security contracts and one three-platform release gate remain mandatory.
- Mac `main` and *locally known* `origin/main`: `5e952cb9f5b787670a0d11fecea2b69720f16083`. No remote fetch or pull; current GitHub freshness not asserted.
- Original uncommitted v0.24.3 macOS Cloudflare writable-workdir feature, its tests, documentation and phase-C reports were preserved. No destructive Git actions, commits, pushes or releases.

## Direct MCP source changes
1. `src/serverfs_mcp/darwin_service.py`: added `cloudflare_connector_start/stop/restart`; only `cloudflare` LaunchAgent is affected, never HTTP Origin, Tunnel or Agent Bridge.
2. The existing `http-origin` LaunchAgent is temporarily reused as a **persistent Core service identity** (no duplicated HTTP process). New `core_start/stop/restart` perform a minimal manifest argument/port preflight and act only on `http-origin`.
3. `src/serverfs_mcp/cli.py`: added macOS opt-in `serverfs service {start|stop|restart} --component {tunnel|cloudflare|core}`. The same option explicitly fails closed on Windows.
4. New deterministic tests: `tests/test_darwin_v0250_connector_lifecycle.py`, `tests/test_darwin_v0250_core_lifecycle.py`, covering strict component dispatch and malformed/absent Core manifest denial.

**Compatibility caveat:** Legacy commands without `--component`, including `serverfs service cloudflare stop/restart`, still have historical coupled behavior. New M1 APIs are source-level only and must not be confused with completed production lifecycle migration. The Core install pipeline is still paired with Cloudflare and the Mac production Cloudflare origin still uses the old read-only TOML.

## Verification evidence
- `uv run --no-sync pytest tests/test_darwin_v0250_connector_lifecycle.py tests/test_darwin_v0250_core_lifecycle.py tests/test_darwin_service_lifecycle.py tests/test_darwin_cloudflare_service.py`: **76 passed, 1 skipped**, exit 0.
- `uv run --no-sync ruff check` over two edited sources and two new tests: **PASS**, exit 0.
- `git diff --check`: **PASS**, exit 0.
- `uv run --no-sync ruff format --check` over the same four files: **PASS**, exit 0 (`4 files already formatted`); the initial two layout-only issues in the new Core test file were corrected directly before the final check.

## Unchanged production and next gates
- No native install/uninstall, registration, start/stop/restart, Cloudflare/DNS change, credential read/rotation, public write operation, Agent handover or production switch was executed.
- Remaining M1: independent Core installer and canonical TOML/auth migration; replace/refuse ambiguous legacy grouped lifecycle; OpenAI direct HTTP-to-Core; Bridge policy/recovery and file-only availability guarantees; security and rollback tests; full root + Bridge suites.
- Remaining M2: genuine Mac service/process fault isolation, public authenticated Cloudflare and OpenAI HTTP end-to-end, writable/Agent/Repo/binary/Host/SSE and rollback validation. Windows/Linux live acceptance plus final-SHA CI remain required before one v0.25.0 release.
