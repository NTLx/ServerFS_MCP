# Windows v0.25.0 — latest installed native opt-in and release selection (2026-11-20)

**Verdict: PASS for the latest pinned installed Windows supplemental gates and read-only source-selection proposal. Not production rollout or release approval.**

## Actual installed native regression

- Core Wheel SHA256: 688f6fdc04bd90c67a45ae5aa8d437faf69b88fde672c837540ed0d911320dd8
- Bridge Wheel SHA256: 8df1cde88129d25a146d6d92ebaa3ea5bcac81e3ccb5deacbeaf02233ffdbcdd
- Installed package payloads compared to the respective Wheels before the test run.
- All commands used isolated interpreters; tests used explicit fresh fixtures only; no real provider call or Tunnel.

| Scope | Passed | Skipped | Failed | Exit |
| --- | ---: | ---: | ---: | ---: |
| Private ACL, Job Object, Scheduler fixtures | 7 | 0 | 0 | 0 |
| Unique Scheduler runtime fixtures | 2 | 0 | 0 | 0 |
| Four independent components, disabled registration | 1 | 0 | 0 | 0 |
| Windows-dispatcher signed runtime-discovery tests | 7 | 0 | 0 | 0 |
| Total | 17 | 0 | 0 | 0 |

The first ten are Windows opt-in native fixtures repeated against the latest Core. The final seven are synthetic Bridge tests executed on Windows using the latest installed Bridge distribution; no provider or live Pipe connections in that file. They were added after the earlier full Bridge run of 761 passed/119 skipped. Do not represent their sum as a new full-suite run.

The prior latest three-Wheel I17 acceptance remains COMPLETE/PASS, cleanup_verified=true; it was not repeated. The four-component registration test does not prove live public ingress.

## Release source selection

Candidate HEAD: 01d3e81dc56637df461857cf8fa2da06ef66790f (local detached candidate; not a claim about current remote main).

At extraction: 20 modified tracked paths, 452 untracked paths, zero staged, 472 Git-visible changed paths. Groups: 47 runtime Python modules, 195 test files, 1 release tool, 205 deployment-review paths, 24 diagnostics docs, zero other. Literal recursive dependency analysis identified 128 of the 205 deployment scripts. The proposed review selection consists of 371 SHA256-labeled paths (47+195+1+128); 77 other deployment scripts and 24 historical diagnostics remain outside the initial selection, not deleted. Static filename matching cannot establish all dynamic dependencies, tracked callers or non-Python assets; the proposal is not a final allowlist or staging authorization.

After the frozen inventory was written, this turn added deployment/windows/v0250-native-operations.md and this evidence document, and modified deployment/windows/README.md to point to the new named-instance runbook. These three documentation paths are a pending manifest delta; rerun selection when the final source set is fixed. No runtime code, pinned Wheel, metadata, original checkout or Git refs changed in this turn.

## Evidence paths relative to the candidate

- .remember/tmp/v0250-win-versioned-python-20261120/scripts/final_native_and_source_selection_20261120.py
- .remember/tmp/v0250-win-versioned-python-20261120/scripts/latest_release_selection_20261120.py
- .remember/tmp/v0250-win-versioned-python-20261120/logs/release-selection-v2-20261120.json
- .remember/tmp/v0250-win-versioned-python-20261120/scripts/latest_wheel_native_optin_and_bridge_20261120.py
- .remember/tmp/v0250-win-versioned-python-20261120/cli-wiring-rebuilt-core-wheel-20261120/native-optin-cli-fixed-20261120.json

Both subordinate commands and their wrapper returned 0. Remaining release blockers include coordinated source integration, shared platform differences, actual 0.25.0 release metadata/lock synchronization, final immutable-build validation, public ingress acceptance and the outstanding I11-B provider exact-output gate. Named-instance private provisioning remains an operator prerequisite; legacy fixture scripts are not generic release installers.
