# macOS v0.25.0 — Shared Core Bridge Owner Restart Acceptance

Date: 2026-11-16
Result: **PASS** for the isolated native Shared Core owner crash/restart gap.

The opt-in live test registered and ran two randomly named, signed Shared Core `SMAppService` items against one synthetic Bridge owner. After task isolation was established, the test stopped only the Bridge subprocess. Both Core HTTP `list_workdirs` endpoints remained available, both native Core registrations remained live, and signed Bridge RPC returned `CoreBridgeUnavailable`. Restarting the synthetic owner with the same configuration and state produced a new PID; both existing Cores then completed signed `task.submit` RPCs without Core restart. Each Core retrieved its own task and the other Core's task remained inaccessible.

## Evidence

- `SERVERFS_ENABLE_SHARED_SMAPP_LIVE_TEST=1 uv run pytest -q -s tests/test_darwin_v0250_shared_smappservice_live_macos.py`: **1 passed**, exit 0.
- `uv run pytest -q` on the SMAppService safety, Shared owner writer preflight, Shared lifecycle and Shared release source tests: **132 passed**, exit 0.
- Modified test: Python bytecode compilation, Ruff check and Ruff format check all passed.
- Protected launchd PIDs before and after, in test order (Tunnel, Bridge, Cloudflare, HTTP Origin): `(1779, 1790, 40543, 40516)`; unchanged.
- Post-run cleanup inspection: no `shsm-*` launchd rows and no `/tmp/shsm-*` scratch paths.

The test used only random `shsm-*` Core identities and its synthetic subprocess owner/state. No production owner or service was restarted. This acceptance covers the stated owner-process restart gap; it does not claim logout/login, reboot recovery, public Connector ingress, or a production deployment.
