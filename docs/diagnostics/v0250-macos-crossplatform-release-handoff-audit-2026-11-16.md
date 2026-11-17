# v0.25.0 macOS / cross-platform release handoff audit

Date: 2026-11-16

Scope: read-only audit of the local macOS worktree and release handoff. This report does not claim that the cached `origin/main` ref matches the remote; no network or Git mutation was used.

**Design clarification added 2026-11-16:** The subsequent accepted ADR `docs/architecture/v0.25.0-native-platform-boundaries.md` establishes **native platform implementations first**. The source-convergence recommendations below are audit-time history; common private HMAC bytes, Bridge RPC schema, persistent task store or shared source are NOT release prerequisites absent a demonstrated interoperability benefit. The original inventory/counts remain a snapshot. **Later native acceptance:** an isolated signed macOS file-only Core passed real SIGKILL → launchd KeepAlive recovery in 4.89 seconds, with all test-owned resources removed and protected production PIDs preserved; see `v0250-macos-keepalive-native-rerun-2026-11-16.md`. This does not establish production Connector crash recovery.

## Baseline and scope

- Repository: `/Users/lx/Projects/ServerFS_MCP`
- Branch: `main`; upstream: `origin/main`
- `HEAD`: `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`
- Locally cached `origin/main`: `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`; `HEAD...origin/main` is `0 0`. This is local ref equality only, not proof of current remote state.
- Initial worktree inventory, before creating this report: **233 changed paths**: 11 modified tracked files, 222 untracked files, 0 staged paths. Tracked diff summary: **1,344 insertions, 58 deletions** across 11 files. No tracked version/lock metadata file was modified.
- The checkout was already extensively dirty. All existing files, scratch, production services, tasks, tokens, and Agent processes were left untouched. The report is the only file created by this audit.

The inventory is path/status based. Contents of private-looking/suspicious source and generated files were not printed or assessed for secrets. In particular, untracked `*_private.py` files and `.serverfs-v0250-*` scratch directories are identified by path only and must be preserved until their owner classifies them. Existing reports state that the scratch directories contain offline wheel/venv artifacts; this audit did not open their contents.

## Change classification

### 1. macOS-specific implementation, acceptance and documentation

- Modified tracked: `deployment/macos/README.md`; `native/macos/service_manager/Sources/ServiceLauncher/main.swift`; `native/macos/service_manager/Sources/ServiceManager/main.swift`; `native/macos/service_manager/build.sh`; `src/serverfs_mcp/darwin_service.py`.
- Untracked implementation: 22 `src/serverfs_mcp/darwin_*` modules plus `instance_identity.py`; `native/macos/service_manager/Sources/ServiceLauncher/SharedManifestGate.swift`.
- Untracked acceptance: 24 `deployment/macos/v0250_*.py` scripts and 49 root `tests/test_darwin_v0250_*`/related platform tests. These include opt-in live tests; their existence does not mean they were run by this audit.
- Untracked docs: 51 `docs/diagnostics/*` reports and 7 `docs/architecture/*` documents. The two specifically current inputs, `v0250-macos-release-readiness-2026-11-16.md` and `v0250-macos-cloudflare-connector-restart-acceptance-2026-11-16.md`, record successful dirty-tree macOS acceptance, not a release SHA.

### 2. Bridge / Core / protocol implementation boundaries (audit-time inventory)

- Modified tracked Bridge implementation: `agent_bridge/src/serverfs_agent_bridge/{config,main,protocol,service,store}.py` (5 files).
- Untracked Bridge implementation: `core_grants.py`, `core_grants_private.py`, `macos_owned_config.py`. The grant/protocol/store behavior is shared security surface; the private helper and macOS-owned configuration are platform adapters and must not be merged by filename alone.
- Untracked Core/protocol: `src/serverfs_mcp/core_bridge_client_v2.py` is shared protocol/client surface; other `darwin_*` modules contain macOS binding, lifecycle, staging and runtime adapters.
- Public MCP/authorization and task-ownership **outcomes** are cross-platform gates. Private v2 wire vectors, transport and persistence may be platform-specific where there is no real cross-OS Bridge interoperability requirement; `_macos` IPC, SMAppService, launchd and signing tests remain Mac-native gates.
- The local reconciliation document records a material mismatch with the inspected Windows reference: MAC canonicalization/signature representation, grant/policy binding, task isolation and continuation/idempotency behavior are not yet jointly frozen. The v2 ADR still describes an open review state. Windows is under concurrent development; no cross-machine checkout was inspected during this audit, and this Mac worktree cannot prove Windows' current source state.

### 3. Unrelated pre-existing changes

No clearly unrelated path was identified from the status inventory: the dirty set appears associated with the v0.25.0 macOS candidate, shared Bridge v2 work, its tests/evidence, and generated candidate artifacts. This is a path-level inference only. Treat every unclassified or private-looking path as owner-held and preserve it; do not discard it as unrelated without its owner’s review.

### 4. Generated artifacts

The 222 untracked paths include **55 paths** under four `deployment/macos/.serverfs-v0250-*` scratch trees (wheelhouse, wheels, venv/import evidence and dependency artifacts), along with acceptance scripts and reports. These scratch paths are untracked and are not release inputs. Do not add them to a release commit or delete them in bulk; first identify their owner and retain/remove them only through a separately authorized, bounded cleanup. The remaining untracked source, tests and docs are candidate source/evidence, not generated build output merely because their names include `v0250`.

### 5. Release metadata and version/docs

