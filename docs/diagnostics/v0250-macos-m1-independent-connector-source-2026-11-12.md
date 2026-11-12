# ServerFS v0.25.0 — macOS M1 Connector separation checkpoint (2026-11-12)

**Status: independent Cloudflare Connector staging candidate implemented; native/public E2E and migration NOT ACCEPTED.** This is a source-only milestone. No production service was installed, registered, started, stopped, restarted, uninstalled or migrated.

## Scope and controlled changes

Continuation of `docs/diagnostics/v0250-macos-m1-core-staging-source-2026-11-12.md`. Retains existing uncommitted v0.24.3 Cloudflare writable-workdir candidate and does not overwrite Windows concurrent work.

- `src/serverfs_mcp/darwin_service.py`: extracted reusable `_cloudflared_spec` for exact private current-user `0600` token file, no symlinked path components, credential exclusion from the checkout and all exposed workdirs, and owner-protected executable. The old paired Cloudflare installer continues using the shared validator.
- New `stage_cloudflare_connector(config_path, cloudflared, token_file, hostname)` accepts only an independently owned Core manifest `origin_owner=independent-core`, exact valid Core Streamable HTTP port, same absolute canonical TOML, matching dual public+loopback Host snapshot and valid API Key/OAuth, and safe workdir roots. Refuses existing, registered or still-running Cloudflare Connector; checks for unmanaged cloudflared. Writes only a mode-0600 manifest entry `connector_owner=independent-connector` and `core_port`, without ever registering a LaunchAgent or writing a second HTTP-origin spec.
- New `unstage_cloudflare_connector()` refuses legacy/unmarked or registered/running Connectors. It atomically removes only the independently owned Connector manifest entry and preserves Core, credentials and operator config.
- `src/serverfs_mcp/cli.py`: `serverfs service cloudflare stage --config ... --cloudflared ... --token-file ... --hostname ...` and `service cloudflare unstage`, macOS-only; Windows rejects these new stage commands explicitly and keeps its existing command behavior.
- `tests/test_darwin_v0250_connector_staging.py`: negative and positive unit regression for stage-only behavior, marker/port/config/Host, refused duplicate/registered/legacy/active instances, insecure/exposed credentials, independent unstage, legacy pair stop fail-closed and platform-aware CLI dispatch.
- `deployment/macos/README.md`: explicitly labeled **unreleased source-only** instructions and warnings; no production migration authorization.

## Architecture and evidence

The target topology remains two independently supervised ingress connectors, one persistent authenticated loopback HTTP Core owning sessions and the single canonical workdir TOML, and an independent Agent Bridge owning providers/lease. Unlike the legacy Cloudflare pair, **Connector staging does not own or spawn the Core**.

Windows W0 ADR confirmed installed tunnel-client support for `--mcp.server-url` and private reference-backed extra headers; its direct HTTP feasibility was locally proven there, but no Mac hosted/OpenAI evidence exists yet. The upstream docs at `https://github.com/openai/tunnel-client/blob/master/docs/configuration.md` and `https://github.com/openai/tunnel-client/blob/master/docs/connectors.md` describe the HTTP endpoint and header-file behavior. These external docs do not replace installed-mac-client verification. The planned Mac OpenAI connector needs a separately tested direct Streamable HTTP mode without `--mcp.command`, independent Core/Bridge ownership and a protected backend HTTP credential.

## Tests

- Initial focused Mac unit set before autoformat: **108 passed, 1 skipped**, exit 0; Ruff lint PASS and `git diff --check` PASS. The new test file required mechanical Ruff format; corrected in a narrowly scoped single-file command, formatting check exit 0.
- **Final macOS full root** `uv run --no-sync pytest`: exit **0**, **1823 passed, 408 skipped, 0 failed, 1 warning** (existing Starlette/anyio BlockingPortal deprecation); 70.78 seconds.
- `uv run --no-sync ruff check .`: exit **0**, all checks passed.
- `uv run --no-sync ruff format --check .`: exit **0**, 525 files formatted.
- `git diff --check`: exit **0**, no whitespace errors.
- No native public runtime/process verification was performed in this checkpoint; the skip count does not replace M2 acceptance.

## Open gates

- Real Mac native ServiceManager and exact wheel member verification (earlier app build/codesign/LaunchAgent plists passed, wheel contents were checked against wrong archive prefix).
- Explicit independent OpenAI HTTP connector lifecycle/credential handling; direct-Mac installed binary protocol/readiness/safety evidence; no conflation of static backend auth and per-caller auth.
- Controlled migration of live legacy Cloudflare pair to independent Core/Connector, with out-of-band operator authority, rollback journal and public E2E; no in-band Bridge self-termination.
- Remove or explicitly fail closed ambiguous old `service stop/restart/uninstall` semantics after isolation is accepted.
- Full root+Agent Bridge tests where relevant, Mac Cloudflare/OpenAI public write/read/revision/denied path/Agent/Repo/SSE/fault injection/rollback acceptance and Windows/Linux final shared-source integration.
- Do not tag/push/release v0.25.0 until all three platforms pass in one release.
