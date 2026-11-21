# Windows v0.25.0 — integration handoff and commit preparation (2026-11-21)

**Verdict: PASS for the 396-file candidate source preflight; release and production rollout remain unauthorized.** This document records the final read-only audit, not a new build/test run or a merge approval.

## Source and Git identity

- Candidate worktree: `.v0250-win-merge-candidate` (detached at audit time).
- Candidate HEAD, local origin/main and live remote main at audit: `01d3e81dc56637df461857cf8fa2da06ef66790f`. Live `git ls-remote` exit 0; same baseline, no base-to-main changed path overlap.
- Proposed independent branch: `windows/v0.25.0-checkpoint-20261121`. Neither local nor remote branch existed at audit time.
- Final source selection: `.remember/tmp/v0250-win-versioned-python-20261120/refreshed-i11-release-selection-20261121/release-selection.json`; SHA-256 `12216a47017b73858ebad15f7a6e8d5698ae35fa2f8aeafe05ff44108169daf9`.
- This selection supersedes the 392-path ACP build selection: 391 previous entries byte-identical, one operations document updated, four refreshed acceptance documents added. No new package was built. The proposed full source manifest contains 1,167 files, including baseline files and snapshot-only version metadata.
- Initial Claude audit `agt_238b0d6439efb710fbdcdb7a` interrupted on interaction approval timeout without final result or report. Independent narrow audit `agt_57ceeb639ed36be643118c4a` succeeded, executable exit 0. Audit script was placed under ignored scratch `scripts/commit_preparation_20261121.py` in the evidence directory.

| Fresh source preflight | Result |
| --- | --- |
| Selected path SHA-256 matches | 396 / 396 |
| Missing, unsafe or mismatching selected paths | 0 |
| Tracked changed paths | 29, all selected |
| Untracked paths | 468, of which 367 selected and 101 excluded |
| Selected paths not Git-visible as changed | 0 |
| Staged paths at preflight | 0 |
| git diff --check HEAD | exit 0, no output |
| Six-category bounded secret signature scan | 0 findings |

Secret result is a limited preflight (PEM, GitHub, OpenAI, AWS, Slack and inline database credential patterns); it is not proof that arbitrary secrets, nonstandard tokens, or unpublished operational details are absent. Do not stage runtime caches, credentials, ignored evidence, or the 101 excluded untracked paths. Review any additional paths explicitly.

## Accepted Windows artifacts

Exact previously frozen artifacts (no rebuild or reinstallation in this audit):

| Artifact | SHA-256 |
| --- | --- |
| Core 0.25.0 wheel | `6308f00cc9db55a8695caa4072ffac61a6fc81aacc2efb806d2a9333363f44a8` |
| Bridge 0.25.0 wheel | `d262552bde08c926e510fd5187fa83b8c1cf05f3bb7650bf9c7c8072a5642aa1` |
| Native ABI3 Windows x64 wheel | `5b796e1d875def29657fcca42df5adf79abf8157a8fc821d9b135573baeecac6` |
| Agent plugin ZIP | `717ab4faa9b89d7a9ef5842d4cd3a31b7d823705f92e57fc494b1ab5ee549178` |

Historical acceptance, tied to the above recorded artifact pins rather than this source audit:

- [ACP and refreshed release regression](v0250-windows-acp-job-and-refreshed-release-2026-11-20.md): 2,636 distinct full-suite passes, 134 skips, zero failures; 24 repeated ACP executions separately. Core/Bridge installed payload parity, wheel RECORD, offline locks and Ruff checked.
- [Native lifecycle and I17](v0250-windows-refreshed-native-lifecycle-2026-11-20.md): 17 native tests, independent Bridge lifecycle and signed Repo Ops; cleanup and protected production baseline verified in the final gate.
- [Dual Cloudflare public gate](v0250-windows-refreshed-public-cloudflare-2026-11-20.md): two independent nonproduction connectors and file-only Core, authenticated public MCP, negative Auth/Origin checks, stop/restart isolation and restoration.
- [I11 no-model isolation and OpenAI readiness](v0250-windows-refreshed-i11-no-model-and-openai-readiness-2026-11-21.md): two fresh independently owned native pairs and signed pipe isolation, no real model calls.
- [Generic runtime install](v0250-windows-generic-runtime-install-2026-11-20.md), [private provisioning](v0250-windows-generic-private-provision-2026-11-20.md), [connector provisioning](v0250-windows-generic-connector-provision-2026-11-20.md): previous acceptance with their own stated pins.
- Shared POSIX ACP process-stop smoke was independently exercised in a previous operator check; full POSIX adapter/suite remains a cross-platform CI requirement.

## Cross-platform boundary

The candidate already descends from the merged macOS baseline. Same remote baseline does not establish safety against unmerged Linux or macOS changes. Preserve each platform's native implementation and review shared seams before combining platform branches. See the selected [frozen release and CI reconciliation](v0250-windows-frozen-release-and-ci-convergence-2026-11-20.md). In particular:

- Core `agent_client.py`, `agent_core_grant_client.py`, `agent_lifecycle.py`, `native_lifecycle.py`, `supervisor.py` and `cli.py`: maintain platform dispatch and lifecycle compatibility.
- Bridge `config.py`, `main.py`, `protocol.py`, `service.py`, `store.py`, `adapters/claude.py` and `adapters/acp.py`: keep macOS protocol activation, Windows named-pipe/native grant policy and Linux host-Bridge semantics distinct; do not overwrite a platform-specific path with another implementation. Reconcile macOS policy-digest validation and partial-stream handling against accepted contracts.
- Windows protocol/native and scheduler/job modules, scripts and tests remain native Windows concerns. Both updated Windows workflow files should be evaluated with the final shared CI configuration.
- Core and Bridge candidate project versions remain 0.24.2; eight version metadata files were changed only inside the frozen 0.25.0 build snapshot. Coordinate shared version/lock/manifest synchronization and release notes once, across platforms. Do not force the frozen metadata into this Windows-only checkpoint.

## Independent commit preparation

This handoff is the sole proposed additional path beyond the 396-path selection; exact intended diff is 397 paths. The 396-entry manifest stays immutable and this report is not a new source freeze. Verify the handoff hash separately, ensure candidate HEAD and selection are unchanged, create the proposed local branch in the candidate only, and stage by exact literal path list (never `git add -A`). Confirm staged path set equals the selection plus this report, no unexpected unstaged tracked changes, and `git diff --cached --check` passes. Checkpoint commit and push require a separate action. Do not modify main or other worktrees, deploy, tag, or publish.

## Remaining release gates

1. Full cross-platform CI and semantic integration of shared modules with current Linux/macOS development.
2. Strict I11-B actual-provider output/isolation acceptance; previous exact-response failure remains open. A previously blocked execution must not be bypassed.
3. OpenAI live public ingress using independently verified nonproduction Tunnel identity and matching binary pins; no production credentials/routes reused.
4. Coordinated version metadata, dependency locks, release notes and official release build/verification. Native Rust bit-for-bit rebuild reproducibility remains unproved.

Windows source checkpoint preparation is separable from these release gates. Do not relabel the pending gates as PASS or imply production deployment approval.
