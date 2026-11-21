# Windows v0.25.0 — generic offline runtime installation (2026-11-20)

**Verdict: COMPLETE/PASS for fresh named x64 runtime provisioning.** Not a production deployment, a full installer for policies/grants, or an in-place upgrade.

## Source and pinned artifacts

- New operator helper: deployment/windows/install-v25-runtime.py (SHA256 7975b0affeb1ac748d448a142fe71ab2e1a9c853a985941f95bd8c8a9294d367).
- New portable regression: tests/test_windows_v25_runtime_installer.py (SHA256 cfe20fdd3d9cee16213e2ee12d286164c98e8a9fb604ddb53483102c0ee0b037).
- Updated deployment/windows/v0250-native-operations.md.
- Existing accepted Core, Native, Bridge Wheels unchanged:
  - Core 688f6fdc04bd90c67a45ae5aa8d437faf69b88fde672c837540ed0d911320dd8
  - Native 79328bb990279059d632a04a41fa48996eb18fcbb4dd0930788ed7d971c66e11
  - Bridge 8df1cde88129d25a146d6d92ebaa3ea5bcac81e3ccb5deacbeaf02233ffdbcdd

Generic helper uses a trusted installed 0.25.0 Core/Native controller, validates three release Wheel pins and archive metadata, uses an existing operator-owned uv cache, enforces offline/copy and no project config, creates only new empty private environments, checks private ACLs/regular file ownership/no multiply-linked installed files, checks installed bytes and import origin/version, and records a private one-shot redacted audit. The Connector venv belongs to a component+instance, not to each connector ID. No manifest or Scheduler mutation, no implicit credential or grant provisioning. Transitive dependencies are taken from the trusted offline cache, not individually pinned by this helper.

## Failure evidence and fixes

1. First disposable run inst-v25-d403f93c22e3: wrong hash refusal before instance creation PASS; Core venv created, offline package installation FAILED. Protected 40-task baseline and production generation unchanged. Retired fixture preserved.
2. Offline dry-run matrix against that exact failed venv: strict env exit 1 (no solution/cache/network); sole operator uv variable UV_CACHE_DIR restored with --no-config, exit 0; both configured variants exit 0. Corrected only cache preservation/validation, not network or configuration policy.
3. Second gate ended before installation due to portable regression guard, with protected baseline unchanged. JUnit-instrumented gate reported 21 pass / 1 fail: Windows ZipInfo normalization removed the intended raw backslash from the fixture. Corrected fixture to assign raw member name after ZipInfo construction; added production comparison of orig_filename and filename before archive acceptance. Local 22 passed; actual three Wheels passed exact metadata and hash checks. No historical audit changed.

## Actual Windows acceptance

Fresh instance inst-v25-0190b734a911 (not registered). Final gate pinned both source hashes and all three Wheel hashes. Isolated Windows pytest: 22 cases collected, 22 passed, 0 skipped/failed, exit 0. Bad-hash request again refused before creating a new instance.

| Component | Environments installed | Exit | Repeat refusal |
| --- | --- | --- | --- |
| Core | core | 0 | PASS |
| Bridge | bridge, agent-bridge | 0 | PASS |
| Cloudflare | cloudflare | 0 | PASS |
| OpenAI | openai | 0 | PASS |

All five environments verified, all four component manifests absent, repeat calls exit 2 and original audits unchanged. Native gate COMPLETE/PASS, code 0. All 40 pre-existing task states and original Core generation verified unchanged. No test component task or provider/tunnel start. New private venvs and failed-attempt artifacts retained for inspection; explicit future cleanup required, no broad deletion performed.

## Evidence paths relative to candidate

- .remember/tmp/v0250-win-versioned-python-20261120/fresh-component-runtime-install-gate-20261120/audit.json
- .remember/tmp/v0250-win-versioned-python-20261120/fresh-component-runtime-install-gate-20261120/offline-dryrun-diagnostic.json
- .remember/tmp/v0250-win-versioned-python-20261120/fresh-component-runtime-install-cache-fixed-20261120/audit.json
- .remember/tmp/v0250-win-versioned-python-20261120/fresh-component-runtime-install-final-20261120/audit.json
- .remember/tmp/v0250-win-versioned-python-20261120/fresh-component-runtime-install-final-20261120/pytest.log
- .remember/tmp/v0250-win-versioned-python-20261120/fresh-component-runtime-install-wire-fixed-20261120/audit.json
- .remember/tmp/v0250-win-versioned-python-20261120/fresh-component-runtime-install-wire-fixed-20261120/pytest.xml

Remaining: generic private HTTP inputs and signed Bridge grant/binding provisioning, coordinated release selection and immutable final build, installed connector/public-route and real-provider acceptance. The deployment helper and test must be explicitly included in the next release manifest selection (the previous literal v0250-* script heuristic is not enough). No runtime package, shared metadata, original main checkout, production configuration or Git refs modified here.
