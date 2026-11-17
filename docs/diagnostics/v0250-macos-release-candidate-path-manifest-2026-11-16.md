# v0.25.0 macOS release-candidate path manifest

Date: 2026-11-16

**Purpose and boundary.** This is a path-only no-loss inventory of the current local macOS Git worktree for later release-candidate review. It is not a clean SHA, a release approval, or evidence that the candidate is ready. No pre-existing file, service, task, token, process, dependency or Git state was changed. No private file contents, `.env`, credential/token files, cache/venv contents, generated scratch contents, or owner-private module contents were read. Classification is conservative and based on path/status plus the referenced audit and accepted native-first ADR.

## Frozen local Git reference snapshot

- `HEAD`: `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`
- locally cached `origin/main`: `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`
- These refs are equal in this local snapshot. No fetch was performed; this does not establish remote freshness.
- Branch/upstream from the prior handoff audit: `main` / `origin/main` (not independently relied on for remote state).
- The inventory was captured with `git status --porcelain=v1 -uall -z`; all 11 tracked changes are unstaged, and the index has no staged paths. Status does not show ignored files.
- Before this report was created: **239 dirty paths** = 11 modified tracked + 228 untracked. Of the untracked paths, 55 generated scratch/build artifacts and 5 private-named owner-held paths are excluded from A–E, leaving 179 candidate paths in A–E. The report itself is the one newly created path and is outside that baseline count.

## Candidate path manifests

Lists below are exact relative paths observed in this worktree. Inclusion means “candidate source/evidence to review”, not automatic approval to stage. Candidate path counts are shown per group.
### A. Root/common code and tests (5 paths)

```text
src/serverfs_mcp/cli.py
src/serverfs_mcp/core_bridge_client_v2.py
src/serverfs_mcp/instance_identity.py
tests/test_core_bridge_client_v2.py
tests/test_streamable_http_session_sse_acceptance.py
```

### B. macOS native code and Swift (26 paths)

```text
native/macos/service_manager/Sources/ServiceLauncher/SharedManifestGate.swift
native/macos/service_manager/Sources/ServiceLauncher/main.swift
native/macos/service_manager/Sources/ServiceManager/main.swift
native/macos/service_manager/build.sh
src/serverfs_mcp/darwin_instance_bridge_identity.py
src/serverfs_mcp/darwin_instance_cli.py
src/serverfs_mcp/darwin_instance_owned_binding.py
src/serverfs_mcp/darwin_instance_owned_lifecycle.py
src/serverfs_mcp/darwin_instance_owned_runtime.py
src/serverfs_mcp/darwin_instance_owned_staging.py
src/serverfs_mcp/darwin_instance_owned_unstage.py
src/serverfs_mcp/darwin_instance_runtime.py
src/serverfs_mcp/darwin_instance_shared_binding.py
src/serverfs_mcp/darwin_instance_shared_lifecycle.py
src/serverfs_mcp/darwin_instance_shared_manifest.py
src/serverfs_mcp/darwin_instance_shared_staging.py
src/serverfs_mcp/darwin_instance_shared_unstage.py
src/serverfs_mcp/darwin_instances.py
src/serverfs_mcp/darwin_owned_bridge_supervisor.py
src/serverfs_mcp/darwin_service.py
src/serverfs_mcp/darwin_shared_agent_adapter.py
src/serverfs_mcp/darwin_shared_agent_factory.py
src/serverfs_mcp/darwin_shared_bridge_readiness.py
src/serverfs_mcp/darwin_shared_core_bootstrap.py
src/serverfs_mcp/darwin_shared_release_source.py
src/serverfs_mcp/darwin_shared_runtime.py
```

### C. macOS tests and deployment sources (72 paths)

