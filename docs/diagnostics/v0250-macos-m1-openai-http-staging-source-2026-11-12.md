# ServerFS v0.25.0 — macOS M1 OpenAI direct HTTP Connector source checkpoint (2026-11-12)

**Status:** OpenAI direct HTTP Connector staged-source candidate implemented; installed-Mac and public E2E **NOT VERIFIED**. No release, no production cutover.

## Implementation scope and ownership

- The already staged `http-origin` is the **sole independent MCP Core**, loaded from one canonical native `serverfs.toml`. The independent Agent Bridge remains an app-owned, separate LaunchAgent. Cloudflare is staged as a separate connector using `connector_owner=independent-connector`.
- New `src/serverfs_mcp/darwin_service.py` `stage_openai_http_connector` adds an **unregistered manifest-only** entry using the existing app-associated `tunnel` LaunchAgent identity, marked `connector_owner=independent-openai-http`. Its executable is the fixed current-user tunnel-client binary; arguments use `--mcp.server-url http://127.0.0.1:<core-port>/mcp`. There is **no** `--mcp.command` or supervisor process and no Core/Bridge process ownership inside Tunnel.
- The OpenAI runtime key uses `--control-plane.api-key file:/private/path`. A separate private file holds exactly `Bearer <Core API Key>` for `--mcp.extra-headers` and `--mcp.discovery-extra-headers`. Distinct files, secret equality rejected; both files mode 0600, current-user-owned, without symlinks, outside repo and all exposed workdirs. Neither value is placed in argv, manifest or logs. OAuth-only Core configuration currently fails closed.
- Native manifest must contain an independently owned Core with canonical TOML and exact two-authority public+loopback Host setting. OpenAI stage refuses any existing, registered or process-active legacy Tunnel and any unmanaged Tunnel PID before publishing. `register_service` also rechecks unmanaged Tunnel ownership at start time; stage alone never starts a service.
- Uses `--health.listen-addr 127.0.0.1:0` to avoid the tunnel-client default health-port collision.
- `unstage_openai_http_connector` only removes a marked, unregistered/non-running OpenAI HTTP manifest entry, preserving both source credential files and the persistent Core. Core unstage refuses while an independent OpenAI Connector entry exists.
- `src/serverfs_mcp/cli.py` adds `serverfs service openai {stage|unstage}`, macOS-only. Existing `serverfs service {start|stop|restart} --component tunnel` is the explicit OS lifecycle; this does not alter Windows CLI semantics.
- `tests/test_darwin_v0250_openai_http_staging.py` covers required exact flags, no stdio/supervisor, dual-Connector/single-Core coexistence, wrong or exposed secrets, registered/unmanaged/duplicate refusal, ownership gates, Core unstage denial and CLI macOS-only dispatch.
- `deployment/macos/README.md` updated with isolated-stage-only examples, exact private file requirements and explicit Mac M2 gates.

## Authoritative compatibility basis and limits

- Windows v0.25.0 ADR and W0 local installed tunnel-client `0.0.17` verified HTTP-to-Core feasibility via `--mcp.server-url` under disposable setup.
- Current upstream reference `https://github.com/openai/tunnel-client/blob/master/docs/configuration.md` documents HTTP binding, `file:`-backed MCP extra/discovery header values, and environment precedence; `https://github.com/openai/tunnel-client/blob/master/docs/connectors.md` describes connector POST streaming.
- This is **not** evidence the actual installed Mac binary accepts all flags or performs successful real authentication; no Mac client help/doctor/protocol test, public ChatGPT MCP call, SSE >60s, Agent/Repo test, isolated service start/restart, native wheel member check, production migration or rollback was performed.
- Existing legacy generic service group operations and historical Cloudflare pair commands are still compatibility paths; do **not** use them against independent Core/Bridge. Explicit migration and failure-safe remapping remain open.
- No Git commit/push/tag, installed app replacement, Login Items change, runtime service stop/restart, credential rotation, DNS/Tunnel mutation or production workdir mutation occurred.

## Verification

Initial focused OpenAI suite after first source increment reported **112 passed, 7 failed, 1 skipped**. The 7 failures were introduced by `_register_and_wait` using an optional manifest argument incompatible with zero-argument test stubs. The source was narrowed to the existing required manifest interface, preserving unmanaged Tunnel refusal. The new test file needed mechanical Ruff formatting. Final post-fix **focused pytest 119 passed, 1 skipped, 0 failed**, exit 0. Full-repository Ruff lint and format checks PASS (527 files formatted), `git diff --check` PASS, all exit 0. Final post-fix complete Mac root `uv run --no-sync pytest`: **1834 passed, 408 skipped, 0 failed, 1 warning**, exit 0. The one warning is the existing Starlette/anyio BlockingPortal deprecation. Full `ruff check .`, `ruff format --check .` (527 files) and `git diff --check` all exit 0. No installed-Client binary or public runtime acceptance is implied. A separate read-only installed-client help probe was attempted afterward, but its single Python command failed syntax parsing before locating/running tunnel-client. No binary capability was established by that attempt; no credentials or service operations occurred.

Next gates: focused/full Python root and Bridge suites where applicable, correct-prefix wheel member inspection, installed-Mac tunnel-client direct HTTP startup and protected `file:` resolution, independent SMAppService Core/Connector/Bridge runtime and fault isolation, public authenticated Cloudflare+OpenAI writable/Agent/Repo/SSE, no-loss historical migration and rollback, Linux/Windows integration and single v0.25.0 release acceptance.
