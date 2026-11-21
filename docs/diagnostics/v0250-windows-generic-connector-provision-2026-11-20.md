# Windows v0.25.0 — generic initial Connector configuration (2026-11-20)

**Verdict: COMPLETE/PASS for fresh named private Connector configuration and independent disabled Scheduler registration.** Not public ingress, real Tunnel authentication, production migration or a credential rotation/upgrade.

## Source and artifacts

- deployment/windows/configure-v25-connector.py, SHA256 bfcaa16bc715fec472bd5d5eccfbeb48aa8070eb7dd28a0167a9f48f29d04892.
- tests/test_windows_v25_connector_provision.py, SHA256 1992f99f5f1dd1f6d69e754e9d2c60d9d7d886f917da58b2ab77e9df36971fbf.
- Reuses the adjacent configure-v25-private.py and the previously accepted installed Core, Native and Bridge 0.25.0 Wheels, unchanged.
- deployment/windows/v0250-native-operations.md updated with initial Connector steps and limitations.

The helper copies explicitly supplied private credential files and pinned binaries into a fresh per-Connector directory, validates credential envelopes, inherits the exact Core host/port, generates the OpenAI Bearer header from the Core's private policy, and publishes a new manifest only. Cloudflare and OpenAI external route and credential provisioning are not automated. The OpenAI bundled cloudflared binary is copied alongside tunnel-client.exe. Named generic Cloudflare audit identities and installed OpenAI Tunnel identities participate in duplicate checks. Five existing manual/legacy Cloudflare peers lacked generic identity audit records and were not checked for remote Tunnel identity; operators must verify them separately. Local provisioning checks require serial operation, not a cross-process reservation.

## Verification and failures

Actual local exact source hashes: 40 tests passed, using the extracted verified Core Wheel. Windows JUnit: 40 collected, 40 passed, exit 0. Two test-file syntax typos initially caused collection failures; one local batch edit was not fully reproduced on the remote file. After the final remote correction, exact downloaded bytes were hash-checked and retested. The original pending execution was cancelled before launch, and the first native attempt failed at test collection without configuration changes.

A subsequent native attempt passed 40 tests but stopped at the scratch pinned-inputs stage with PermissionError. Its audit does not establish the precise cause; no Connector was configured, baseline and cleanup verified. The final gate used a fresh report path/new fixture IDs and bounded retry for scratch audit replacement with redacted exception location/Win32 diagnostics. It completed without reproducing the PermissionError; this does not prove the original error's root cause.

## Actual native acceptance

Retired fixture Core: inst-v25-0190b734a911. Cloudflare A cf-a-97045fd3, Cloudflare B cf-b-97045fd3, OpenAI oa-97045fd3. Both Cloudflare owners share the component runtime, have independent identities and private files; OpenAI has a distinct runtime. All three: new config exit 0, repeat exit 2 with byte-unchanged private files, disabled native Scheduler registration and exact cleanup. Wrong binary pin and duplicate Cloudflare/OpenAI Tunnel identities refused before creating the proposed homes. No Connector launched. External credentials were synthetic; no actual Tunnel or public network attempted. All 40 existing task entries and production Core generation unchanged.

Binary inputs: previously pinned installed Cloudflare 2493dbc168923bb3e59e5e7716f71deced92992eea9acd6e9d74f84ac7751343; installed OpenAI tunnel-client.exe 61a770c759c386845f830a106f7e6d0d3f854b932259744a88b4013c282e64ec; its bundled cloudflared eb85d58854b21bf3e48b178377ada9521af58ecd947413afceb6abe718d3a71e. The latter two pins were captured from locally installed legacy binaries, not independently authenticated upstream release pins. Release distribution must establish appropriate binary provenance.

Evidence relative to candidate:
- .remember/tmp/v0250-win-versioned-python-20261120/generic-connector-provision-gate-20261120/audit.json
- .remember/tmp/v0250-win-versioned-python-20261120/generic-connector-provision-gate-fixed-20261120/audit.json
- .remember/tmp/v0250-win-versioned-python-20261120/generic-connector-provision-gate-audited-20261120/audit.json
- .remember/tmp/v0250-win-versioned-python-20261120/generic-connector-provision-gate-audited-20261120/pytest.xml
- .remember/tmp/v0250-win-versioned-python-20261120/scripts/generic_connector_provision_gate_audited_20261120.py

No changes to runtime Wheels, production configuration, original main checkout, or Git refs. Synthetic fixture files/venvs retained for diagnosis. The three generic operator entry points and their tests must be explicitly included in release-source selection. Public ingress and real Provider validation remain separate acceptance gates.
