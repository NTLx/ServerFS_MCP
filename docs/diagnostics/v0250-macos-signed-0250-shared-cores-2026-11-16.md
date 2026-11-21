# macOS v0.25.0 signed two Shared Cores: isolated SMAppService acceptance — 2026-11-16

> Chronological historical record: the initial preflight stopped (NOT RUN); later separately authorized lock repair, source-backed owner, installed owner, same-source and post-guard JUnit runs have their own results below. The final durable JUnit run passed, but the intervening dot-only run remains inconclusive. The candidate is a mutable private 0.25.0 snapshot, not an immutable release SHA or current authorization. See the [integrated release checklist](../v0.25.0-release-checklist-2026-11-21.md).

## Result

**NOT RUN — preflight stopped at lock-record comparison.** No native test was invoked and no test-owned SMAppService Core was registered. Per the one-attempt instruction, no retry was made.

## Verified before stopping

- Read-only production `launchctl list | grep -F 'com.ntlx.serverfs.'` showed exactly four labels: Tunnel PID **1779**, Bridge PID **1790**, HTTP Core PID **40516**, and Cloudflare PID **66419**. No additional native ServerFS label appeared.
- `.venv/bin/python -B deployment/macos/v0250_candidate_source_gate.py` passed: **202 reviewed paths**, **1650 excluded pre-existing scratch entries**, base HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`.
- The gate created the one requested private candidate snapshot at `deployment/macos/.serverfs-v0250-candidate-9f3c2a7b6d41`. It was current-user owned, mode `0700`, canonical, contained no `.git`, nested candidate scratch, symlink, or non-regular file.
- Only snapshot `pyproject.toml`, `agent_bridge/pyproject.toml`, and `plugin.json` were changed from guarded `0.24.2` to `0.25.0`. The live checkout was not version-edited.
- `UV_CACHE_DIR` and `UV_NO_CACHE` were unset. The `uv lock --offline` and `uv lock --check --offline` commands were mistakenly executed twice from the snapshot Root directory. Root lock update/check completed and reported 40 packages, changing the local Root package entry from 0.24.2 to 0.25.0.

## Stop condition

The subsequent lock-record comparison exited **1** before comparing either project because the invoked system `python3` has no `tomllib` module (`ModuleNotFoundError`). As a result, the required **39 Root** and **49 Bridge** third-party record equivalence was not established; the Bridge lock was not separately updated or checked. This is a failed preflight. The native signed test, installed-runtime version/import proof, Core task isolation and Bridge restart/revoke/reauthorization were **not run**.

The candidate snapshot is retained because acceptance did not pass; no cleanup or broad deletion was attempted. No production launchd service was changed. No Git staging, commit, push, tag, release, dependency sync, or network operation occurred. No immutable release SHA, installed native Bridge, or public ingress acceptance is claimed.

## Separate offline lock repair

This chronological follow-up repaired only the retained isolated candidate's lock metadata. Preflight confirmed the exact snapshot path was canonical, owned by current UID 501, mode `0700`, with zero symlinks, no `.git`, and no nested candidate scratch. Root and Bridge `pyproject.toml` versions and the root `plugin.json` version were exactly `0.25.0`. Before repair, Root `uv.lock` had 40 packages and its local `serverfs-mcp` entry was already `0.25.0`; Bridge `uv.lock` had 50 packages and its local `serverfs-agent-bridge` entry was `0.24.2`.

Using the populated default uv cache, with no `UV_CACHE_DIR` or `UV_NO_CACHE` override, commands ran from each explicit candidate directory:

- Snapshot root: `uv lock --offline` and `uv lock --check --offline` both passed, resolving 40 packages.
- Snapshot `agent_bridge/`: `uv lock --offline` passed, resolving 50 packages and updating only `serverfs-agent-bridge` from `0.24.2` to `0.25.0`; `uv lock --check --offline` then passed, resolving 50 packages.

The live checkout and snapshot lockfiles were parsed with `/Users/lx/Projects/ServerFS_MCP/.venv/bin/python` (CPython 3.12.12 with `tomllib`). Root had 40 packages (39 nonlocal); Bridge had 50 packages (49 nonlocal). Every nonlocal package dictionary matched between live and candidate, including version, source, dependencies, artifacts and hashes. The only difference in each project was its local package version, `0.24.2` live to `0.25.0` candidate. No lockfile was manually edited. No network, download, install, sync, wheel build, native test, or SMAppService test was run.

Final read-only checks: the candidate source gate passed with `reviewed=203`, `excluded_scratch=2441`, base HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`; production launchd entries remained present at PIDs 66419 (`com.ntlx.serverfs.cloudflare`), 40516 (`com.ntlx.serverfs.http-origin`), 1790 (`com.ntlx.serverfs.bridge`), and 1779 (`com.ntlx.serverfs.tunnel`); `git diff --check` passed. The snapshot remains retained for the separate native-test task. This is offline lock-graph repair evidence only and does not change the earlier failed preflight record or claim native acceptance.