```text
deployment/macos/README.md
deployment/macos/v0250_connector_id_validate_acceptance.py
deployment/macos/v0250_dual_connector_lifecycle_acceptance.py
deployment/macos/v0250_file_only_http_e2e.py
deployment/macos/v0250_file_only_multi_connector_acceptance.py
deployment/macos/v0250_installed_wheel_three_process_acceptance.py
deployment/macos/v0250_instance_bundle_smoke.py
deployment/macos/v0250_multi_connector_native_acceptance.py
deployment/macos/v0250_named_connector_lifecycle_acceptance.py
deployment/macos/v0250_named_file_core_smoke.py
deployment/macos/v0250_named_instance_runtime_smoke.py
deployment/macos/v0250_named_launchd_acceptance.py
deployment/macos/v0250_named_openai_lifecycle_acceptance.py
deployment/macos/v0250_owned_bridge_preflight_smoke.py
deployment/macos/v0250_owned_bundle_policy_smoke.py
deployment/macos/v0250_owned_connector_launcher_diagnostic.py
deployment/macos/v0250_owned_core_direct_runtime_acceptance.py
deployment/macos/v0250_owned_dual_connector_acceptance.py
deployment/macos/v0250_owned_registration_preflight_smoke.py
deployment/macos/v0250_owned_smappservice_live_acceptance.py
deployment/macos/v0250_two_core_bridge_http_acceptance.py
deployment/macos/v0250_two_named_cores_acceptance.py
deployment/macos/v0250_two_owned_bridges_ipc_acceptance.py
deployment/macos/v0250_wheel_archive_gate.py
deployment/macos/v0250_wheel_offline_preflight.py
tests/test_core_bridge_v2_interop_macos.py
tests/test_core_bridge_v2_native_startup_macos.py
tests/test_core_bridge_v2_restart_rotation_macos.py
tests/test_darwin_v0250_bridge_identity.py
tests/test_darwin_v0250_bridge_lifecycle.py
tests/test_darwin_v0250_connector_id_stage_cli.py
tests/test_darwin_v0250_installed_wheel_three_process_macos.py
tests/test_darwin_v0250_instance_runtime.py
tests/test_darwin_v0250_instances.py
tests/test_darwin_v0250_multi_connector_acceptance_source.py
tests/test_darwin_v0250_named_core_keepalive_live_macos.py
tests/test_darwin_v0250_native_validation.py
tests/test_darwin_v0250_openai_connector_ids.py
tests/test_darwin_v0250_owned_dynamic_keys.py
tests/test_darwin_v0250_owned_lifecycle.py
tests/test_darwin_v0250_owned_runtime.py
tests/test_darwin_v0250_owned_staging.py
tests/test_darwin_v0250_owned_unstage.py
tests/test_darwin_v0250_parallel_core_e2e.py
tests/test_darwin_v0250_shared_agent_adapter.py
tests/test_darwin_v0250_shared_agent_factory.py
tests/test_darwin_v0250_shared_agent_factory_ipc_macos.py
tests/test_darwin_v0250_shared_binding.py
tests/test_darwin_v0250_shared_cli_serve.py
tests/test_darwin_v0250_shared_core_bootstrap.py
tests/test_darwin_v0250_shared_interpreter_pin.py
tests/test_darwin_v0250_shared_lifecycle.py
tests/test_darwin_v0250_shared_manifest.py
tests/test_darwin_v0250_shared_native_live_preflight_macos.py
tests/test_darwin_v0250_shared_native_signed_gate_macos.py
tests/test_darwin_v0250_shared_owner_process_macos.py
tests/test_darwin_v0250_shared_owner_writer_preflight.py
tests/test_darwin_v0250_shared_port_inventory.py
tests/test_darwin_v0250_shared_preflight_ipc_macos.py
tests/test_darwin_v0250_shared_readiness.py
tests/test_darwin_v0250_shared_release_source.py
tests/test_darwin_v0250_shared_runtime_install.py
tests/test_darwin_v0250_shared_runtime_verify.py
tests/test_darwin_v0250_shared_smappservice_live_macos.py
tests/test_darwin_v0250_shared_smappservice_safety.py
tests/test_darwin_v0250_shared_staging.py
tests/test_darwin_v0250_shared_swift_gate.py
tests/test_darwin_v0250_shared_swift_gate_paths.py
tests/test_darwin_v0250_shared_two_core_http_macos.py
tests/test_darwin_v0250_shared_unstage.py
tests/test_darwin_v0250_wheel_archive_gate.py
tests/test_darwin_v0250_wheel_offline_preflight.py
```

### D. Agent Bridge code and tests (13 paths)

