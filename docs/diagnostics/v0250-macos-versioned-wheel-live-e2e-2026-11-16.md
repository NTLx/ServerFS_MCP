# macOS v0.25.0 versioned installed-wheel three-process E2E — 2026-11-16

## Result

**OVERALL FAIL — versioned wheels built and installed offline, but the one opt-in three-process E2E failed before Bridge readiness.** This is development evidence from a dirty candidate snapshot. It is not a release, immutable-SHA, or production acceptance result.

The candidate source gate passed with **195 reviewed paths**, **842 excluded existing scratch entries**, zero staged changes admitted, and base HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`. A new private snapshot was created at `deployment/macos/.serverfs-v0250-candidate-4aa59c2ac29a`. Its root had no `.git` directory and contained no copied `.serverfs-v0250-candidate-*` tree. The earlier failed scratch was not accessed. Only the snapshot's Root and Bridge `pyproject.toml` files and Plugin `plugin.json` were changed from `0.24.2` to `0.25.0`.

## Offline locks and wheels

`UV_CACHE_DIR` and `UV_NO_CACHE` were unset, and uv used its existing default cache with offline mode:

- Root `uv lock --offline` and `uv lock --check --offline`: **PASS**, 40 packages. All **39 nonlocal package records**, including versions, sources, and dependencies, were unchanged.
- Bridge `uv lock --offline` and `uv lock --check --offline`: **PASS**, 50 packages. All **49 nonlocal package records**, including versions, sources, and dependencies, were unchanged.
- Offline wheel builds for both snapshot projects: **PASS**.
- Current `v0250_wheel_archive_gate.py --expected-version 0.25.0`: **PASS**.

Wheel archive evidence:

- `serverfs_mcp-0.25.0-py3-none-any.whl`: 113 members, SHA-256 `8ff1df10b157dca0fc6ce612a6af6aca648e01f68d2581b8f121e729f44f7083`.
- `serverfs_agent_bridge-0.25.0-py3-none-any.whl`: 54 members, SHA-256 `80721da07b18adedff5bf0244f5d17599aff5389555ded1acb48c74ea2b4ab79`.

## Installed-wheel preflight

The existing local CPython **3.12.12** interpreter at `/Users/lx/.local/share/uv/python/cpython-3.12.12-macos-aarch64-none/bin/python3.12` created two independent fresh venvs under the snapshot. Each wheel and all dependencies installed using `uv pip install --offline`; no download or network access was used. `uv pip check` passed in both environments (29 Core packages and 40 Bridge packages).

Independent `-I -B -c` checks confirmed actual distribution version **0.25.0** and site-packages-only imports:

- Core `serverfs_mcp.cli` and `serverfs_mcp.darwin_shared_binding_private` came from `core-venv/lib/python3.12/site-packages`.
- Bridge `serverfs_agent_bridge.main` and `serverfs_agent_bridge.core_grants_private` came from `bridge-venv/lib/python3.12/site-packages`.

No source-tree or editable fallback was used.

## One installed-wheel E2E attempt

The single requested test was run with the repository pytest interpreter, `SERVERFS_ENABLE_INSTALLED_WHEEL_E2E=1`, the snapshot wheelhouse, a mode-`0700` `e2e` scratch directory, no public hold, and a 180-second timeout:

```text
.venv/bin/python -B -m pytest -q tests/test_darwin_v0250_installed_wheel_three_process_macos.py
```

**FAIL, exit 1.** The Bridge owner exited with code 1 during startup. The precise failure was:

```text
serverfs_agent_bridge.errors.BridgeError: socket path exceeds the Darwin sun_path limit
```

The randomized absolute snapshot path made the test's Unix socket path too long. The test stopped while waiting for Bridge readiness, before starting either Core process. Consequently loopback MCP authentication, cross-Core isolation, Bridge owner stop/restart, and the two installed Core processes were **not reached**. No retry or source/test modification was made.

## Postflight and limits

- The failed Bridge owner exited; no test pytest, `core-venv`, or `bridge-venv` process remained. The test owner socket did not exist, and TCP port `127.0.0.1:18765` had no listener. The test failed before Core HTTP listeners started.
- Protected launchd PIDs `1779`, `1790`, `40516`, and `66419` remained present with their original commands. No launchd/service or production configuration was changed.
- A separate existing process pair was observed after the run: supervisor PID `2719` and Core PID `2731`, both started **2026-11-10**, with the live `serverfs.toml` and no task-scratch path in their commands. They were not modified.
- The new candidate snapshot and E2E scratch were retained after failure. No cleanup was attempted.
- Exact process-path check found no pytest, `core-venv`, or `bridge-venv` process; port `127.0.0.1:18765` had no listener.
- `git diff --check`: **PASS** after writing this report.

This run proves offline versioned wheel construction, installation, distribution metadata, and installed module provenance. It does **not** prove the three-process runtime acceptance, a clean immutable release SHA, public production behavior, SMAppService acceptance, or Cloudflare acceptance.

## Short-socket installed-wheel E2E follow-up — 2026-11-16

The two explicit test-only preflight defects were corrected before this follow-up: the focused socket-budget test now imports `serverfs_agent_bridge.local_ipc` through the existing three-process E2E fixture bootstrap, and the test uses a short, private runtime path so its test-owned socket fits Darwin `sun_path`. No source, test, Git, installed live environment, or preserved wheelhouse was modified in this run.

### Preflight

All required gates passed before any test process was started:

- `uv run --offline --no-sync pytest -q -p no:cacheprovider tests/test_darwin_v0250_installed_wheel_socket_budget.py`: **PASS, exit 0, 3 passed**.
- Targeted Ruff check: **PASS, exit 0**.
- Targeted Ruff format check: **PASS, exit 0, 2 files already formatted**.
- `.venv/bin/python -B deployment/macos/v0250_candidate_source_gate.py`: **PASS, exit 0, reviewed=197, excluded_scratch=1650**, base HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`.
- `git diff --check`: **PASS, exit 0**.

