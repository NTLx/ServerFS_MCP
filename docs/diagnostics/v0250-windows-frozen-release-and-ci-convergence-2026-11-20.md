# Windows v0.25.0 — frozen release source and CI convergence (2026-11-20)

**State: Windows source/build and scoped lint gates PASS; final installed Bridge regression OPEN. No release authorization.**

## Source freeze and packaging

Candidate HEAD and read-only GitHub origin/main lookup both returned 01d3e81dc56637df461857cf8fa2da06ef66790f at freeze time. Root original dirty main and Git refs were unchanged. The frozen source contains 1,162 files (800 tracked baseline files plus 362 selected new files); 384 changed paths including windows-native.yml, 101 untracked diagnostic/evidence paths excluded from the review snapshot but retained locally. Eight version metadata files were changed in the snapshot only, synchronizing ten independent version declarations to 0.25.0. Both repository release metadata gates and both uv lock --check --offline commands passed. Candidate version files remain 0.24.2 pending coordinated multi-platform integration.

| Artifact built offline | SHA256 |
| --- | --- |
| Core Wheel | 6308f00cc9db55a8695caa4072ffac61a6fc81aacc2efb806d2a9333363f44a8 |
| Bridge Wheel | 8df1cde88129d25a146d6d92ebaa3ea5bcac81e3ccb5deacbeaf02233ffdbcdd |
| Native ABI3 Windows x64 Wheel | 5b796e1d875def29657fcca42df5adf79abf8157a8fc821d9b135573baeecac6 |
| Agent Plugin ZIP | 717ab4faa9b89d7a9ef5842d4cd3a31b7d823705f92e57fc494b1ab5ee549178 |

Core package 140/140 members unchanged vs previously accepted wheel, Bridge 51/51 unchanged (whole ZIP identical). Core archive adds dist-info/licenses/LICENSE with SHA256 92a0f47148e6149706c3e4da1495fa5f7161f284161ef0ecc651027762570a3c matching frozen tracked LICENSE; other changes only METADATA License-File and RECORD. All three wheel RECORD inventories and per-member SHA256/size verified locally with zero failures. Cargo --locked --offline metadata and official three-wheel metadata gate exit 0. New native pyd SHA256 37465294e687a773edba8ebc12ed403f1541a3035039d3b50ae47b38ebe373b1; compared with prior accepted pyd, both 407040 bytes, only 32 bytes differ: PE/three debug timestamps (4 bytes each) and CodeView GUID (16). Equal after normalizing those exact five ranges in analytical copies; normalized SHA256 c062a48310a21c6367c0618bcbc58ca3d3fc2deed303a549bc5f2a27af54ef40. No released binary was modified. Bit-for-bit reproducibility remains unproved.

## Installed regression and ACP follow-up

Two fresh scratch noneditable environments installed the three exact wheels offline. Core/native: 200 test files including 142 v25 files, 1,864 passed/15 skipped/0 failed, exit 0. Bridge: 767 passed/119 skipped/1 failed, exit 1. Failure: test_acp_adapter.py::test_reconciliation_requires_measured_load_capability, AGENT_PROVIDER_STOP_UNKNOWN in Windows ACP process-tree shutdown proof. Both post-origin checks and postflight package/source byte parity passed. Overall combined regression exit 2; do not label it PASS.

Three targeted synthetic repetitions using the same installed Bridge and instrumented stop proof each returned exit 0, no known live PID, no discovered child, no retained handle, result true. Original failure not reproduced; root cause unestablished. No ACP lifecycle weakening, retry-as-success, or provider change. New full regression after diagnosis is required; public ingress and I11-B exact real-provider gate remain independent.

## CI convergence

windows-native.yml now triggers on windows_v25/**, agent_core_grant_client.py, deployment/windows/**, and auto-runs all test_windows_v25_*.py. windows-agent.yml triggers on the new Bridge paths and includes real v2 worker tests after checking separate Bridge worker interpreter existence. Both exact YAML files parsed locally; actual GitHub Actions not executed.

Ruff 0.16.8 initially unavailable in offline tool cache; reused existing same-version original WorkPC .venv/Scripts/ruff.exe read-only. Initial check silently ignored all scratch files due parent Git ignore (warning preserved), then explicit --no-respect-gitignore and --show-files verified 800 actual scanned files. Before repair: 24 lint violations in 9 paths (17 deployment, 7 tests), 38 formatting paths. All violations and formatting targets were within previous selected changed paths; no runtime module errors.

New style overlay copied frozen source, applied narrowly reviewed B904/B007/E731 changes, safe I001 fixes, AST-invariant formatter. Full Ruff check and format exit 0. Affected Core tests 258 passed, Bridge 7 passed, zero failures/skips. Exact 38 verified changed paths promoted to candidate with preimage and postimage SHA256 guards; old frozen snapshot unchanged. No runtime product package source change. Formatting changed original test/helper pins; earlier historical evidence remains valid for its original bytes. Latest candidate requires refresh of frozen selection hashes and full regression; style overlay test results are not a new whole-native deployment proof.

## Evidence paths (relative to candidate)

- .remember/tmp/v0250-win-versioned-python-20261120/release-snapshot-freeze-20261120/audit.json
- .remember/tmp/v0250-win-versioned-python-20261120/release-snapshot-freeze-20261120/source-manifest.json
- .remember/tmp/v0250-win-versioned-python-20261120/release-snapshot-native-regression-20261120/audit.json and both JUnit files
- .remember/tmp/v0250-win-versioned-python-20261120/release-snapshot-lint-acp-diagnostic-20261120/audit.json
- .remember/tmp/v0250-win-versioned-python-20261120/release-snapshot-ci-lint-verified-20261120/audit.json
- .remember/tmp/v0250-win-versioned-python-20261120/release-style-scoped-repair-20261120/audit.json and both affected JUnit files

No production service, real provider, public Connector, credentials, original checkout, Git staging/commit/push/tag or release artifact publication. Release workflow requires a coordinated docs/releases/v0.25.0.md (absent from the frozen candidate), multi-platform CI, current main SHA and explicit operational acceptance. The second Windows-only CI workflow and this handoff doc are new source selection deltas outside the historical frozen snapshot.