## Actual signed native two-Core acceptance (2026-11-16)

**Result: PASS — one test passed, pytest exit 0.** Ran the single requested opt-in test using the root `.venv` and the retained versioned candidate source; no retry was made:

```sh
SERVERFS_ENABLE_SHARED_SMAPP_LIVE_TEST=1 SERVERFS_V0250_NATIVE_CORE_SOURCE=/Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-candidate-9f3c2a7b6d41 .venv/bin/python -B -m pytest -q -s -p no:cacheprovider tests/test_darwin_v0250_shared_smappservice_live_macos.py
```

The command completed in approximately 60 seconds with exit code **0** and one pytest pass marker (`.`). The passing fixture built its temporary signed Core runtime from the candidate and its pinned physical Python verified distribution metadata `serverfs-mcp==0.25.0` plus imports of `serverfs_mcp.cli` and `serverfs_mcp.darwin_shared_binding_private` resolving inside that runtime's isolated installed site-packages. The test covered two random signed SMAppService Core Login Items, authenticated local MCP and task isolation, synthetic Bridge-owner stop/restart, grant revocation and reauthorization, and test-owned teardown.

Pre-test production launchd baseline was exactly:

- `com.ntlx.serverfs.tunnel` PID **1779**
- `com.ntlx.serverfs.bridge` PID **1790**
- `com.ntlx.serverfs.http-origin` PID **40516**
- `com.ntlx.serverfs.cloudflare` PID **66419**

Post-test launchd status contained exactly those same four labels and PIDs. No `shsm-a-*` or `shsm-b-*` launchd label remained. No test Core or synthetic Bridge-owner process, `/tmp/shsm-*` scratch directory, or test-owned `ServerFS/instances/shsm-a-*` / `shsm-b-*` state remained. The production Bridge process remained at its baseline PID **1790**. `git diff --check` passed.

This is actual local signed SMAppService acceptance against the retained dirty-source candidate only. The synthetic Bridge owner runs current checkout source, not an independently installed 0.25.0 Bridge. There is no clean immutable release SHA evidence, no production service/config/credential/Tunnel/domain change, and no public ChatGPT or public ingress acceptance. This is not a release claim.

## Final independent installed Bridge follow-up (2026-11-16)

**Result: PASS — one native test passed, actual pytest exit code 0.** The single bounded run started at **16:16:42 CST**, ended at **16:17:52 CST**, and completed in **70 seconds** (300-second hard limit). No retry was made.

The two explicit preexisting test inputs were verified read-only before startup:

