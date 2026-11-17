# macOS v0.25.0 signed Shared Core preflight retry after launchd rc 113 parser fix — 2026-11-15

## Result

The single requested opt-in test completed with **1 passed, 0 failed, exit code
0** in **100.70 seconds** on macOS 27.2.0, Apple arm64. The run used the
working tree's frozen environment and uv offline mode; it did not access the
network or change source. The exact command was:

```text
SERVERFS_ENABLE_SHARED_SIGNED_LIVE_PREFLIGHT_TEST=1 uv run --offline --frozen --no-sync pytest -p no:cacheprovider -o addopts='' -q tests/test_darwin_v0250_shared_native_live_preflight_macos.py::test_two_signed_shared_cores_preflight_against_one_live_owner_without_registration
```

The strict rc 113 parser accepts only the documented one-line
`Could not find service ...` stderr form or the exact two-line form beginning
`Bad request.` and followed by `Could not find service ...`; stdout must be
empty. The prior accepted same test had failed because actual launchd stderr
began with `Bad request.`. This retry passed after the parser correction. The
separately reported synthetic parser regression run had **134 passed**; it was
not rerun as part of this live retry.

The live test exercised two newly randomized signed Shared Core candidates
against one private synthetic Bridge owner, including signed Launcher
`--validate-only`, Manager `register-preflight`, product API/CLI authorization,
provenance-mode rejection and recovery, owner availability/restart, and
owner-only revocation. The test made no native registration attempt. No
`SMAppService` registration, unregistration, stop or restart was performed.

## Read-only baseline and post-run checks

Before the run, no other `SERVERFS_ENABLE_*` variable was set. The four
protected launchd labels had the recorded PIDs and process start times:

| Label | PID | Start time |
| --- | ---: | --- |
| `com.ntlx.serverfs.tunnel` | 1779 | Tue Nov 10 08:26:31 2026 |
| `com.ntlx.serverfs.bridge` | 1790 | Tue Nov 10 08:26:31 2026 |
| `com.ntlx.serverfs.cloudflare` | 99279 | Thu Nov 12 14:39:38 2026 |
| `com.ntlx.serverfs.http-origin` | 99271 | Thu Nov 12 14:39:36 2026 |

After the run, all four PIDs and start times were unchanged. No active
test-owned Core or Bridge process remained. The newly randomized test
candidate directories and scratch directory were removed by the successful
test cleanup; the only `shpf-*` instance directory and `/tmp` scratch directory
found afterward were the known pre-existing residuals:

- `~/Library/Application Support/ServerFS/instances/shpf-a-0958931b80`
- `/tmp/shpf-0958931b80-0cs3xq79`

Both old residuals remain present and unresolved. This run did not modify or
remove them, and this report does not claim that all scratch state was cleaned.

No Git operation, public ChatGPT ingress, source change, or wheel import was
performed. This evidence covers non-registering signed preflight only; it does
not establish registered Login Item acceptance or a v0.25.0 release.
