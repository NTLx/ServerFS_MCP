# ServerFS MCP v0.25.0 — macOS real Shared Core grant revocation acceptance

**Date:** 2026-11-16, macOS local. **Status: PASS** for the current dirty-source v0.25.0 candidate (not a tagged release).

## Test scope

Real macOS native `SMAppService` opt-in test `tests/test_darwin_v0250_shared_smappservice_live_macos.py` creates and registers two randomly named, signed Shared Cores against a synthetic, independent Bridge v2 owner. It verifies signed per-Core task isolation and HTTP MCP file access, Bridge owner exit/restart while both Cores remain registered, and explicitly revokes Core B in the owner's offline-grant set before restarting the owner:

- Core A retains signed `bridge.ping` access and can retrieve its own task.
- Core B's existing signed credential is rejected with `CORE_NOT_AUTHORIZED`, including failure to retrieve its previous task through registered MCP.
- Core B's authenticated file-service `list_workdirs` remains functional while Agent authorization is revoked.
- Both Core login-item PIDs remain unchanged during revocation. No production Bridge is changed.
- Finally, both test-owned Core jobs are unregistered/cleaned; no residual `shsm-*` launchd job, process or test scratch remains.

## Positive native evidence

The explicit opt-in test was run **once** in the final verification with a private JUnit XML output. The native execution agent independently reported process exit **0**, and its test-generated JUnit XML was independently read using the existing OpenAI Tunnel ServerFS `read_text_file` tool:

- XML testsuite: `tests=1`, `failures=0`, `errors=0`, `skipped=0`.
- Test case: `tests.test_darwin_v0250_shared_smappservice_live_macos::test_two_real_smappservice_shared_cores_keep_owner_alive`.
- Test suite time **65.566 s**, test-case time **63.981 s**; XML timestamp **2026-11-16T08:38:57.649691+08:00**.
- XML contained no captured stdout/stderr, and was read and **deleted** by its exact revision after verification. Only sanitized evidence is retained in this Markdown report.
- Protected production launchd PIDs remained `(1779, 1790, 40543, 40516)` in order default OpenAI Tunnel, Agent Bridge, Cloudflare Connector, Cloudflare HTTP Core.
- Targeted source tests before live registration returned **132 passed**, Python/Ruff lint and format checks passed.

Two earlier opt-in invocations produced an isolated `.` and correct cleanup but did not expose an exit code. Those *were not counted* as passing results. This final JUnit run is the first independently readable proof of the revocation gate. No shell-output inference substitutes for the observed XML.

## Release limitations

This passes the real native Shared Core grant-revocation scenario. It does **not** establish logout/login or reboot recovery, public arbitrary-client SSE/resumption, post-revocation Core B credential rotation/re-provision, cross-platform identical release SHA, or final v0.25.0 publication. Both production connectors remain running, and no Cloudflare Tunnel/DNS/credentials or Git release metadata were modified.