The preserved candidate wheelhouse `deployment/macos/.serverfs-v0250-candidate-4aa59c2ac29a` was owned by the current UID (501), mode `0700`. Independent `-I -B` checks imported Core `serverfs_mcp.cli` and Bridge `serverfs_agent_bridge.main` from their respective venv site-packages and reported distribution version **0.25.0** for both. `uv pip check --python` passed for both environments (29 Core packages and 40 Bridge packages). No editable/source-tree fallback, package installation, rebuild, or network access was used.

Before E2E startup, protected launchd PIDs were confirmed as Tunnel **1779**, Bridge **1790**, Core HTTP origin **40516**, and Cloudflare **66419**, with their expected commands. The test-owned runtime directory was created directly under `deployment/macos` as mode `0700`:

```text
deployment/macos/.serverfs-v0250-e2e-d48e53e4
```

The exact future `/case/owner.sock` absolute path measured **93 UTF-8 bytes**, within the Darwin `sun_path` limit of **103 bytes**.

### Single E2E attempt

Exactly one opt-in `tests/test_darwin_v0250_installed_wheel_three_process_macos.py` run used the root `.venv/bin/python -B -m pytest -q -p no:cacheprovider` command with a 180-second alarm limit, the preserved candidate wheelhouse, this new scratch path, no public hold, and no network use. **PASS, exit 0, completed in about 12 seconds.** The test exercised one independently installed Bridge and two independently installed Core processes on local loopback, HTTP/MCP authentication, cross-Core task isolation, Bridge owner stop and restart, and continued Core availability. No retry was made.

### Postflight and limits

- No E2E child process matched the unique scratch or the candidate venv executables. No Unix socket remained under the E2E scratch. Test-selected loopback ports **56709** and **56710** had no TCP listeners.
- Protected launchd PIDs **1779**, **1790**, **40516**, and **66419** remained unchanged with their expected commands.
- The task-owned scratch was verified to be the exact newly created direct child, current-UID owned, mode `0700`, and free of symlinks, then removed. It no longer exists. The preserved candidate wheelhouse and older scratch were not touched.
- Final `git diff --check`: **PASS, exit 0**.

This follow-up resolves the prior short-socket startup failure for this one installed-wheel three-process run. It is development evidence only: it does not establish a clean immutable release SHA, production behavior, native SMAppService registration/recovery, public ingress or Cloudflare acceptance, or a release claim.