- Signed Core source: `deployment/macos/.serverfs-v0250-candidate-9f3c2a7b6d41`, canonical, current-user owned, mode `0700`, no `.git`; Root and Bridge project metadata and local lock records both declared `0.25.0` (40 and 50 locked packages). The candidate source gate passed with **203 reviewed paths**, **2441 excluded scratch entries**, and base HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`.
- Independent Bridge interpreter: `deployment/macos/.serverfs-v0250-candidate-4aa59c2ac29a/bridge-venv/bin/python`. `importlib.metadata` reported `serverfs-agent-bridge==0.25.0`; `serverfs_agent_bridge.main` and `serverfs_agent_bridge.core_grants_private` both resolved under that venv's `site-packages`. `uv pip check` passed for all **40 packages**.

The exact test invocation used both opt-ins and the repository `.venv` pytest interpreter:

```sh
SERVERFS_ENABLE_SHARED_SMAPP_LIVE_TEST=1 \
SERVERFS_V0250_NATIVE_CORE_SOURCE=/Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-candidate-9f3c2a7b6d41 \
SERVERFS_V0250_NATIVE_BRIDGE_PYTHON=/Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-candidate-4aa59c2ac29a/bridge-venv/bin/python \
.venv/bin/python -B -m pytest -q -s -p no:cacheprovider \
tests/test_darwin_v0250_shared_smappservice_live_macos.py
```

The single pytest case passed. It registered two randomly named signed native 0.25.0 Shared Cores, used the independently installed 0.25.0 Bridge distribution in all four synthetic Bridge-owner subprocess phases (`-I -B`, without `PYTHONPATH`), and exercised authenticated loopback MCP, cross-Core task isolation, owner restart, grant revocation and reauthorization, and Core teardown. No public ingress or production service signal, token, credential, or configuration was used.

Protected launchd labels and PIDs were unchanged before and after:

| Label | Before PID | After PID |
| --- | ---: | ---: |
| `com.ntlx.serverfs.tunnel` | 1779 | 1779 |
| `com.ntlx.serverfs.bridge` | 1790 | 1790 |
| `com.ntlx.serverfs.http-origin` | 40516 | 40516 |
| `com.ntlx.serverfs.cloudflare` | 66419 | 66419 |

Postflight found no `shsm-*` launchd label, test Core or Bridge process, matching TCP listener, instance state, or `/tmp/shsm-*` scratch. The test's own teardown removed its isolated state. The selected Core source snapshot and installed Bridge wheel/venv came from **different dirty development snapshots**, not one immutable shared SHA. This does not establish production Cloudflare behavior or a release; no release claim is made.

## Bridge product bytes across two dirty candidate snapshots (2026-11-16)

**Result: PASS — all requested read-only parity checks completed.** Compared only candidate A `deployment/macos/.serverfs-v0250-candidate-9f3c2a7b6d41` (signed Core source) and candidate B `deployment/macos/.serverfs-v0250-candidate-4aa59c2ac29a` (independent installed Bridge), without changing either snapshot or installed environment. Both candidate roots were canonical, owned by current UID **501**, and mode **0700**. No symlink appeared in either Bridge product-source path; neither source tree contained compiled/pycache files.

The sorted regular-file inventory under each `agent_bridge/src/serverfs_agent_bridge` contained **50 files / 761,497 bytes**. File sets and per-file SHA-256 values were identical, with no mismatching filenames. The aggregate SHA-256, computed in sorted relative-path order over each relative path, NUL separator, file bytes and newline, was **`918373b78186fe32f8ed93a7a28af9518555de31f6f41c8454f84485960ab815`** for both trees. Both `agent_bridge/pyproject.toml` files and their local `serverfs-agent-bridge` lock records declared **0.25.0**. Both `agent_bridge/uv.lock` files had identical SHA-256 **`13f95299d6f6a86dcea6c01a319387ffc0f31819bd3e07b4ce249709d461435e`**; parsing with repository `.venv/bin/python` 3.12 found **49** nonlocal package records in each, and their complete package dictionaries were identical.

Installed B `bridge-venv/lib/python3.12/site-packages/serverfs_agent_bridge` matched B source for all **50** regular Python source/package-resource files: no missing, extra, or differing files. Its isolated `bridge-venv/bin/python -I -B` probe exited **0**, reported `serverfs-agent-bridge==0.25.0`, and resolved both `serverfs_agent_bridge.main` and `serverfs_agent_bridge.core_grants_private` inside B's own `bridge-venv/lib/python3.12/site-packages`.

The requested candidate source gate passed (**203 reviewed**, **2441 excluded scratch**, base HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`); `git diff --check` passed. Read-only protected launchd status still showed exactly the existing four labels/PIDs: Tunnel **1779**, Bridge **1790**, HTTP origin **40516**, and Cloudflare **66419**. This byte equivalence does **not** produce the same Git SHA and does **not** prove an exact clean release; no release claim is made.