- `pyproject.toml`, `agent_bridge/pyproject.toml`, `plugin.json`, `uv.lock`, and `agent_bridge/uv.lock` currently carry **0.24.2** project/distribution/Plugin lock metadata. No version files are dirty. The two wheels described in the 2026-11-16 readiness report also have 0.24.2 metadata despite passing current dirty-source archive, fresh-install and three-process checks.
- The repository baseline in `AGENTS.md` still identifies v0.24.2 as stable. No coordinated v0.25.0 release metadata, release note/checklist, or exact-SHA candidate exists yet.
- Accordingly, the passing macOS source, native, public Cloudflare/SSE, restart and dirty-wheel evidence is **candidate evidence only**. It is not acceptance of a clean v0.25.0 commit or its release wheels.

## Acceptance evidence and limits

The 2026-11-16 readiness report records: real two-Core SMAppService registration; Bridge-owner crash/restart and grant revoke/re-authorize; dirty-source Root/Bridge full suites and Ruff; two fresh Python 3.12 wheel installs and installed-wheel three-process E2E; public Cloudflare MCP and a 75.640-second SSE; and Connector-only restart with a 75.018-second SSE. The connector report records a bounded Cloudflare restart: default OpenAI Tunnel PID 1779, Agent Bridge PID 1790, and HTTP Core PID 40516 were unchanged; Cloudflare Connector changed PID 40543 → 66419. The audit did not re-run or independently reproduce those operational checks.

As of the subsequent macOS native rerun, isolated file-only Core KeepAlive abrupt-exit recovery has passed. Still-unresolved product gates include exact v0.25.0 release-SHA wheel/build acceptance; **production Connector** unclean-exit recovery; cross-region/multi-client behavior; and delegated nested external-MCP authorization. The existing public Cloudflare workdir/Agent acceptance does not establish that nested MCP calls are authorized.

## Minimal safe convergence and release-candidate sequence

1. **Freeze the input set without altering it.** Record this `HEAD`, classify every tracked and untracked path with its owner, and preserve the four scratch trees. Do not use broad staging or cleanup. Confirm Mac production service/task/process baselines using the existing operational procedure only when a later authorized acceptance requires it.
2. **Verify platform-independent behavior without forcing shared private implementations.** Review public MCP tool/error behavior, Core grant isolation, replay/revocation, task ownership, topology A/B/C/D and independent service lifecycles across macOS, Windows and Linux. Keep each OS's proven native private IPC, credentials, state and supervision implementation. Adopt common source or HMAC/serialization test vectors only when a documented interoperability or maintenance/security benefit requires them; see the native-first ADR.
3. **Build a reviewed source manifest from explicit paths.** Separate shared portable source/tests from macOS-only source/tests/docs and exclude generated scratch. Review every pre-existing dirty file and document its disposition. Do not stage or discard unknown paths.
4. **Prepare an isolated, clean release branch/checkout from the agreed base.** Integrate only the reviewed candidate source and required tests/docs, then coordinate the 0.25.0 version updates across root/Bridge/Windows metadata, locks, Plugin metadata, current-state docs, and release note/checklist. Keep historical evidence unchanged. Do not perform Git mutations as part of this audit.
5. **Validate that one clean candidate SHA.** Run the applicable root and Bridge locked gates; macOS native build, wheel-member/source-integrity, fresh install and real native lifecycle tests; Windows native/Agent gates on Windows; Linux/container gates; and release workflow/version consistency checks. Rebuild Core/Bridge wheels from that SHA and require metadata to report 0.25.0. Run exact-SHA acceptance on all platforms and Pages before considering publication.
6. **Protect the existing production OpenAI and Cloudflare paths.** Use isolated test-owned service identities/configuration and disposable workdirs for candidate acceptance. Do not replace/reconfigure production ingress or restart production OpenAI/Cloudflare/Core/Bridge merely to obtain candidate evidence. Any deployment/cutover is a separate explicit operation with its own bounded rollback plan.
7. **Only after the candidate gates pass, use the repository's dispatch-first stable release workflow.** Push the intended commit, wait for ordinary CI, dispatch `wheel-release.yml` on `main` with exact `v0.25.0`, and let the workflow recheck that exact SHA and current `main` before tag creation. This audit did not authorize or perform these Git/GitHub actions.

## Cross-platform release blockers

- Shared Bridge v2 **observable authorization and isolation outcomes** still require platform-by-platform confirmation. The macOS and Windows private client wire implementations need not be byte-identical unless an actual cross-platform interoperability scenario is supported.
- No Windows or Linux exact-SHA source/native/container acceptance for this candidate is evidenced here.
- Current dirty-tree wheels are 0.24.2, not release-matched 0.25.0; no clean release SHA or matching platform artifacts exist.
- No three-platform same-SHA CI/build gates or Pages gate have run for a release candidate.
- On macOS, exact-SHA revalidation, **production Cloudflare Connector** unclean-exit recovery, multi-region/client behavior, and nested delegated MCP authorization remain open; isolated native named file-only Core SIGKILL/KeepAlive recovery passed in the separately dated rerun evidence.
- Current cached `origin/main` equality does not establish the present remote tip. A later explicitly authorized release workflow must establish freshness through its normal CI and dispatch-time SHA check; this audit must not fetch to guess.

## Audit verification

- No service, task, token, active Agent process, dependency, lockfile, project version, or pre-existing file was changed by this audit.
- `git diff --check` passed for modified tracked files. This new untracked report was separately checked for whitespace and common credential/token patterns; no matches were found.
- Tests/builds/live acceptance were not run as part of this audit. Previously reported evidence is cited as existing evidence, not newly verified.
