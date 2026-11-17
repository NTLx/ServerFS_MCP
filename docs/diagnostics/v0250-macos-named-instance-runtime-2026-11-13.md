# v0.25.0 macOS — Named Core runtime and Bridge isolation checkpoint (2026-11-13)

> **历史检查点（已由 2026-11-14 验收更新）：** 本文提及的 `owned` Bridge 尚未验收、原生注册禁止以及主项目完整 pytest 退出码未确认，均为 11 月 13 日阶段状态。最新真实 SMAppService 端到端结果、全量回归和剩余发布门禁见 `docs/diagnostics/v0250-macos-owned-native-acceptance-2026-11-14.md`。本文件保留原始审计，不应作为最新验收结论。

**Status: named file-only multi-Core SOURCE + REAL macOS ServiceManagement E2E PASS; owned/shared Bridge runtime NOT ACCEPTED.** This is an unreleased working-tree candidate, not a v0.25.0 cross-platform release.

## Scope and shared architecture

- Windows-authored `docs/architecture/v0.25.0-instance-model.md`, `docs/architecture/v0.25.0-lifecycle-decoupling.md` and `src/serverfs_mcp/instance_identity.py` were previously copied and independently verified to match Mac byte-for-byte. Public identity contract: `instance_id`, `bridge_id`, `bridge_mode=disabled|owned|shared`, and optional `connector_id`. The common files were not edited in this increment. No Git fetch/commit/push/tag/release.
- Production safety baseline: default OpenAI Tunnel PID 1779, Agent Bridge PID 1790, Cloudflare Connector PID 99279, HTTP Core PID 99271 (as observed on 2026-11-13 before work). Real native acceptance scripts independently sampled **all four** before/after execution and verified they remained unchanged.

## Implemented Mac-specific source

- `src/serverfs_mcp/darwin_instance_runtime.py`: file-only (`bridge_mode=disabled`) named-instance private state, 0600 credentials/manifest, explicit private `SERVERFS_DATA_HOME`, file ingress disabled, config SHA pinning, duplicate-instance/port denial, dedicated signed bundle, per-component `SMAppService` status/register/unregister, PID and HTTP 401 readiness, scoped rollback on readiness error, conservative unstage, Cloudflare Connector staging/unstage with protected token path. Neither `default` nor `owned/shared` can be activated through this API.
- `src/serverfs_mcp/darwin_instance_cli.py`: opt-in `python -m serverfs_mcp.darwin_instance_cli --instance ID {stage|status|start|stop|unstage|cloudflare-stage|cloudflare-unstage}`; independent from Windows public CLI development.
- `tests/test_darwin_v0250_instance_runtime.py`: instance state, collisions, credentials, Bridge deny, lifecycle, rollback, Connector and private data-home guards.
- `src/serverfs_mcp/darwin_instance_bridge_identity.py`: *planning only* of independent owned Bridge socket/lock/state/config path. Darwin AF_UNIX sun_path limit enforced. Socket directory uses SHA-256-derived 16-hex suffix `sfbr25-...` to stay shorter than the legacy directory and avoid full identifier length growth; explicit owner collision checks. **No Bridge registration/start, Core endpoint integration or per-Core client credentials.**
- `tests/test_darwin_v0250_bridge_identity.py`: identity and isolation tests, defaults/refusal/long-path checks.
- `native/macos/service_manager/Sources/ServiceManager/main.swift` refuses non-default Bridge registration; default original four jobs preserved.

## Actual Mac acceptance — measured, not inferred

