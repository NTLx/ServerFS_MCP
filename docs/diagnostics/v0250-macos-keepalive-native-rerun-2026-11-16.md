# macOS v0.25.0 named file-only Core KeepAlive recovery rerun — 2026-11-16

## Result

**PASS — one real native recovery attempt completed.** On Apple Silicon macOS 27.2.0, the corrected opt-in test registered one randomly named, signed, file-only Core, verified its authenticated local MCP file operations, issued exactly one SIGKILL to that test-owned Core PID, and observed the same launchd label recover under a new PID in 4.89 seconds. The 120-second recovery bound was not approached. The earlier fixture-mismatch incident remains preserved in [the original acceptance report](v0250-macos-keepalive-native-acceptance-2026-11-16.md).

## Command and result

Exact command, executed once:

```sh
SERVERFS_ENABLE_NAMED_CORE_KEEPALIVE_LIVE=1 uv run pytest -q -s -p no:cacheprovider tests/test_darwin_v0250_named_core_keepalive_live_macos.py
```

Pytest exited **0: 1 passed**. The test emitted:

| Evidence | Value |
| --- | --- |
| Test instance | `v25-kac-eba1c4ac2d` |
| Launchd label | `com.ntlx.serverfs.v25-kac-eba1c4ac2d.core` |
| Initial test-owned Core PID, SIGKILLed once | `69477` |
| Recovered Core PID | `69524` |
| Recovery time | `4.887719874968752` seconds |
| Authenticated HTTP MCP file roundtrip | Passed |
| Recovery deadline | 120 seconds |

The test verified the initial PID against the named launchd job, current UID, manifest-pinned executable, unique config path and selected port before the single kill. It verified the recovered process and authenticated MCP response, and the named registration remained enabled during recovery.

## Safety preflight and production baseline

Before the run, read-only inventory found exactly the four protected ServerFS launchd labels and no other `com.ntlx.serverfs.*` label. `launchctl print` showed all four running. Their baseline PIDs were:

| Service | Label | Baseline PID |
| --- | --- | ---: |
| OpenAI Tunnel | `com.ntlx.serverfs.tunnel` | 1779 |
| Agent Bridge | `com.ntlx.serverfs.bridge` | 1790 |
| Cloudflare Connector | `com.ntlx.serverfs.cloudflare` | 66419 |
| Production HTTP Core | `com.ntlx.serverfs.http-origin` | 40516 |

The PID/UID/command lines matched the respective launchd jobs. No prior `v25-kac-*` launchd item, process, instance state directory or `serverfs-v25-kac-*` scratch directory was found. The existing production HTTP `/mcp` endpoint returned its expected anonymous **401**. The test independently required signed app verification, `KeepAlive=true`, owner-manifest/config/argv checks, native registration preflight, exact protected-label set and unchanged production PIDs before registration and before SIGKILL.

## Postflight and cleanup

The test reported `test_service_removed=true` and `test_scratch_removed=true`. Independent postflight confirmed:

- `launchctl list` again contained exactly the four protected labels with baseline PIDs 1779, 1790, 66419 and 40516;
- the instance state path for `v25-kac-eba1c4ac2d` and exact scratch directory `/private/var/folders/0j/wbbc4kpd7j383n2swkl9fhp40000gn/T/serverfs-v25-kac-5lzgh9mn` were absent;
- the test PIDs 69477 and 69524 were absent, and no listener was reported on test port 63533;
- production HTTP `/mcp` still returned anonymous **401**.

Cloudflare Connector and production HTTP Core were observed read-only as running. `lsof` emitted a warning that an unrelated SMB filesystem could not be inspected; it returned no listener row for the exact test port. No attempt was made to repair or mutate external Cloudflare connector state.

Ruff check and format check for the test passed (`1 file already formatted`). `git diff --check` passed. No source, test, version, lock, Bridge or service-configuration files were changed by this rerun; no Git operation was performed.

## Scope and limitations

This accepts only the isolated native macOS named **file-only Core** abrupt-exit/KeepAlive recovery path under the tested local configuration. It does not establish production service recovery, public Cloudflare ingress behavior, a provider connector repair, or other platform behavior. The separate earlier attempt and its pre-kill fixture mismatch remain unchanged in the original report.
