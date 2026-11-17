# macOS v0.25.0 signed Launcher negative preflight — 2026-11-15

## Scope and result

One opt-in real signed Launcher non-registration acceptance was run through
the existing two-Shared-Core preflight test, with tests-only changes in
`tests/test_darwin_v0250_shared_native_live_preflight_macos.py`. The test
completed with **1 passed, 0 failed, exit code 0**. It continued to prove
ordinary signed preflight and authenticated Shared Core preflight after each
temporary mutation was restored.

The negative cases independently changed only test-owned files, preserving
their original bytes and modes in `finally`:

1. The private input TOML was changed while the staged manifest retained its
   original SHA-256.
2. The private environment JSON allowed-hosts value was changed.
3. The `services.json` owner value was changed.
4. The `services.json` `http-origin` executable was changed to a safe,
   nonexistent absolute path.

For each case and each of the two signed test Cores, the test invoked exactly
the bundled Launcher with `--validate-only <random-test-instance>
http-origin` and the bundled Manager with `register-preflight http-origin`.
Both rejected each mutation; successful `ok` or `eligible` flags, private path
disclosure, new Core launchd PIDs and log-file creation were ruled out. The
original bytes and file mode were checked after restoration. No signed App
metadata or installed Python package was changed.

## Execution and protected services

The only live opt-in was
`SERVERFS_ENABLE_SHARED_SIGNED_LIVE_PREFLIGHT_TEST=1`; other
`SERVERFS_ENABLE_*` flags were unset. The exact command was:

```text
SERVERFS_ENABLE_SHARED_SIGNED_LIVE_PREFLIGHT_TEST=1 uv run pytest -q tests/test_darwin_v0250_shared_native_live_preflight_macos.py::test_two_signed_shared_cores_preflight_against_one_live_owner_without_registration
```

The focused synthetic Shared Core suites also passed: **44 passed, 2 skipped**.
Ruff check and format check passed for the modified test.

The protected launchd PID tuple was `(1779, 1790, 99279, 99271)` before and
after the live test. The PIDs and process start times matched the accepted
preflight baseline and a post-run `ps` inspection:

| Service label | PID | Process start time |
| --- | ---: | --- |
| `com.ntlx.serverfs.tunnel` | 1779 | Tue Nov 10 08:26:31 2026 |
| `com.ntlx.serverfs.bridge` | 1790 | Tue Nov 10 08:26:31 2026 |
| `com.ntlx.serverfs.cloudflare` | 99279 | Thu Nov 12 14:39:38 2026 |
| `com.ntlx.serverfs.http-origin` | 99271 | Thu Nov 12 14:39:36 2026 |

The existing conservative unstage cleanup passed. Post-run checks found no
`shpf-*` candidate directory under the ServerFS instances root or `/tmp`, and
no test Bridge owner process or scratch directory remained. No
`SMAppService` item was registered. No Manager `register`, `unregister`,
`status` mutation or `shared-start` command was called.

## Limits

This records negative preflight evidence only. It does not establish v0.25.0
release acceptance, and no v0.25 release SHA is claimed. No product source,
commit, push, tag, deployment or release publication was changed or performed.
