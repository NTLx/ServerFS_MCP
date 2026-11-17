# v0.25.0 macOS native instance foundation — 2026-11-13

**Status: SOURCE / ISOLATED BUNDLE ACCEPTED; MULTI-INSTANCE RUNTIME NOT ACCEPTED.**
No Git fetch, pull, commit, push, tag or release; no production service registration, migration, restart, credential change, or public workdir mutation.

## A. Read-only actual macOS baseline

The directly connected `macbook-serverfs` workdir `serverfs` is read-write. A separately bounded read-only native host check (`agt_32ff72afa52ab9c73a346c3e`) observed:

- Root: `/Users/lx/Projects/ServerFS_MCP`; HEAD: `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`; branch `main`, upstream `origin/main`; **clean working tree before this increment**. No remote fetch was executed.
- Existing `gui/501` launchd jobs all registered, enabled and running: OpenAI Tunnel PID **1779**, Agent Bridge PID **1790**, Cloudflare Connector PID **99279**, HTTP Core PID **99271** (loopback `127.0.0.1:8765`).
- Installed default app: `~/Library/Application Support/ServerFS/service-manager/ServerFSServiceManager.app`.
- Bridge had a current-user, mode-0700-parent / mode-0600-socket Unix-domain socket under the host's temporary runtime directory; no IPC handshake was attempted.
- These are process/OS observations, not a new claim of public MCP E2E health.

Earlier actual Cloudflare/OpenAI public ingress, filesystem CRUD, policy parity, and read-only Codex E2E evidence remains in `docs/diagnostics/v0250-macos-cloudflare-parallel-cutover-2026-11-12.md`.

## B. Windows contract synchronized without overwriting concurrent work

The WorkPC source revision of `src/serverfs_mcp/instance_identity.py` and both public ADRs were copied byte-for-byte to this Mac source tree and then compared with the Windows connector again; all **three complete contents matched exactly** at the comparison point:

- `src/serverfs_mcp/instance_identity.py` (WorkPC source revision `v1:960ef7600ee033b3`);
- `docs/architecture/v0.25.0-instance-model.md` (`v1:f7972b493847fde3`);
- `docs/architecture/v0.25.0-lifecycle-decoupling.md` (`v1:e0129eb61c60a503`).

This is **not a Git pull or merge**, and does not assert future Windows changes are synchronized. These public contract files were copied unchanged rather than edited on Mac. Both hosts' uncommitted work must be reconciled before a shared commit.

## C. Mac-owned source implementation

1. `native/macos/service_manager/Sources/ServiceManager/main.swift`: qualified per-instance LaunchAgent plist identities from the signed app `ServerFSInstanceID`; default names unchanged; non-default `all` operations refused.
2. `native/macos/service_manager/Sources/ServiceLauncher/main.swift`: locate the actual binary's containing app, validate bundled instance ID, require matching per-instance launcher arguments and bind to a deterministic `~/Library/Application Support/ServerFS/instances/<id>/service-manager/services.json`. Default manifest lookup remains unchanged.
3. `src/serverfs_mcp/darwin_instances.py`: deterministic instance app/data/bundle IDs; strict source-template plist checks, symlink guards, per-instance renamed LaunchAgents and identity arguments, owned-Bridge-only LaunchAgent inclusion, private temporary staging, codesign+verification and no replacement of existing bundle. **No SMAppService registration occurs here**.
4. `src/serverfs_mcp/darwin_service.py`: Bridge-only `bridge_start/stop/restart` source APIs with manifest ownership presence checks; no Core/ingress lifecycle in these calls. This is not an instance-aware CLI.
5. New deterministic `tests/test_darwin_v0250_instances.py`, `tests/test_darwin_v0250_bridge_lifecycle.py` and disposable native bundle smoke `deployment/macos/v0250_instance_bundle_smoke.py`.

No changes to general `cli.py`, common Bridge IPC protocol, Windows implementation, Linux Compose, Agent task ownership or production service manifests.

## D. Executed host validation

First bounded test task `agt_3ec8a3e1dd3985cfe190676d`:

- Initial macOS targeted pytest: **75 passed**.
- Both native Swift `xcrun swiftc -typecheck` entry points: **PASS**, exit 0.
- Initially Python Ruff lint/format required mechanical fixes; these were not disguised as success.

Second narrowly bounded task `agt_289877cb4e97da5a0833464f`:

- `ruff format` on exactly four new Mac Python files: PASS, exit 0.
- Ruff lint + format check on those files and common `instance_identity.py`: PASS, exit 0.
- Targeted pytest (new + existing Darwin lifecycle/staging tests): **81 passed**, exit 0.
- Disposable native app bundle smoke (real Swift build, new instance app/plists, codesign, `serverfs-service-manager info`): **PASS**, `installed=false`, `registered=false`, exit 0.
- `git diff --check`: PASS, exit 0.
- Full repo `uv run --no-sync pytest -p no:cacheprovider -q`: **PASS, exit 0, 100%**, but quiet output did not provide a reliable pass/skip count; no count is asserted here.

The native app smoke used a disposable temporary directory and did not register LaunchAgents or touch production credentials. New ADR copies were completed after the test run; they only add static documentation.

## E. Non-negotiable remaining gates

- The current source increment **does not** provide registered multiple instances: implement instance-scoped manifest/config/private env creation, OS app install/registration/status/rollback and per-component CLI, with collision checks and exact owner proofs.
- Instance-local Core listener, Bridge IPC socket, lease, task state, runtime directories and process ownership must be isolated. Merely assigning a new label never suffices.
- Mode `disabled` needs real file-only Core runtime acceptance; mode `owned` needs independently owned Bridge and distinct writable-root collision denial; mode `shared` must **not** be enabled until per-Core authenticated grants, capability and policy revision checking are implemented and tested. A shared-mode staged app intentionally has **no** Bridge LaunchAgent; that does not establish shared IPC authorization.
- Per-Core `connector_id` and multiple same-provider connectors remain unimplemented in native ServiceManagement.
- Real non-default LaunchAgent registration, fault isolation, independent stop/start/uninstall, public ingress, Agent/Repo Ops, transport/SSE, binary, rollback, and Linux/Windows three-platform gates remain open.
- Before any runtime deployment, require a new read-only baseline and explicit no-touch production guards. No v0.25.0 release authorization exists.

**Handoff:** Mac implementation should continue by adding guarded per-instance manifest/storage and native register/status/lifecycle API, reusing the unchanged `InstanceIdentity` names, without altering the default installed app or its four live jobs. Cross-platform Bridge grants require a separately reconciled public protocol contract.
