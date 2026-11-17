# macOS v0.25.0 named Core KeepAlive recovery acceptance — 2026-11-16

## Result

**Real abrupt-exit recovery: NOT VERIFIED.** The opt-in run registered one random file-only Core and proved it started, but its first authenticated MCP roundtrip failed on a disposable fixture-content mismatch. The test therefore did **not** issue SIGKILL and did not enter the 120-second recovery gate. Per the task boundary, no second native registration was attempted after correcting the fixture.

## Native attempt and cleanup

Command executed once:

```sh
SERVERFS_ENABLE_NAMED_CORE_KEEPALIVE_LIVE=1 uv run pytest -q -s -p no:cacheprovider tests/test_darwin_v0250_named_core_keepalive_live_macos.py
```

Pytest result: **1 failed**. The disposable `hello.txt` contained `keepalive-acceptance`, while the reused `v0250_file_only_http_e2e._mcp_roundtrip()` asserts that it contains `synthetic-file-only`. The signed build, file-only staging, native registration and Core start were reached; the MCP assertion then failed. The SIGKILL statement is after the roundtrip, so **no process was killed**.

The test's `finally` path completed its exact named-service status/unregister checks, observed no named Core launchd PID, verified the file-only manifest owner/instance, removed only the two expected test log files, and unstaged the named instance. The first-run process scan used `--macos-instance`, which file-only Core argv omits; therefore that run did **not** independently verify process absence by its config path. The corrected test now checks for the unique absolute TOML path. Evidence after the run: the instance root was absent; `launchctl list` contained only the four protected ServerFS labels; the protected PIDs matched their pre-run snapshot. The test scratch directory remained because the original assertion failure prevented its success-only scratch removal. Its exact path was `/var/folders/0j/wbbc4kpd7j383n2swkl9fhp40000gn/T/serverfs-v25-kac-blxgexwr`; it was UID 501, mode 0700, contained only the test config, operator key file, workdir and signed test app, with no symlinks and all entries UID-owned. That exact directory was then removed and absence verified.

The failing run did not emit its generated test label or initial PID before the MCP assertion. Consequently those historical values cannot be reported. No matching launchd item, process, or staged instance root remains. No second service was registered, and no SIGKILL was issued.

## Corrected opt-in test

Added [test_darwin_v0250_named_core_keepalive_live_macos.py](/Users/lx/Projects/ServerFS_MCP/tests/test_darwin_v0250_named_core_keepalive_live_macos.py). It reuses the existing file-only HTTP MCP roundtrip and named launchd cleanup helpers, sets `hello.txt` to the helper's required `synthetic-file-only` content, and emits test label/PID/config/port evidence before the HTTP probe. PID ownership is checked against the exact launchd PID, current UID, manifest-pinned executable, unique absolute config path and port. It requires `KeepAlive=true`, signed bundle verification and the existing `register-preflight http-origin` contract before registration; it authorizes at most one SIGKILL and caps recovery at 120 seconds. Bridge mode is disabled; configuration and key are in a disposable temp tree, outside the exposed workdir.

The Swift usage contract confirms `register-preflight http-origin` is the existing supported invocation. The native plist template sets `KeepAlive=true`. The opt-in test will skip by default.

## Validation

- Corrected focused collection: **50 passed, 1 skipped**. The skip is the explicit live opt-in guard.
- Root full pytest: exit **0**, **2,873 tests collected**. The quiet full run did not retain an aggregate passed/skipped breakdown.
- Agent Bridge full pytest: exit **0**, **671 tests collected**.
- Root Ruff check: passed; Ruff format check: **702 files already formatted**.
- Agent Bridge Ruff check: passed; Ruff format check: **120 files already formatted**.
- `git diff --check`: passed.
- No production source, common Bridge code, production configuration, or Git publication state was changed.

## Protected production health and unrelated tool signal

Before and after the native attempt, protected PIDs were unchanged:

| Service | Launchd label | PID |
| --- | --- | ---: |
| OpenAI Tunnel | `com.ntlx.serverfs.tunnel` | 1779 |
| Agent Bridge | `com.ntlx.serverfs.bridge` | 1790 |
| Cloudflare Connector | `com.ntlx.serverfs.cloudflare` | 66419 |
| Production HTTP Core | `com.ntlx.serverfs.http-origin` | 40516 |

Read-only `launchctl print` showed the Cloudflare Connector and HTTP Core running; the Core loopback `/mcp` endpoint returned the expected anonymous **401**. The user's report that the Cloudflare MCP `list_workdirs` tool returned an internal error twice, while the preserved OpenAI Tunnel entry succeeded, is separate from this test. Its cause is **unproven**. No Cloudflare, production Core, Tunnel or Bridge restart/stop/kill was performed.

The corrected recovery acceptance remains outstanding for a later explicitly bounded run. No commit, push or tag was made.
