# Windows v0.25.0 — generic initial private configuration (2026-11-20)

**Verdict: COMPLETE/PASS for three fresh named deployment topologies with actual Windows native lifecycle and HTTP MCP validation.** Not a production migration, public ingress, or real provider proof.

## Source and accepted installation artifacts

- deployment/windows/configure-v25-private.py; SHA256 3ade8ddb95365f25d833e6b2d8fea012b6a5f6d6dbbd25417c2585eaaac078d2
- tests/test_windows_v25_private_provision.py; SHA256 ce13664599fbc50e11b43e1ef2743705863229c50cb8c42ff1b60df41d991e95
- deployment/windows/v0250-native-operations.md updated.
- Uses previously accepted install-v25-runtime.py and exact Core 688f6fdc04bd90c67a45ae5aa8d437faf69b88fde672c837540ed0d911320dd8, Native 79328bb990279059d632a04a41fa48996eb18fcbb4dd0930788ed7d971c66e11 and Bridge 8df1cde88129d25a146d6d92ebaa3ea5bcac81e3ccb5deacbeaf02233ffdbcdd 0.25.0 Wheels. No package bytes changed.

The helper takes existing operator-authored native TOML policy, checks each runtime's accepted installation audit, fresh private locations, identity, port and task availability, known Core/Bridge workdir boundaries, then generates private bearer and initial signed bindings/grants. It derives Bridge policy revisions in the actual installed worker, enforces exact effective per-Core scope, publishes component manifests exclusively, and does not register or start tasks. No grant replacement, live append, rotation, automatic rollback or central manager. Bearer-only, nonproxy runtime policy; shared runtime settings must match. Separate manual path remains for OAuth/specialized settings.

## Verification

Local portable tests using extracted exact installed Core Wheel: 28 passed. Actual Windows installed Core controller: JUnit 28 collected, 28 passed, exit 0.

Main native gate, code 0, COMPLETE/PASS:
- Owned: inst-v25-0190b734a911 (previously accepted fresh generic install fixture), port 18920. Generated manifest/config/grants, repeat refusal with byte-unchanged inputs, authentic native Bridge/Core launch and signed HTTP Repo Ops.
- File-only: cfg-file-f87c4b79d7, port 18921. Fresh runtime installation, private config, repeat refusal, native Core launch with MCP file read and absent inspect_repo.
- Shared: cfg-owner-f87c4b79d7 owns distinct cfg-a-f87c4b79d7 (18922) and cfg-b-f87c4b79d7 (18923). Fresh runtime installs, distinct scoped config, repeat refusal, three independent native tasks started, both Cores read their own fixture and perform signed Repo Ops; each rejects the other's file alias.
- All six exact fixture tasks removed, listener ports released, all 40 pre-existing task state entries and production Core generation unchanged. All test configuration/venvs retained privately.

The initial file-only smoke used an incorrect Agent tool name (agent_submit_task). This is an oracle typo, not a product defect; do not rely on that assertion. Separate native follow-up on the exact retired file-only fixture returned COMPLETE/PASS with actual MCP list_tools count 11, all ten actual Agent tools absent, inspect_repo absent, file read PASS, cleanup/baseline PASS. The same follow-up independently checked shared persisted bindings' distinct secrets and key IDs, and exact distinct alias digests, without outputting any secret.

## Evidence (relative to candidate)

- .remember/tmp/v0250-win-versioned-python-20261120/scripts/generic_private_provision_gate_20261120.py
- .remember/tmp/v0250-win-versioned-python-20261120/generic-private-provision-gate-20261120/audit.json
- .remember/tmp/v0250-win-versioned-python-20261120/generic-private-provision-gate-20261120/pytest.xml
- .remember/tmp/v0250-win-versioned-python-20261120/scripts/generic_private_provision_file_surface_20261120.py
- .remember/tmp/v0250-win-versioned-python-20261120/generic-private-provision-gate-20261120/file-only-exact-surface.json

No real provider, Tunnel, production cutover, Git mutation, or new release metadata synchronization. Include both generic installer/configuration helper paths, their tests and operator runbook explicitly in final source selection. Remaining ingress/private connector setup, coordinated immutable release integration and provider/public ingress acceptance are separate gates.
