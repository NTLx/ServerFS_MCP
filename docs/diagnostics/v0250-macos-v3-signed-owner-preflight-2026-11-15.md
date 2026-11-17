# macOS v0.25.0 — signed Shared Core owner preflight integration

Date: 2026-11-15. Scope: one opt-in, non-registering native integration test. No source changes, production service mutations, or Git publication occurred.

## Preconditions and execution

Immediately before execution, fresh `launchctl print gui/501/<label>` inspections found exactly the four expected protected services. Each was `running`, `active count = 1`, with the audited PID. Process start times and command identities were independently captured:

| Service label | PID | Process start time |
| --- | ---: | --- |
| `com.ntlx.serverfs.tunnel` | 1779 | Tue Nov 10 08:26:31 2026 |
| `com.ntlx.serverfs.bridge` | 1790 | Tue Nov 10 08:26:31 2026 |
| `com.ntlx.serverfs.cloudflare` | 99279 | Thu Nov 12 14:39:38 2026 |
| `com.ntlx.serverfs.http-origin` | 99271 | Thu Nov 12 14:39:36 2026 |

No `shpf-*` launchd jobs or candidate instance directories were present before the run. The test's signed-live-preflight opt-in was set to `1` for this invocation; the SMAppService live registration test opt-in was explicitly unset. No other matching live/registration toggle was set in the invoking environment.

Executed exactly one pytest node:

```text
env -u SERVERFS_ENABLE_SHARED_SMAPP_LIVE_TEST SERVERFS_ENABLE_SHARED_SIGNED_LIVE_PREFLIGHT_TEST=1 .venv/bin/python -m pytest -q tests/test_darwin_v0250_shared_native_live_preflight_macos.py::test_two_signed_shared_cores_preflight_against_one_live_owner_without_registration
```

Result: **1 passed, 0 failed, pytest exit code 0**.

The test built and verified two randomly named signed inactive Shared Core candidates and exercised the real `shared-preflight` API/CLI, including native launcher `--validate-only`, against one disposable local Bridge v2 owner. It also verified provenance-mode refusal and recovery, owner unavailability/restart, revocation denial for Core B while Core A remained authorized, and unchanged protected production PIDs. No `shared-start`, ServiceManagement register/start/stop/unregister operation, Cloudflare request, or production service mutation was performed.

## Independent cleanup verification

After pytest exited, fresh launchd queries again showed all four protected labels `running`, `active count = 1` and at their original PIDs. A second `ps` inspection showed the same four process start times and command identities listed above. Searches found no `shpf-*` launchd jobs, instance directories, or `/tmp` scratch directories. The test's `unstage_shared_core` cleanup completed without reporting an error.

## Limits

This is preflight and authorization evidence only. It does **not** establish acceptance for two Shared Cores registered as macOS Login Items, nor does it establish a published v0.25.0 release. No commit, push, tag, deployment, or release publication was performed.