```text
agent_bridge/src/serverfs_agent_bridge/config.py
agent_bridge/src/serverfs_agent_bridge/core_grants.py
agent_bridge/src/serverfs_agent_bridge/macos_owned_config.py
agent_bridge/src/serverfs_agent_bridge/main.py
agent_bridge/src/serverfs_agent_bridge/protocol.py
agent_bridge/src/serverfs_agent_bridge/service.py
agent_bridge/src/serverfs_agent_bridge/store.py
agent_bridge/tests/test_core_grants.py
agent_bridge/tests/test_core_grants_macos_ipc.py
agent_bridge/tests/test_core_grants_protocol.py
agent_bridge/tests/test_core_grants_startup.py
agent_bridge/tests/test_core_grants_submission.py
agent_bridge/tests/test_macos_owned_config.py
```

### E. Architecture, documentation and evidence (63 paths)

```text
docs/architecture/v0.25.0-bridge-v2-macos-windows-reconciliation.md
docs/architecture/v0.25.0-instance-model.md
docs/architecture/v0.25.0-lifecycle-decoupling.md
docs/architecture/v0.25.0-macos-multiple-connector-ids.md
docs/architecture/v0.25.0-macos-shared-core-manifest.md
docs/architecture/v0.25.0-macos-shared-v2-integration-gates.md
docs/architecture/v0.25.0-native-platform-boundaries.md
docs/architecture/v0.25.0-shared-bridge-v2-protocol.md
docs/diagnostics/v0250-macos-cloudflare-connector-restart-acceptance-2026-11-16.md
docs/diagnostics/v0250-macos-cloudflare-cutover-2026-11-15.md
docs/diagnostics/v0250-macos-cloudflare-cutover-preparation-2026-11-15.md
docs/diagnostics/v0250-macos-connector-native-acceptance-2026-11-14.md
docs/diagnostics/v0250-macos-crossplatform-release-handoff-audit-2026-11-16.md
docs/diagnostics/v0250-macos-current-head-source-gates-2026-11-15.md
docs/diagnostics/v0250-macos-current-source-gates-after-native-negative-2026-11-15.md
docs/diagnostics/v0250-macos-current-wheel-fresh-venv-install-2026-11-16.md
docs/diagnostics/v0250-macos-dirty-source-offline-wheel-acceptance-2026-11-16.md
docs/diagnostics/v0250-macos-exact-commit-real-offline-install-2026-11-15.md
docs/diagnostics/v0250-macos-final-source-quality-gates-2026-11-15.md
docs/diagnostics/v0250-macos-fresh-wheel-three-process-e2e-2026-11-16.md
docs/diagnostics/v0250-macos-independent-installed-wheel-runtime-2026-11-15.md
docs/diagnostics/v0250-macos-installed-wheel-three-process-e2e-2026-11-15.md
docs/diagnostics/v0250-macos-instance-foundation-2026-11-13.md
docs/diagnostics/v0250-macos-keepalive-native-acceptance-2026-11-16.md
docs/diagnostics/v0250-macos-keepalive-native-rerun-2026-11-16.md
docs/diagnostics/v0250-macos-local-http-session-sse-acceptance-2026-11-16.md
docs/diagnostics/v0250-macos-named-instance-runtime-2026-11-13.md
docs/diagnostics/v0250-macos-named-openai-unstage-fix-2026-11-16.md
docs/diagnostics/v0250-macos-offline-isolated-wheel-record-2026-11-15.md
docs/diagnostics/v0250-macos-offline-wheel-preflight-2026-11-15.md
docs/diagnostics/v0250-macos-owned-dual-connector-acceptance-2026-11-14.md
docs/diagnostics/v0250-macos-owned-native-acceptance-2026-11-14.md
docs/diagnostics/v0250-macos-post-freshwheel-public-sse-full-regression-2026-11-16.md
docs/diagnostics/v0250-macos-post-presence-full-source-regression-2026-11-15.md
docs/diagnostics/v0250-macos-public-cloudflare-baseline-2026-11-15.md
docs/diagnostics/v0250-macos-public-cloudflare-sse-acceptance-2026-11-16.md
docs/diagnostics/v0250-macos-release-readiness-2026-11-15.md
docs/diagnostics/v0250-macos-release-readiness-2026-11-16.md
docs/diagnostics/v0250-macos-shared-core-lifecycle-batch-2026-11-15.md
docs/diagnostics/v0250-macos-shared-exact-commit-runtime-2026-11-15.md
docs/diagnostics/v0250-macos-shared-lifecycle-gates-final-2026-11-15.md
docs/diagnostics/v0250-macos-shared-lock-and-wheel-gates-2026-11-15.md
docs/diagnostics/v0250-macos-shared-native-bytecode-runtime-integrity-2026-11-15.md
docs/diagnostics/v0250-macos-shared-native-gate-2026-11-15.md
docs/diagnostics/v0250-macos-shared-native-reauthorization-2026-11-16.md
docs/diagnostics/v0250-macos-shared-native-revocation-acceptance-2026-11-16.md
docs/diagnostics/v0250-macos-shared-offline-runtime-native-preflight-2026-11-15.md
docs/diagnostics/v0250-macos-shared-preflight-provenance-2026-11-15.md
docs/diagnostics/v0250-macos-shared-presence-signed-preflight-retry-2026-11-15.md
docs/diagnostics/v0250-macos-shared-register-uncertain-2026-11-15.md
docs/diagnostics/v0250-macos-shared-runtime-site-integrity-2026-11-15.md
docs/diagnostics/v0250-macos-shared-smappservice-owner-restart-acceptance-2026-11-16.md
docs/diagnostics/v0250-macos-shared-staging-native-acceptance-2026-11-15.md
docs/diagnostics/v0250-macos-shared-stop-safe-2026-11-15.md
docs/diagnostics/v0250-macos-shared-three-process-acceptance-2026-11-15.md
docs/diagnostics/v0250-macos-signed-launcher-negative-preflight-2026-11-15.md
docs/diagnostics/v0250-macos-signed-preflight-residual-safe-cleanup-2026-11-15.md
docs/diagnostics/v0250-macos-source-full-regression-2026-11-15.md
docs/diagnostics/v0250-macos-two-shared-cores-real-smappservice-2026-11-16.md
docs/diagnostics/v0250-macos-v3-signed-owner-preflight-2026-11-15.md
docs/diagnostics/v0250-macos-wheel-gate-current-regression-2026-11-15.md
docs/diagnostics/v0250-shared-bridge-v2-foundations-2026-11-14.md
docs/diagnostics/v0250-shared-v2-owner-task-scope-2026-11-14.md
```