1. Disposable signed named bundle + isolated synthetic private Core/Cloudflare **staging** roundtrip: `deployment/macos/v0250_named_file_core_smoke.py`, PASS / exit 0; no LaunchAgent registration.
2. Disposable named file-only Core short-lived standalone HTTP MCP SDK: `deployment/macos/v0250_file_only_http_e2e.py`, PASS / exit 0. Anonymous and wrong API Key 401; 11 file tools (no Agent tools); read, revision-guarded create/delete, authentic SDK roundtrips.
3. **Real app-associated launchd/ServiceManagement registration** of one randomly named Core: `deployment/macos/v0250_named_launchd_acceptance.py --execute`, PASS / exit 0; ID `v25-e2e-d48cba83`. Real MCP file operations; scoped unregister and cleanup succeeded, four production PIDs unchanged. Final record: `isolated_mcp_accepted=true`, `named_service_stopped=true`, `named_state_removed=true`, `retained_diagnostic_dir=null`.
4. **Two new independently registered and concurrent Core instances:** `deployment/macos/v0250_two_named_cores_acceptance.py --execute`, PASS / exit 0; IDs `v25a-807848dc`, `v25b-807848dc`. Distinct TCP ports, 4 successful MCP SDK roundtrips, stop/restart first Core without changing second Core PID/functional capability. Both unregistered/cleaned; four production PIDs unchanged; no retained diagnostic directory.
5. Focused native instance/Bridge identity suites after short socket hash correction: **46 passed**, Ruff and `git diff --check` PASS. Additional stage-time default Core reserved TCP port collision regression added, followed by a later source preflight: **47 passed, 0 failed**, full repo Ruff lint/format PASS (`ruff format --check`: 548 files), and `git diff --check` PASS.
6. Full project `uv run --no-sync pytest -p no:cacheprovider -q` performed before the last Bridge identity/default-port test additions: exit 0, 2288 tests collected, progress 100%. The latest 47-test focused run covers subsequent changes; a final complete root + separate Agent Bridge package suite remains a closure gate. Native Swift ServiceManager and ServiceLauncher `xcrun swiftc -typecheck` both passed earlier in this session. No final installed production bundle was replaced.

## Deliberate limits / remaining steps

- **Only topology D (multiple file-only Cores) is currently real Mac native acceptance.** Existing default dual ingress remains stable and independently demonstrated in 2026-11-12 report.
- Cloudflare named Connector `stage/start/stop` API has source/fixture tests, but real Cloudflare public Connector with per-instance Tunnel token/DNS, long SSE, public ChatGPT/Agent/Repo Ops **NOT VERIFIED**. Do not claim staging means Tunnel is live.
- Native OpenAI direct HTTP named Connector and multiple same-provider `connector_id` app-associated LaunchAgents remain incomplete; app bundle currently supports one OpenAI and one Cloudflare label per named Core.
- Bridge config schema already supports custom `socket_path`, `state_dir`, `lock_dir`, but Core's `native_endpoint.derive_endpoint()` currently derives a fixed global UID socket and rejects nonmatching overrides. **Independent owned Bridge** requires scoped Core endpoint wiring, lease/policy/workdir exclusion, real fault isolation and rollback; deliberately denied now.
- **Shared Bridge** requires authenticated per-Core identity, explicit workdir/policy grants and revision/revocation checks. Same-UID socket access is insufficient; no cross-Core shared Bridge started.
- All three OS platform gates, shared-Bridge protocol, source reconciliation with Windows, instance-qualified Connector variants, public E2E and final-SHA release gates remain open.
- No existing production service was stopped, restarted, migrated, upgraded, or retokened; no Git publication.

**Operation principle:** Never run legacy grouped service commands against independent instances; use the explicit named CLI. The staging command accepts a freshly built, signed default template app and keeps workdir operator TOML and secret source files outside exposed directories. Removal is deliberately conservative when Core data/logs or any unexpected files remain.

## Subsequent no-network Connector native acceptance (same day)