## Single source-candidate A Core + Bridge signed joint acceptance (2026-11-16)

**Result: PASS — the one requested native test invocation exited 0 with 1 passed in approximately 62.5 seconds.** No retry was made; the 300-second bound was not approached. Both signed Shared Core runtimes and every synthetic Bridge-owner process used the same candidate A snapshot and its installed Bridge venv.

Read-only preflight confirmed candidate A `deployment/macos/.serverfs-v0250-candidate-9f3c2a7b6d41` was canonical, owned by the current UID, and mode `0700`. Root and `agent_bridge/` project metadata declared `0.25.0`; `uv lock --check --offline` passed from both directories (40 and 50 locked packages). The exact A interpreter `bridge-venv/bin/python -I -B` reported `serverfs-agent-bridge==0.25.0`, and both `serverfs_agent_bridge.main` and `serverfs_agent_bridge.core_grants_private` imported from that venv's site-packages. The sole A wheel SHA-256 was **`80721da07b18adedff5bf0244f5d17599aff5389555ded1acb48c74ea2b4ab79`**. Candidate source gate passed before the test with **204 reviewed paths**, **2441 excluded scratch entries**, base HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`. No candidate source file was edited.

The exact single invocation was:

```sh
SERVERFS_ENABLE_SHARED_SMAPP_LIVE_TEST=1 \
SERVERFS_V0250_NATIVE_CORE_SOURCE=/Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-candidate-9f3c2a7b6d41 \
SERVERFS_V0250_NATIVE_BRIDGE_PYTHON=/Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-candidate-9f3c2a7b6d41/bridge-venv/bin/python \
.venv/bin/python -B -m pytest -q -s -p no:cacheprovider \
tests/test_darwin_v0250_shared_smappservice_live_macos.py
```

The passing test built two isolated signed native Core apps from A, verified their installed `serverfs-mcp==0.25.0` runtime imports, and registered two randomized `shsm-a-*` / `shsm-b-*` SMAppService Core jobs. It exercised authenticated local HTTP/MCP, cross-Core task isolation, the installed A Bridge owner's startup and restart, grant revocation and reauthorization, and native Core teardown. The test emitted no transient Core PID values; it did prove both jobs registered and remained stable during the lifecycle assertions before unregistering them.

Protected production launchd PIDs were unchanged:

| Label | Before PID | After PID |
| --- | ---: | ---: |
| `com.ntlx.serverfs.tunnel` | 1779 | 1779 |
| `com.ntlx.serverfs.bridge` | 1790 | 1790 |
| `com.ntlx.serverfs.http-origin` | 40516 | 40516 |
| `com.ntlx.serverfs.cloudflare` | 66419 | 66419 |

Postflight found exactly these four production labels and no `shsm-*` launchd labels. There were no test Core/Bridge child processes, `/tmp/shsm-*` scratch directories, test instance state, test listeners, or test Unix sockets. The test's own teardown removed its randomized native registrations and scratch. No Git staging or other repository change was made for this acceptance.

Candidate A is a **mutable source snapshot**, not an immutable shared Git SHA; same-source inputs do not establish same-immutable-Git-SHA provenance. This local acceptance does not establish production service behavior, public Cloudflare ingress, or a release.

## Post single-source fail-closed native re-acceptance (2026-11-16)

**Result: test invocation completed with one pass marker (`.`) in approximately 64 seconds; the exact pytest exit code and summary were not retained by the command-session output, so the required `exit 0 / 1 passed` result is not independently verified here.** The native test was invoked exactly once and was not retried. No preflight control was bypassed.

