# ServerFS MCP v0.25.0 — macOS two Shared Cores: live SMAppService acceptance

**Date:** 2026-11-16 (Mac Agent task execution). **Gate:** PASS for the current dirty-source candidate, not a release-tagged v0.25.0 package.

## Live acceptance

One explicitly opted-in native test was invoked through the preserved `macbook-serverfs-mcp` OpenAI Tunnel connection, on macOS Apple Silicon, using the existing `tests/test_darwin_v0250_shared_smappservice_live_macos.py`:

```sh
SERVERFS_ENABLE_SHARED_SMAPP_LIVE_TEST=1 .venv/bin/python -B -m pytest -p no:cacheprovider -q tests/test_darwin_v0250_shared_smappservice_live_macos.py
```

**Result: 1 passed**. The test registers and starts two uniquely named, real signed macOS SMAppService Shared Core login items against one independent disposable Bridge v2 owner, checks Core HTTP unauthorized 401/wrong-token 401 and authenticated MCP workdir discovery, verifies Core A task retrieval and Core B isolation, stops the first Core without stopping the Bridge or second Core, then stops the second Core and verifies production service integrity. Both random `shsm-*` native jobs and `/tmp/shsm-*` test scratch were absent after completion; no test-owned residual service state was reported.

Protected production launchd PIDs were unchanged across the test: `com.ntlx.serverfs.tunnel` **1779**, `com.ntlx.serverfs.bridge` **1790**, `com.ntlx.serverfs.http-origin` **40516**, `com.ntlx.serverfs.cloudflare` **40543**. Production jobs, credentials, DNS, Tunnel configuration and Git publication state were not modified.

## Predecessor failure and verified repair

The earlier real two-Core test failed after registering the first Core because Python's first normal interpreter start created `__pycache__/sitecustomize.cpython-313.pyc`, changing the strict managed `site-packages` inventory before the second Core could stage. This was isolated and reproduced. The fix explicitly adds `-B` to the Shared Core argv, with strict Python manifest and Swift SharedManifestGate validation; the subsequent source regression was Root/Agent Bridge full pytest exit 0, Ruff lint/format exit 0, 93 narrow tests passing. Refer to `v0250-macos-shared-native-bytecode-runtime-integrity-2026-11-15.md` for the failure audit. **The new real opt-in acceptance has now passed after this repair.**

## Still separate gates

- Registered Shared Core **Bridge-owner restart, grant rotation/revocation and reacquisition** in the same live test were not exercised by the baseline real-SMAppService acceptance above; separate process-level Bridge v2 tests have covered those behaviors.
- Exact release-SHA built/installed v0.25.0 wheel and cross-platform release gate remain pending (current project metadata is still 0.24.2).
- The current Cloudflare Connector has passed ChatGPT MCP filesystem CRUD and binary transfer. Native Agent task transport has passed; Agent workspace-tool authorization and SSE/session restart behavior still require separate positive E2E evidence.

No native test service, app or scratch from this acceptance is intended to remain running.