- New `deployment/macos/v0250_named_connector_lifecycle_acceptance.py --execute` uses a unique signed named app and a synthetic Cloudflare Connector executable that performs only `exec /bin/sleep 300`; it contains no real Cloudflare Token or network access.
- **PASS / exit 0**: `v25c-4e39a5a0`; Connector-only register, stop and re-register left Core PID unchanged; MCP file/revision operations passed before/after (2 SDK roundtrips). Default four production PIDs unchanged. Named private state and both test jobs removed; no retained diagnostics (`retained_diagnostic_dir=null`).
- This proves independent macOS ServiceManagement lifecycle for named Core versus named Connector, **not** public Cloudflare HTTPS connectivity, real Tunnel token handling, SSE, DNS, or OpenAI Connector compatibility.
- Entire repository Ruff check and format-check PASS in preflight (550 files formatted), with `git diff --check` PASS before this native acceptance.

## Final broad source verification and transient Doctor diagnostic (same day)

- `uv run --no-sync ruff check .`: **PASS, exit 0**.
- `uv run --no-sync ruff format --check .`: **PASS, exit 0, 550 files checked**.
- `uv run --project agent_bridge --no-sync pytest -p no:cacheprovider -q agent_bridge/tests`: **PASS, exit 0**. The abbreviated captured output did not include a reliable test-count summary.
- Both `xcrun swiftc -typecheck` for the macOS native ServiceManager and ServiceLauncher: **PASS, exit 0**.
- `git diff --check`: **PASS, exit 0**. Branch remained `main` at HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`, with all uncommitted changes preserved.
- First full root `uv run --no-sync pytest -p no:cacheprovider -q` in the final gate: **FAIL, exit 1**, exactly one Doctor test, `tests/test_cli.py::TestDoctor::test_doctor_reports_full_probe_surface`, because macOS test-temp storage identification temporarily returned `None` (fail-closed `filesystem: FAIL`). It did not implicate a new instance test. No product code was changed.
- One targeted, non-mutating reproduction of that exact Doctor test: **PASS, exit 0 (1 passed)**; the separate synthetic temporary volume probe observed `mount` exit 0 with 11 lines and `_darwin_volume_info(...)=('apfs',False)`, classified `OK: APFS` under mountpoint `/`. Thus the first full-run failure is consistent with an intermittent macOS mount-probe environment issue, but its exact transient cause is not proven.
- **One confirmation full root pytest re-run after the targeted diagnosis: PASS, exit 0, reached 100%, no failures**. The quiet-mode output supplied no reliable passed/skipped totals; none are inferred.
- Full native multi-instance acceptance and no-production-PID-change evidence above remain independently valid. No registration of an owned/shared Bridge, no release, no Git commit/push/tag, and no migration of production jobs occurred.

## Parallel-session follow-up — same day, source reconciliation and new tests

This addendum records **additional** work performed against the same live Mac repository without altering the already-accepted production jobs or the Windows-owned shared identity model:

- Named Core readiness now verifies `lsof` listener **PID ownership** for the exact `127.0.0.1:<port>`, in addition to a launchd PID and unauthenticated HTTP 401. Read-only evidence: default HTTP Core PID 99271 owned TCP port 8765. **Do not confuse** default OpenAI Tunnel PID 1779 with Core PID 99271.
- Mac-specific named OpenAI direct HTTP-to-Core Connector source staging/unstage and CLI dispatch added. The control-plane key and Core bearer-header file must be separate, private and consistent, the Tunnel ID must not be claimed by other instances, and Connector lifecycle must depend on the correct named Core. **No real public OpenAI named ingress acceptance is claimed**.
- Non-default native Swift launcher now verifies private instance directory ownership, manifest `owner`, `instance_id`, disabled Bridge mode and absence of Bridge manifest entry before executing; default bundle behavior is unchanged.
- New `tests/test_darwin_v0250_parallel_core_e2e.py` ran **two real `serve` child processes concurrently**, each with a unique port/API key/data root and authenticated MCP filesystem roundtrip; cross-key 401 and no workdir contamination; **PASS, 1 test**, all children cleaned.
- `deployment/macos/v0250_named_instance_runtime_smoke.py` built/signed a disposable native App, staged synthetic Core + Cloudflare + OpenAI manifest, queried app-associated registration state (all absent), and removed all temporary data; **PASS**.
- The pre-existing canonical owned-Bridge planner `darwin_instance_bridge_identity.py` was discovered while working concurrently. A redundant newly authored Bridge namespace module/test pair was **deleted**; the original planner, tests and safety gate remain authoritative. No owned/shared Bridge was started.
- Real one-instance launchd acceptance was rerun with a unique synthetic named Core: business MCP operations accepted but its **old teardown check** rejected immediate post-stop port rebind, preserving the test artefacts. Independent read-only inspection later confirmed the named service **not registered**, no PID or TCP listener, and an available port. A strictly guarded cleanup removed only the exact synthetic instance `v25-e2e-b30b2ab3` and its matching test temp directory; four default production PIDs remained unchanged. The acceptance script was surgically corrected to require TCP `ConnectionRefused` (no live listener), not an immediate `bind()`. A **new** real named Core acceptance `v25-e2e-b17c1196` then **PASS, exit 0**, named service stopped, all synthetic state cleaned, and default PID baseline preserved.
- Named Cloudflare staging now rejects reusing a token **file or hardlink** owned by another named instance; it does not read the token value. Focused latest test run: **48 passed**, Ruff PASS.
- Latest full read-only gate `agt_6c600f2e160d16b21feeee9c`: `ruff check .` PASS; `ruff format --check .` PASS (**552 files**); root pytest and separate `agent_bridge` pytest both PASS, exit 0, 100% (quiet output omitted reliable totals); native Swift ServiceManager/ServiceLauncher typechecks PASS; `git diff --check` PASS. No Git mutation, release or default service operation.

**Remaining:** per-instance public Cloudflare/OpenAI HTTP ingress and Connector ID variants, independent owned Bridge runtime IPC/leases/policy plus overlapping writable-root denial, authenticated per-Core shared Bridge grants/revocation, and the reconciled cross-platform release gates are **not yet accepted**. The current real named Core implementation only enables `bridge_mode=disabled`. No central instance management platform was added.


## Same-day extension: independent named owned-Bridge source and isolated execution

**Scope:** Mac-only native owner namespace/config policy, plus the separate Bridge package's Mac-specific renderer. No Windows/common identity edits; no production service registration, process restart, Git commit/push/tag or version release.

### Real independent process and protocol evidence

1. `src/serverfs_mcp/darwin_instance_bridge_identity.py` now has a conservative cross-Bridge writable workdir overlap check, including same/nested roots and symlink aliases. Read-only mappings do not claim writer leases. It canonicalizes **only the trusted OS-provided macOS runtime path** (needed for /var → /private/var) and preserves strict refusal of user-supplied unsafe symlink paths. Tests passed on the real Darwin AF_UNIX length budget.
2. `deployment/macos/v0250_two_owned_bridges_ipc_acceptance.py --execute`: **PASS / exit 0**. Two separate supervised Bridge processes, independent 0600 AF_UNIX sockets, private task and lock directories, two successful Fake Adapter tasks. Cross-instance workdir submissions and foreign task ID reads refused. Test resources cleaned; four default production PIDs unchanged (`agt_bd18b13e65f49cfa93bdb23a`). No real Codex, Claude or Qoder executed.
3. `deployment/macos/v0250_two_core_bridge_http_acceptance.py --execute`: **PASS / exit 0**. Two separate real local Uvicorn/MCP Core endpoints with distinct synthetic API keys and explicitly bound `AgentBridgeClient` instances; two Bridge Fake tasks retrieved via the correct Core's MCP surface; cross-key 401 and foreign-task lookup refusal; Core+Bridge ephemeral resources removed. Production PIDs unchanged (`agt_6963201f8d1ab3f461eb75d2`). **This does not yet enable signed launchd-owned Core → Bridge startup.**

### Private owner policy and staging acceptance

4. Mac source preflight `preflight_named_owned_bridge` checks candidate TOML, denies overlapping writable roots against default and other private Bridge policies, and refuses unknown owner/mode. `python -m serverfs_mcp.darwin_instance_cli --instance <id> owned-preflight --config <toml>` invokes it read-only. Dedicated suite initially **50 passed**; CLI extension **52 passed**. Real read-only default Bridge policy preflight `deployment/macos/v0250_owned_bridge_preflight_smoke.py`: **PASS**, one peer policy and one synthetic workdir, temp data cleaned (`agt_58152440cf3a682120177207`). This check reads private policy internally but never exposes credentials.
5. Added Bridge-owned `agent_bridge/src/serverfs_agent_bridge/macos_owned_config.py`. It independently derives named socket identity, reuses the Bridge's authoritative `build_policy_document`, validates private 0700 directories and a 0600 config with **actual `BridgeConfig.load`**, and refuses overwrite, default owner, shared-group socket mode, Fake runtime in production CLI, unsafe owner paths and private state exposure. Includes a separate `--check` mode using Bridge-owned policy parity and runtime identity to refuse drift without rewriting configuration. Synthetic Bridge tests: **10 passed** in latest focused run (`agt_6e239eda34dcc0c94bea8efb`).
6. Added `src/serverfs_mcp/darwin_instance_owned_staging.py`: a narrow source-only caller. Core distribution uses `agent_lifecycle.render_request_from_config`, sends non-secret policy via stdin to the separately installed Bridge Python, checks the redacted output and workdir projection, and provides a read-only `check_owned_bridge_policy`. No direct cross-package import; no launchd activation. The macOS operator CLI now also has `owned-policy-check --config <toml> --bridge-python <path>`. Latest main-module targeted suite: **31 passed** (`agt_53f9c1d93162adc02b8c65e4`); initial test/CLI dispatch defects were fixed before this pass.
7. `deployment/macos/v0250_owned_bundle_policy_smoke.py`: **PASS / exit 0**; real Swift template build, signed per-instance app with named owned-Bridge LaunchAgent plist, private owner policy staged by separate Bridge Python, and read-only drift check. Reports `registered=false`, `agent_executed=false`, `production_modified=false`, temporary state removed (`agt_53f9c1d93162adc02b8c65e4`).

### Broad verification and explicit remaining blocker

- Full root `ruff check .`: PASS; root `ruff format --check .`: PASS, **560 files**.
- Independent Bridge `ruff check` and `ruff format --check`: PASS, **105 files**; entire Bridge pytest: PASS, exit 0 (quiet output lacked reliable total).
- Both native Swift typechecks and `git diff --check`: PASS.
- Exact read-only production process continuity: OpenAI Tunnel PID 1779, Agent Bridge PID 1790, HTTP Core PID 99271 and Cloudflare Connector PID 99279 all matched the prior baseline.
- **The latest root full pytest exit is UNVERIFIED**, not PASS: one attempt returned only progress through ~37%, without a terminal exit. A separate controlled attempt to capture the real exit could not proceed reliably because the remote command executor imposed a 30-second outer limit. No success is inferred from partial output. Latest focused root tests did pass (31), and earlier root full-suite passes are recorded above.
- No installed `owned` Bridge or signed named Core → Bridge lifecycle has been authorized: the native ServiceManager and ServiceLauncher still refuse non-default Bridge service registration. This is intentional until per-instance supervisor/lease/manifest validation, writable-root collision checks at startup, Core endpoint binding, rollback, and real app-associated launchd acceptance are complete.
- `shared` Bridge remains **blocked** pending authenticated per-Core grants, capability/policy revision checks and revocation. Non-default public Cloudflare/OpenAI Connector E2E, connector IDs, and coordinated Linux/Windows/macOS release gates remain open. The released/production default service behavior was not changed.