The strict preflight passed for candidate A at `deployment/macos/.serverfs-v0250-candidate-9f3c2a7b6d41`: canonical current-user-owned directory, mode `0700`, no `.git`; root and Bridge project metadata and both local lock entries were `0.25.0`. The candidate source gate passed with **204 reviewed paths**, **2441 excluded scratch entries**, base HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`. Its `bridge-venv/bin/python -I -B` reported `serverfs-agent-bridge==0.25.0`; `main` and `core_grants_private` resolved under that venv's site-packages, and `uv pip check` passed for all **40 packages**. The signed build script and all nine required native inputs were regular files: `build.sh`, `Info.plist`, three Swift files, and four LaunchAgent plists.

The protected launchd preflight found exactly the four production labels and PIDs: Tunnel **1779**, Bridge **1790**, HTTP Core **40516**, and Cloudflare **66419**. No preexisting test-labelled process, `/tmp/shsm-*` scratch, or `instances/shsm-*` state was found. The exact one-shot command was:

```sh
SERVERFS_ENABLE_SHARED_SMAPP_LIVE_TEST=1 \
SERVERFS_V0250_NATIVE_CORE_SOURCE=/Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-candidate-9f3c2a7b6d41 \
SERVERFS_V0250_NATIVE_BRIDGE_PYTHON=/Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-candidate-9f3c2a7b6d41/bridge-venv/bin/python \
.venv/bin/python -B -m pytest -q -s -p no:cacheprovider \
tests/test_darwin_v0250_shared_smappservice_live_macos.py
```

The re-acceptance exercised the updated fail-closed same-source guard with Core source A and Bridge Python from A. The test invokes `bash <candidate-A>/native/macos/service_manager/build.sh` for the signed native app build, provisions the Core runtime from A, and starts each synthetic Bridge owner with A's installed Bridge interpreter. The expected scope includes authenticated local MCP/file operations, two signed Core jobs, cross-Core task isolation, and Bridge restart, grant revocation, and re-authorization. One pytest pass marker was observed; because the returned session output did not expose the final numeric status or pytest summary, the total assertion count and exit status remain **Not verified**.

Postflight independently passed the candidate gate again (**204 reviewed**, **2441 excluded**), `git diff --check`, and the zero-staged check. Launchd still contained exactly the four protected labels at their original PIDs; no test-labelled process, test scratch, or test instance state remained. No candidate snapshot or venv was changed by this acceptance. This evidence is from a mutable private source snapshot, **not the same immutable Git SHA**, and does **not** establish production public ingress or release acceptance.

## Durable JUnit post-guard native acceptance (2026-11-16)

**Result: PASS.** This was the single authorized new native pytest invocation. Its wrapper subprocess returned numeric exit code **0** (`NATIVE_PYTEST_EXIT=0`). Independently parsing the retained, complete JUnit XML found **tests=1, failures=0, errors=0, skipped=0**, with the single named case `test_two_real_smappservice_shared_cores_keep_owner_alive` and no failure, error, or skipped child element. The preceding attempt that emitted only `.` without retaining its numeric exit or pytest summary remains inconclusive and is not counted as acceptance.

The exact JUnit artifact is retained in the current-user-owned mode-`0700` task directory at `deployment/macos/.serverfs-v0250-e2e-c270726f/native-same-source-junit.xml`. The test used candidate A's signed native `build.sh` and A's installed Bridge interpreter at `deployment/macos/.serverfs-v0250-candidate-9f3c2a7b6d41/bridge-venv/bin/python`. Preflight passed the candidate source gate (**204 reviewed paths**), both candidate lock checks, and offline `uv pip check` (**40 Bridge packages**); the signed build script, three Swift sources, four LaunchAgent plists, and Info.plist were regular files.

Postflight retained exactly the protected launchd labels/PIDs: Tunnel **1779**, Bridge **1790**, HTTP origin **40516**, and Cloudflare **66419**. No `shsm-*` launchd job, test child process, test listener or socket, `/tmp/shsm-*` scratch, or test instance state remained. `git diff --check` passed and the index remained unstaged. No candidate snapshot or venv was changed by this acceptance.

Candidate A is a mutable private source snapshot, not an immutable release SHA. This local native acceptance does not establish production public ingress or release acceptance; no release claim is made.
