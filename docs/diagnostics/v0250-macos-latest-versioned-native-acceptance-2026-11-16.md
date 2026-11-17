# macOS v0.25.0 same-source installed-wheel Shared Core acceptance — 2026-11-16

## Result

**PASS — one signed native two-Shared-Core SMAppService test passed.** The test's independent numeric exit was **0**; its retained JUnit records **1 test, 0 failures, 0 errors, 0 skipped**. Runtime was **86.341 seconds**, below the 420-second hard timeout. No native-test retry was made.

This is local acceptance of a private dirty-source candidate, not a release or an exact-SHA platform claim.

## Candidate and wheel provenance

The candidate was `/Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-candidate-7d31f8a2c04e`, canonical, UID **501**, mode **0700**, with no `.git`. The pre-existing snapshot had no symlinks across **880** nodes, and all nodes were current-user owned with no group/world-writable directories. Its Root, Bridge, Windows Cargo, Plugin, registry and local lock metadata passed `release_metadata.py --root <candidate> --tag v0.25.0`. The unchanged checkout separately passed the same checker at **v0.24.2**.

The snapshot's proof Wheels passed the complete archive/RECORD gate:

| Wheel | SHA-256 | Members | Uncompressed bytes |
| --- | --- | ---: | ---: |
| `proof/root/serverfs_mcp-0.25.0-py3-none-any.whl` | `0a0e122157d459defe99dde139fec521ad85ec8adae1d10be30b785b9bad18f3` | 113 | 1,445,427 |
| `agent_bridge/deployment/macos/.serverfs-v0250-candidate-7d31f8a2c04e/proof/bridge/serverfs_agent_bridge-0.25.0-py3-none-any.whl` | `80721da07b18adedff5bf0244f5d17599aff5389555ded1acb48c74ea2b4ab79` | 54 | 767,102 |

The nested Bridge Wheel was independently compared member-by-member against this candidate's `agent_bridge/src/serverfs_agent_bridge`: **50 files / 761,497 bytes matched exactly**, including `main.py` and `core_grants_private.py`. Its METADATA is `serverfs-agent-bridge 0.25.0`; the source inventory aggregate SHA-256 is `3c166a0050f814f94523075e31628c1fb83de65a84b2cb67714a525e38870c3e`.

Before installation, `bridge-venv` was absent. It was created offline using cached uv-managed CPython **3.12.12** at `/Users/lx/.local/share/uv/python/cpython-3.12.12-macos-aarch64-none/bin/python3.12`; the venv's `bin/python` symlink resolves to that owned, non-writable interpreter. The exact candidate Bridge Wheel was installed offline, **40 packages** were checked by `uv pip check`, and `-I -B` reported version **0.25.0**, `sys.prefix` equal to this candidate's `bridge-venv`, and both required Bridge modules under its own `site-packages`, without `PYTHONPATH`.

The native test provisions and installs its Core runtime from the same candidate source via the offline runtime builder. Its isolated installed distribution reported `serverfs-mcp==0.25.0` and imported `serverfs_mcp.cli` plus `serverfs_mcp.darwin_shared_binding_private` from the installed runtime. The nine signed native builder inputs (build script, Info.plist, three Swift files and four LaunchAgent plists) were regular, current-user-owned files in this candidate.

## Validation and native acceptance

- Focused versioned-source and SMAppService safety tests: **46 passed**.
- Root Ruff check and format: **PASS**, 723 Python files formatted.
- Bridge Ruff check and format: **PASS**, 120 Python files formatted.
- Candidate source gate before this report: **PASS**, 211 reviewed paths, 3,253 excluded scratch paths, base HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`.

The sole native test invocation used the repository `.venv` pytest interpreter and these explicit inputs:

```text
SERVERFS_ENABLE_SHARED_SMAPP_LIVE_TEST=1
SERVERFS_V0250_NATIVE_CORE_SOURCE=/Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-candidate-7d31f8a2c04e
SERVERFS_V0250_NATIVE_BRIDGE_PYTHON=/Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-candidate-7d31f8a2c04e/bridge-venv/bin/python
/Users/lx/Projects/ServerFS_MCP/.venv/bin/python -B -m pytest -q -p no:cacheprovider --junitxml=/Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-e2e-native-20261116-u2ak3szn/native-junit.xml tests/test_darwin_v0250_shared_smappservice_live_macos.py
```

The live case registered two randomly named signed Core Login Items. It exercised authenticated local MCP, cross-Core task isolation, synthetic Bridge-owner restart, grant revocation and reauthorization, Core B native SIGKILL followed by KeepAlive recovery and authenticated readiness, and HMAC key rotation with old-key denial. JUnit, log and numeric exit file are retained in the new task-owned directory `deployment/macos/.serverfs-v0250-e2e-native-20261116-u2ak3szn`, mode **0700**; each artifact is mode **0600**.

Default full-suite regressions, each bounded at 240 seconds:

- Root using the Root `.venv`: **exit 0**, 2,951 tests, 0 failures/errors, 420 skipped, 141.319 seconds.
- Bridge using its own pre-existing `agent_bridge/.venv` from `agent_bridge/`: **exit 0**, 672 tests, 0 failures/errors, 134 skipped, 65.082 seconds.
- An earlier Bridge collection attempt used the wrong Root `.venv` and exited **2** with 46 collection errors (`acp`, `websockets`, and the Bridge source package unavailable). Its log/JUnit/exit are retained; it is an interpreter-selection error, not a Bridge product test failure. The corrected Bridge run used the documented Bridge environment and passed.

## Production postflight and remaining limits

The protected launchd labels and PIDs were unchanged before and after:

| Label | PID |
| --- | ---: |
| `com.ntlx.serverfs.tunnel` | 1779 |
| `com.ntlx.serverfs.bridge` | 1790 |
| `com.ntlx.serverfs.http-origin` | 40516 |
| `com.ntlx.serverfs.cloudflare` | 66419 |

Postflight found no `shsm-*` launchd job, `/tmp` scratch, ServerFS instance state, test Core/Bridge child, or test-owned listener. No production service, credential, Tunnel configuration, hostname or network setting was changed. No Git staging, commit, push, tag or release publication occurred.

The snapshot is **not an immutable Git SHA**; its base HEAD plus reviewed dirty-source overlay does not establish same-SHA release evidence. Windows and Linux build/runtime acceptance against one immutable v0.25.0 SHA remain **Not verified**. Metadata consistency, including the Windows Cargo lock record, does not substitute for those platform gates.
