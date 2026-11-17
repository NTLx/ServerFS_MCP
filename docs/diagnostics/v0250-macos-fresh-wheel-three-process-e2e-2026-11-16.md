# v0.25.0 macOS fresh-venv installed-wheel three-process E2E

## Result

PASS on Apple Silicon macOS, 2026-11-16. The existing opt-in test ran with the
fresh isolated Python 3.12 environments in
`deployment/macos/.serverfs-v0250-freshvenv-63P0cK/`; no old wheelhouse was
used. The test reported `1 passed` and the bounded runner returned exit code
`0`.

Before the E2E run, Ruff check passed and Ruff format check reported the test
already formatted. The first test attempt reached shared Core manifest
validation and failed because the test-built launch arguments omitted `-B`,
which the production manifest contract requires. The sole test-only correction
was to include `-B` in that manifest argument list. Ruff check and format check
both passed again after the correction; the rerun passed.

The first attempt's initial supervisor printed `124` after the pytest process
had already exited because of an error in that supervisor's status reporting.
That value is not treated as the test result; the corrected bounded supervisor
captured the passing rerun's actual pytest exit code `0`.

The E2E exercised the installed Core and Bridge distributions in their
respective `core-venv` and `bridge-venv`, including HTTP authentication,
isolated workdir listing, task isolation, Bridge owner stop/restart, Core
continued service during the owner interruption, and Bridge recovery. No
production service or production configuration was modified.

## Execution

```text
SERVERFS_ENABLE_INSTALLED_WHEEL_E2E=1
SERVERFS_V0250_WHEELHOUSE=/Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-freshvenv-63P0cK
SERVERFS_V0250_WHEEL_SCRATCH=/Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-freshvenv-63P0cK/e2e
.venv/bin/python -B -m pytest -p no:cacheprovider -q tests/test_darwin_v0250_installed_wheel_three_process_macos.py
```

The pytest process was supervised with a 180-second wall-clock bound and its
own process group. Final output was `. [100%]`, `1 passed`, and
`bounded_exit_code=0`.

## Cleanup and production state

After the passing run, no test-owned Bridge/Core process remained and the
test-owned `owner.sock` was absent. The private `e2e/case` tree was removed;
the designated `e2e` scratch directory is empty. The original failed attempt's
private case tree was also removed before retrying.

Production PIDs observed before and after the test remained present: 1779,
1790, 2692, 2719, 2731, 2765, 40516 and 40543. They are the existing local
Tunnel, Agent Bridge, Core/supervisor, file ingress and Cloudflare processes;
the test did not signal or alter them.

No package installation, external network access, privileged action, or Git
operation was performed. The test's required HTTP traffic was loopback-only.