## F. Explicitly excluded generated scratch/build artifacts (55 paths)

These are path-only counts; no artifacts or environment contents were opened. Preserve them pending separate owner-directed cleanup. Do not include in candidate source staging.

- `deployment/macos/.serverfs-v0250-dirty-wheel-8c3lj1q_/` — 1 path.
- `deployment/macos/.serverfs-v0250-freshvenv-63P0cK/` — 2 paths.
- `deployment/macos/.serverfs-v0250-wheel-import-uNINb4/` — 1 path.
- `deployment/macos/.serverfs-v0250-wheelhouse-CusylLCY/` — 51 paths.
- Total excluded scratch/build artifacts: **55**.

## G. Ambiguous owner-held paths requiring review (5 paths)

Only prefixes/counts are shown. The candidate manifest does not expose exact names for these files, and their contents were not read. Preserve and have the owning maintainer classify each before deciding whether it belongs in any future candidate.

- `agent_bridge/src/serverfs_agent_bridge/*private.py` — 1 path.
- `agent_bridge/tests/*private.py` — 1 path.
- `src/serverfs_mcp/*private.py` — 1 path.
- `tests/*private*.py` — 2 paths.
- Total owner-held ambiguous paths: **5**.

## Public version metadata check

- `pyproject.toml` root project: `0.24.2`.
- `agent_bridge/pyproject.toml` Bridge project: `0.24.2`.
- `plugin.json`: `0.24.2`.
- `uv.lock` root package `serverfs-mcp`: `0.24.2`.
- `agent_bridge/uv.lock` package `serverfs-agent-bridge`: `0.24.2`.
- None of these metadata/lock paths is modified in this inventory. **v0.25.0 coordinated version updates are a future release gate** across all applicable package/plugin/lock surfaces and release-facing current-state metadata; this task made no version changes.

## Cross-platform review boundary

