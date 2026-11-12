# ServerFS v0.25.0 — macOS M1 native capability, packaging and safety-gate checkpoint (2026-11-12)

**Status:** macOS M1 source hardening and native tunnel-client CLI/wheel-contents verification PASS. **Mac real HTTP↔dev-proxy functional smoke and M2 native/public acceptance remain NOT VERIFIED.** No publication, production service migration, credential rotation or Git commit/push/tag was made.

## Verified evidence on the actual macOS host

- Host's bootstrapped `tunnel-client --version`: `0.0.17+a248b7f725fc989c1eb26680cf4351ee33421f61` (exit 0). `run --help` exit 0 and explicitly advertises `--mcp.server-url`, `--mcp.extra-headers`, `--mcp.discovery-extra-headers`, `--health.listen-addr`, `--control-plane.api-key`.
- Installed binary help describes `--mcp.server-url` as repeatable `url=...,channel=...` syntax, and runtime/discovery extra headers as repeatable `Key: Value` with `env:` or `file:` value references. Mac product stage argument was corrected from bare HTTP URL to `url=http://127.0.0.1:<port>/mcp`; its deterministic test asserts the exact new value.
- `dev proxy --help` exit 0 confirms a local in-memory control plane and `--mcp-server-url` for an isolated fixture. **Help is not a functional handshake/auth result.**
- A newly built Mac wheel passed strict ZIP membership tests using the correct wheel-internal prefix `serverfs_mcp/_darwin_service_manager/`. All eight mandatory entries exist: ServiceManager main.swift, ServiceLauncher main.swift, Info.plist, build.sh, and the four LaunchAgent plist files for tunnel/bridge/http-origin/cloudflare. Build exit 0. Wheel SHA256 `24d45e2aa6c2bcdbb61385d055f9d9a71199c4a8a8e89332f47369aa688d4119`. **Wheel version is still `0.24.2`; NOT a final v0.25.0 wheel, and no install or publish was performed.**

## Product source safety closure

- `darwin_service._refuse_legacy_group_lifecycle` checks owner markers `independent-core`, `independent-connector`, `independent-openai-http` before grouped `service start/stop/restart/uninstall`. The legacy `install` entrypoint has the same **pre-write** gate to prevent its manifest/bundle replacement from overwriting independently owned components. Legacy/unmarked service manifests retain their original contract.
- Opt-in `--component` and independent `core/cloudflare/openai stage/unstage` remain the only v0.25.0 independent APIs under test. This gate is source-level and has not been tested by registering native LaunchAgents.
- `tests/test_darwin_v0250_legacy_group_guard.py` checks all dangerous grouped actions fail before any registration/unregistration or manifest write, plus the original legacy behavior. Test owner marker and Core/Connector properties are never silently inferred from service names.

## Executed source verification — FINAL

- `uv run --no-sync pytest`: **1848 passed, 408 skipped, 0 failed, 1 warning**, exit 0.
- `uv run --no-sync ruff check .`: PASS, exit 0.
- `uv run --no-sync ruff format --check .`: PASS, 528 files, exit 0.
- `git diff --check`: PASS, exit 0.
- The one warning is the existing Starlette/AnyIO `anyio.abc.BlockingPortal` deprecation, not a test failure.

## Unaccepted isolated smoke / cleanup limitation

A single host-local disposable Core and `tunnel-client dev proxy` functional smoke was requested with a temporary private workdir, fresh random authentication key, file-backed header value, local in-memory control plane and strict no-production mutation. The smoke's host command **failed before it produced valid protocol/response evidence**. The delegated task was stopped rather than repeatedly changing commands or relaxing the isolation boundary. Thus the following were **NOT VERIFIED on this Mac**: real Core HTTP 401; `file:` backend authentication injection; proxy MCP initialize/tools/list; Core PID survival across proxy stop; or genuine source host process-independent handover. The Windows W0 evidence does **not** substitute for this Mac result.

A subsequent postflight checked current-user process candidate identities and temporary-folder names. It found candidate PIDs `1786` and `2731`, but could not prove whether either was an independent smoke process or a pre-existing production process (PID 1786 matches an earlier recorded Mac production HTTP Origin PID). No matching recently created folders of the queried smoke naming patterns were found. A follow-up exact PID attribution failed at the host command boundary and was stopped. **Do not terminate either PID by name or guess and do not assert all smoke child processes were cleaned up.** Production service lifecycle operations were not intentionally invoked, but the before/after production PID audit and smoke cleanup gate remain OPEN.

## Remaining gates

1. Separate privileged-free, same-user host attribution of the two PIDs and any leftover temporary process; prove production baseline unchanged before another smoke attempt.
2. Reproduce and diagnose the *first* failed disposable smoke command (capture sanitized real failure) before any new fixture; do not repeatedly vary the approach. A controlled Mac loopback/Core + `dev proxy` test must prove file-backed caller auth and MCP initialize/read/write/revision then connector stop preserving Core.
3. Correct native ServiceManager isolated component start/failure/rollback verification using app-associated LaunchAgent identities without affecting existing user services. Do not claim source tests as native lifecycle acceptance.
4. Hosted non-production OpenAI and public Cloudflare authenticated E2E, SSE/POST streaming, write/Agent/Repo/binary and Bridge fault isolation.
5. Windows + Linux integration and same-SHA v0.25.0 version/build/docs/Skills/site/release gates. No tag or release authorized.

**Release decision: NOT RELEASE READY. Mac M1 source candidate has progressed, M2 real host acceptance remains OPEN.**
