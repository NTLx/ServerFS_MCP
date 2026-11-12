# ServerFS v0.25.0 — macOS M1 independent Core staging (2026-11-12)

**Status:** Source-only candidate implemented; native/public service acceptance **NOT VERIFIED**. This record does not authorize deployment, Git publication or release.

## Background and topology

The original `http-origin` is already a separate `SMAppService` managed LaunchAgent. v0.25.0 initially reuses that OS service identity for a long-lived HTTP MCP Core, avoiding a duplicate persistent process. Existing production Cloudflare still owns its historical paired HTTP-origin + Connector registration; the active service and its read-only private TOML have not been changed.

Windows W2/W3 and Mac M0/M1 development are parallel, under the common v0.25.0 independent Core/Bridge/Ingress architecture and one-release cross-platform acceptance gates in WorkPC `dev_plan_v0.25.0.md`. Do not infer that Mac HEAD was refreshed from GitHub: Mac locally observed `main == origin/main == 5e952cb9`, with existing uncommitted v0.24.3 source/test/docs preserved.

## Direct source implementation

- `src/serverfs_mcp/darwin_service.py`: refactored HTTP-origin spec/security preflight into a shared helper while retaining legacy Cloudflare validations; introduced independent `_core_service_spec` with exact public hostname plus exact `127.0.0.1:<port>` Host authority (no wildcard).
- `stage_core_service`: validates local installed ServiceManager and embedded app-owned plists; rejects any registered or existing Core/HTTP-origin manifest, existing Cloudflare connector manifest, or pre-existing private environment snapshot. Workdir/native TOML syntax/roots, symlink boundaries, env file owner/mode 0600, all exposed workdirs and repo-root credential confinement, strict supported HTTP auth setting list and exactly one API Key/OAuth remain gated. The snapshot gets revalidated before writing. It stages a private mode-0600 environment snapshot and an atomic manifest entry marked `origin_owner=independent-core` but does **not** register or start any LaunchAgent. On manifest publish failure it removes only the newly created snapshot and preserves the previous manifest.
- `_require_shared_core_config`: if an OpenAI Tunnel service manifest exists, validate its exact single absolute `--config` and require that it resolves to the **same canonical TOML** as the new Core; fail closed on malformed/missing/mismatched configs.
- `core_status`: reports registration, manifest presence, explicit Core ownership and OS-managed PID independently, without printing secrets.
- `unstage_core_service`: only removes the manifest entry marked `independent-core` when Core is unregistered and Cloudflare connector absent; verifies the owned private snapshot first and never deletes the source credential file. Legacy or running HTTP origins are refused.
- Existing `core_start/stop/restart` now reject unmarked legacy HTTP origins; the legacy `cloudflare_start/stop/restart/uninstall` pair path refuses independently owned Core rather than accidentally stopping its HTTP process.
- `src/serverfs_mcp/cli.py`: added `serverfs service core stage --config ... --env-file ... --http-port ... --hostname ...`, `service core status` and `service core unstage`; macOS-only, explicit command. Existing opt-in `service {start|stop|restart} --component {tunnel|cloudflare|core}` preserves separate lifecycle. No automatic Core/Bridge/Connector installation.

## Security and limitations

- This stage intentionally **does not migrate** the existing coupled production `http-origin + cloudflare` registration. Such a host is refused for staging; its existing PID, LaunchAgent owner and private HTTP env remain unchanged.
- Mac's generic legacy `service stop/restart/uninstall` still groups Tunnel + Bridge. Legacy `service cloudflare` commands still operate historical pairs only; neither is the final v0.25.0 component-only contract.
- Installer staging is not a live OS or public ChatGPT endpoint acceptance. Core sharing, OpenAI HTTP upstream, actual caller auth, >60s SSE, Agent/Repo Ops, Bridge fault isolation and controlled migration need dedicated real Mac M2 evidence.
- No Git commit, push, tag, release, service restart, credential read/rotation, DNS change or production workdir mutation occurred.

## Tests and remaining release gates

- Added `tests/test_darwin_v0250_core_staging.py`; extends component lifecycle tests. It exercises manifest-only staging, no registration, exact dual-ingress Host, private env permissions, denied credential paths/unsupported env, source TOML equivalence, pre-existing origin/connector refusal, failed manifest publication rollback and unstage guards. Legacy Cloudflare focused tests also cover regression.
- Final Mac focused tests after the canonical-TOML guard: **96 passed, 1 skipped**, exit 0.
- Final full root `uv run --no-sync pytest`: **1811 passed, 408 skipped, 1 Starlette/anyio DeprecationWarning**, exit 0. Skipped tests are NOT acceptance.
- Full `uv run --no-sync ruff check .`: **PASS**, exit 0. Full `uv run --no-sync ruff format --check .`: **PASS**, 523 files, exit 0. `git diff --check`: **PASS**, exit 0.
- Isolated native ServiceManager.app build, codesign and four bundled LaunchAgent plist checks: **reported PASS** in local build execution. The candidate wheel build command succeeded, but subsequent ZIP content assertion checked incorrect **source-tree prefix** `native/macos/service_manager/Sources/` instead of the packaged prefix `serverfs_mcp/_darwin_service_manager/` defined by `pyproject.toml`. Thus **wheel member-content acceptance is NOT VERIFIED**. The delegated build task was cancelled rather than repeatedly requesting out-of-workdir command approval; no installed application or service was changed.
- Required next: independently verify wheel members using the correct packaging prefix; independent Connector staging and historical pair migration/rollback; OpenAI direct HTTP ingress; real Mac component/public/Agent E2E; Windows/Linux unified acceptance and exact-SHA release pipeline.

**Conclusion:** independent Core staging and fail-closed native component boundaries exist as Mac source/test candidates. Full M1 and M2 acceptance remain OPEN.