The accepted `docs/architecture/v0.25.0-native-platform-boundaries.md` is authoritative: preserve common public MCP behavior and security outcomes, but allow native per-OS service lifecycle, IPC, private state, serialization and supervision. Mac/Windows shared code or private protocol differences require a separate OS behavior/security review against the contract and each host’s own acceptance. Byte-identical implementation or private wire bytes are not a requirement absent demonstrated interoperability need. The historical reconciliation document remains historical evidence; its convergence recommendations are superseded by the ADR.

## Safe future source-staging recipe (not performed)

1. Freeze and record the agreed candidate base SHA and a separate candidate source SHA. Re-run `git status --porcelain -uall`, review every path with its owner, and resolve all G entries. Compare this report against that fresh inventory; this snapshot is not a substitute for review of subsequent changes.
2. Prepare explicit, reviewed path manifests for A–E, separated by platform/boundary if useful. Exclude F and any unresolved G entries. The path lists must be literal, exact paths with no directory-wide globs; preserve the source-vs-evidence disposition for each path.
3. In a disposable isolated worktree/checkout, construct the reviewed candidate tree from the agreed base plus only the reviewed source paths. Review staged names and staged diff against the manifests. Never use wildcard `git add -A`, never stage the dirty source worktree wholesale, and do not discard or clean omitted owner files.
4. After review, freeze the resulting candidate SHA. For an archival source snapshot, use `git archive <frozen-candidate-sha> <explicit-reviewed-path-1> <explicit-reviewed-path-2> ...` with the complete literal manifest; archive paths are repository-relative. Inspect the archive listing against that same manifest. Do not archive `HEAD` as if it contained unstaged dirty content: a frozen candidate SHA must first actually contain the reviewed inputs through an explicitly authorized integration workflow.
5. Only then run the release gates against that frozen candidate SHA, including coordinated `0.25.0` metadata and platform-specific behavior/security acceptance. This recipe describes a future review process; no staging, archive, commit, tests, builds, deployment or publication was performed here.

## Post-inventory path delta (added after the snapshot)

The original 239-path counts above deliberately remain a historical snapshot, **not** an automatically refreshed release manifest. Subsequent same-day macOS work introduced or changed the following release-relevant paths; a future clean candidate MUST re-enumerate all paths and explicitly reconcile these deltas:

- `deployment/macos/v0250_named_connector_lifecycle_acceptance.py` — already in category C, subsequently modified with opt-in no-network synthetic Connector KeepAlive acceptance.
- `tests/test_darwin_v0250_named_connector_keepalive_source.py` — **new candidate test path**, not included in the original category C count.
- `docs/diagnostics/v0250-macos-post-inventory-artifact-build-2026-11-16.md` — **new evidence path**, not included in category E count.
- `docs/diagnostics/v0250-macos-synthetic-connector-keepalive-2026-11-16.md` — **new evidence path**, not included in category E count.
- `docs/diagnostics/v0250-macos-cloudflare-intermittent-transport-readonly-2026-11-16.md` — **new read-only ingress diagnostic path**, not included in category E count.
- `deployment/macos/v0250_candidate_source_gate.py` and `deployment/macos/v0250_candidate_source_paths.txt` — **new deterministic source-selection tool and explicit path allowlist**, not in original C count.
- `tests/test_darwin_v0250_candidate_source_gate.py` — **new 13-test regression suite**, not in original C count.
- `docs/diagnostics/v0250-macos-reviewed-source-snapshot-dryrun-2026-11-16.md` — **new isolated source/Wheel evidence**, not in original E count.
- `docs/diagnostics/v0250-macos-version-metadata-isolated-dryrun-2026-11-16.md` — **new isolated `0.25.0` metadata/lock/Wheel feasibility evidence**, not a released artifact and not in original E count.
- `docs/diagnostics/v0250-macos-release-readiness-2026-11-16.md` — previously included in category E; further revised with the above results.
- This path-manifest report itself is a **new evidence path** created after the snapshot; this addendum modifies that report, without changing the original count.

The native app and both current `0.24.2`-metadata Wheels built offline and passed static integrity/source byte checks in a private task directory; those artifacts were removed and are not release inputs. The synthetic Connector automatic recovery test passed on native macOS (one test-owned PID crash; not real Cloudflare recovery). Full evidence is in the reports named above. Neither result changes the requirement for a reviewed `0.25.0` clean SHA, matching package metadata and each platform's native acceptance.

## Later source-dependency closure and private-named module review (2026-11-16)

