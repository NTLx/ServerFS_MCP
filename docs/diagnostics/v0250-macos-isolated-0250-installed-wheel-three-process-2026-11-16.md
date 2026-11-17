# macOS v0.25.0 isolated installed-wheel three-process acceptance — 2026-11-16

## Result

**NOT PASSED — stopped at offline lock resolution; no wheel build, fresh venv installation, or three-process pytest acceptance was run.** This is a bounded failure report, not release or production acceptance evidence.

The read-only candidate source gate passed with 194 reviewed paths, 55 excluded pre-existing scratch entries, and base HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`. It created the single task snapshot `deployment/macos/.serverfs-v0250-candidate-b9a796536911`. Snapshot creation passed its source-copy checks. Root, Bridge, and Plugin version metadata were changed only inside that snapshot to `0.25.0`.

## Offline lock phase

Commands were run with `UV_OFFLINE=1`, `--offline`, and `UV_CACHE_DIR` inside the task snapshot:

- Root `uv lock --offline`: failed; resolver reported `mcp==2.3.0` unavailable in cache while resolving the Python 3.14 marker split.
- Root `uv lock --check --offline`: failed with the same unavailable `mcp==2.3.0`.
- Bridge `uv lock --offline`: failed; resolver reported `agent-client-protocol==0.12.1` unavailable in cache while resolving the Python 3.14 marker split.
- Bridge `uv lock --check --offline`: failed with the same unavailable `agent-client-protocol==0.12.1`.

The shell command did not short-circuit after these failures; all four invocations ran and reported the offline cache errors. The workflow then stopped. It did not access the network, use dependency indexes, build wheels, create venvs, install packages, invoke the pytest harness, or use old wheels/source imports as substitutes.

## State and limits

The failed task snapshot was retained for diagnosis. At the recorded postflight it was owned by UID 501, mode `0700`, with zero symlinks. No test-owned child process or socket was created because pytest never started. The four protected launchd labels still reported PIDs: Tunnel `1779`, Bridge `1790`, HTTP Core `40516`, and Cloudflare `66419`.

`git diff --check` passed. Postflight porcelain counts were 11 tracked unstaged entries, 1,025 untracked entries, and zero staged/other entries; the untracked count includes this retained task snapshot and this failure report. No Git ref or index operation was performed. Adding this report means the reviewed candidate manifest will require a future explicit list update before a subsequent candidate-source check can pass; the manifest was not edited.

This run establishes neither installed-wheel provenance nor authenticated local MCP, cross-Core isolation, Bridge owner crash/restart behavior, public ingress, production lifecycle, or a clean immutable release SHA. The blocker is the unavailable offline cache set above; no attempt was made to fetch it.
