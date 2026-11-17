# v0.25.0 macOS installed-wheel three-process E2E

## Result

PASS on Apple Silicon macOS 27.2.1, 2026-11-15. The explicit acceptance wrapper launched one Bridge owner and two independent HTTP Core processes from the existing installed wheel interpreters in `deployment/macos/.serverfs-v0250-wheelhouse-CusylLCY/{bridge-venv,core-venv}`. Every child used `-I -B`, had no `PYTHONPATH`, and ran with its working directory in a unique mode-0700 scratch directory under `deployment/macos`. The wheelhouse and venvs were not modified.

The test verified installed distribution/module provenance for both children; two unauthenticated HTTP requests and two wrong-token requests returned 401; authenticated MCP `list_workdirs` returned the isolated `Workspace` on each Core; and Core B could not read a task owned by Core A. Before stopping Core A, it gracefully stopped the test-owned Bridge owner, observed its socket disappear, confirmed both HTTP Core PIDs remained alive and both continued serving `list_workdirs` while the Bridge was unavailable, and restarted the owner with the same installed Bridge interpreter, private v2 grants configuration and state directory. Core A's signed `bridge.ping` succeeded after recovery, both Cores again served `list_workdirs`, and Core B still could not read Core A's task. It then terminated Core A and confirmed the restarted owner and Core B remained alive and the Bridge still answered `bridge.ping`. Test-owned processes were terminated and the wrapper removed its scratch tree.

The test materializes each private shared manifest and binding under the test-only HOME, then validates the manifest with `resolve_shared_core_manifest` before Core startup. Signed app staging was not needed for direct process startup. The installed venv `bin/python` entries are symlinks, while the signed staging helper requires a canonical non-symlink interpreter path; using the resolver keeps startup bound to the actual wheel interpreter without changing the venv.

## Executed commands

```text
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
683 files already formatted

$ uv run python -B deployment/macos/v0250_installed_wheel_three_process_acceptance.py
.
[100%]
scratch=/Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-wheel-e2e-wz8u2g91
pytest_exit=0
exit status: 0
```

The wrapper's scratch path was checked after exit and did not exist. The independent installed-wheel test is explicitly opt-in and is not skipped on Darwin when launched through this wrapper.

## Issues found and resolved during execution

1. First run used `async def` without an installed pytest async plugin. Changed the test to a synchronous pytest entry point running `asyncio.run`, matching the existing macOS helper convention.
2. The initial harness indexed `PolicyRegistry` as a dictionary. Corrected it to the supported `.get("Workspace")` API.
3. The first actual Core process exited because direct launch did not forward its individual HTTP API key and allowed-host setting from the private manifest. Added those per-Core environment values; the next full three-process run passed.

No production source, packaging, services, Git state, cloudflared, or outside network was used by the acceptance.