**The original A–E-only manifest is not a valid standalone release candidate.** A read-only Python AST import audit found two G-listed implementation modules imported by A–E product code. They are mandatory source dependencies; omitting them would make a release candidate structurally incomplete even if unrelated components build successfully. The original 239-path snapshot and A–G counts above remain historical, not a claim of present completeness.

A limited AST/structural and known-credential-pattern inspection subsequently classified the five G paths as **candidate Python source/test files, not observed credential files**. The proposed reviewed inclusion set is:

- Root/macOS product source: `src/serverfs_mcp/darwin_shared_binding_private.py` — imported by Core shared-binding/bootstrap/manifest code; required for runtime.
- Bridge product source: `agent_bridge/src/serverfs_agent_bridge/core_grants_private.py` — imported by the Bridge entry point; required for runtime.
- Bridge tests: `agent_bridge/tests/test_core_grants_private.py` — private grants validation and refusal tests.
- Root/macOS tests: `tests/test_darwin_v0250_shared_binding_private.py` and `tests/test_darwin_v0250_shared_private_ipc_macos.py` — native private binding/IPC acceptance tests.

This review examined source structure and did not find known-format literal credentials, but cannot prove the absence of all secret representations. Final source ownership/content approval remains necessary before any release staging. Never treat a `private` filename as grounds either to suppress required code or to permit raw credentials into Git.

**Packaging gap fixed in the current dirty worktree:** `deployment/macos/v0250_wheel_archive_gate.py` now explicitly requires `serverfs_mcp/darwin_shared_binding_private.py` in Root Wheel, matching the preexisting Bridge Wheel requirement for `serverfs_agent_bridge/core_grants_private.py`. `tests/test_darwin_v0250_wheel_archive_gate.py` adds independent negative cases proving that either missing module is rejected even when the synthetic archive has a valid complete RECORD. Focused pytest **15 passed**, Ruff lint/format and `git diff --check` passed. Fresh offline dirty-source Root and Bridge builds both passed the updated wheel gate at metadata version `0.24.2`; each required module was present and byte-for-byte matched its local source. Only this task's private temporary build tree was cleaned. This is **not** evidence of clean SHA or a `0.25.0`-versioned release.

A subsequent exact-path candidate reconciliation MUST include these five paths and the post-snapshot changes before any isolated candidate integration; count them prospectively, not by silently rewriting the original snapshot. Do not stage or publish automatically.

## Later executable release-candidate input gates (2026-11-16)

This is a **later development checkpoint**, separate from the original frozen 239-path inventory above. The exact current changed-source allowlist is `deployment/macos/v0250_candidate_source_paths.txt`, checked by `deployment/macos/v0250_candidate_source_gate.py`. After correcting the test-only Darwin Unix socket path, compiling and installing two offline `0.25.0` Wheels, and enforcing narrow Ruff scratch exclusions, the gate reported **201 reviewed paths** with no staged/unreviewed source changes. The list now includes a modified root `pyproject.toml`, previously omitted macOS regression tests and diagnostics, and the five necessary private-named source/test paths. Any subsequently created file still requires explicit review and allowlist inclusion.

Only five exact release-metadata input names may bypass ordinary source-extension restrictions: root/Bridge `pyproject.toml`, root/Bridge `uv.lock`, and root `plugin.json`. No other TOML/JSON configuration is permitted. The retained `.serverfs-v0250-*` development scratch directories remain excluded by the source gate and Ruff `extend-exclude`; the full Root Ruff lint/format checks passed for **715 real Python files**, including 18 precisely formatted macOS v0.25 files whose ASTs remained identical. This is not an immutable Git commit, tag, production upgrade or Windows/Linux acceptance. See `v0250-macos-release-readiness-2026-11-16.md` and `v0250-macos-versioned-wheel-live-e2e-2026-11-16.md`.

## Verification and limits

- This document inventories names/status only. The previous handoff audit and native-first ADR were read; prior acceptance claims were not rerun or independently reproduced.
- No tests, builds, production changes, staging, checkout, fetch, push, tag, clean, removal or movement were performed.
- `git diff --check` and local whitespace sanity for this newly created report are the only checks run for this task.
- This is **inventory only**: it does not establish a clean SHA, exact-SHA acceptance, remote-tip freshness, or release readiness.
