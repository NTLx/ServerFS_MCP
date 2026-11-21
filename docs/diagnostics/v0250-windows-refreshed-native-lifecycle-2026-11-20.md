# Windows v0.25.0 — refreshed native lifecycle and I17 acceptance (2026-11-20)

**Verdict: COMPLETE/PASS for the refreshed exact three-Wheel native fixture and I17 lifecycle gates. Not release or production authorization.**

## Artifact identity

| Distribution | SHA256 |
| --- | --- |
| Core 0.25.0 | 6308f00cc9db55a8695caa4072ffac61a6fc81aacc2efb806d2a9333363f44a8 |
| Bridge 0.25.0 | d262552bde08c926e510fd5187fa83b8c1cf05f3bb7650bf9c7c8072a5642aa1 |
| Windows Native 0.25.0 | 5b796e1d875def29657fcca42df5adf79abf8157a8fc821d9b135573baeecac6 |

Source: the 1,163-file versioned reviewed snapshot, validated by RELEASE_REFRESH_ACP=PASS. See [ACP and refreshed build/regression](v0250-windows-acp-job-and-refreshed-release-2026-11-20.md). The 2,636 distinct full-suite passes, 134 skips, zero failures and 24 repeated ACP stress executions remain separate evidence. Native lifecycle uses those exact artifact hashes; no new package was built.

## Initial failure and diagnosis

The first native gate passed seven ACL/Job/Scheduler fixture cases and two real Scheduler runtime cases. Aggregate exit 2 because protected baseline comparison changed after unique_scheduler_runtime. I17 did not start. This run did not store the per-name baseline preimage and therefore does not identify which protected attribute or generation changed. The exact original cause remains unknown; neither a production mutation nor its absence during that first interval is proved by the incomplete audit.

Read-only diagnostic then sampled all 40 existing ServerFS task entries and production Core identity three times: exact identical name/state/enabled inventory, identical Core generation, zero residual W2 runtime or Pythonw launcher task names. The original test reports show both W2 runtime cases passed and their exact fixture cleanup assertions passed. No state-changing corrective action was taken.

New v2 gate pinned its preflight to the recorded stable diagnostic and persisted full preimage, individual per-phase observations/deltas and postflight. It retained strict refusal on any change (no allowed transient, retry-as-success or skipped protected entry).

## Verified v2 native gate

| Scope | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| Private ACL, Job Object, Scheduler | 7 | 0 | 0 |
| Independent Scheduler runtime | 2 | 0 | 0 |
| Four-component disabled registration | 1 | 0 | 0 |
| Bridge signed runtime discovery | 7 | 0 | 0 |
| Total | 17 | 0 | 0 |

All four installed-package origin checks passed. All four per-phase baseline comparisons and the final postflight recorded zero task changes and unchanged production Core generation.

## Fresh I17

Nonproduction instance i17-v25-ccad26367b75, loopback port 18892. Three independent offline, ACL-protected venvs installed exact pinned Wheels. Two fresh disabled Task Scheduler entries registered by exact ownership and then activated. Actual authenticated MCP and signed Repo Ops succeeded. Controlled Bridge stop left the same Core process serving file reads while Repo Ops failed closed. Independent Bridge restart restored Repo Ops. Held and revalidated test Bridge owner handle terminated, Scheduler established a new generation, and same Core process served signed Repo Ops again. All 17 I17 markers passed, including both exact fixture task removals; port absence and protected production state/generation verified. Fixture private artifacts retained for diagnosis.

REFRESHED_I17=PASS, REFRESHED_NATIVE_LIFECYCLE=PASS, wrapper exit 0; cleanup_verified=true, source_hashes_unchanged=true, installed_payloads_unchanged=true, protected_baseline_unchanged=true.

## Evidence paths

Relative to candidate .remember/tmp/v0250-win-versioned-python-20261120/:
- refreshed-native-lifecycle-gate-20261120/audit.json (failed initial gate; preserve)
- native-baseline-diagnostic-20261120/audit.json (read-only stable preimage)
- refreshed-native-lifecycle-gate-v2-20261120/audit.json and four JUnit XML files
- refreshed-wheels-i17-20261120/audit.json
- scripts/refreshed_native_lifecycle_gate_v2_20261120.py
- scripts/refreshed_wheels_i17_20261120.py

Only isolated fixture lifecycles and ignored scratch files were created. No production lifecycle mutation, Git staging/commit/push/tag, real Provider call, public Tunnel launch or external network operation.

## Release limitations

This closes the latest Wheel Windows native lifecycle revalidation gate, including the ACP-updated Bridge. Earlier generic provisioning, direct HTTP, upgrade/rollback and public/Provider histories use their own recorded artifact pins and are not relabeled. The historical I11-B exact response gate remains open. Public Cloudflare/OpenAI acceptance requires dedicated nonproduction routes and verified credential/binary inputs; historical disabled I7 and I11 fixtures must not be assumed to contain the latest packages. POSIX regression of shared ACP setup-failure cleanup, three-platform source integration and CI, shared version synchronization and coordinated release notes are pending. Prior Rust bit-for-bit reproducibility remains unproved. No automatic production rollout.

The present handoff and updated operations document are documentation-only deltas to the prior frozen source; include them explicitly in the coordinated release selection.
