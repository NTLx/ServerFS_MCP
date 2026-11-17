# macOS v0.25.0 current dirty-wheel fresh-venv installation — 2026-11-16

## Scope and version status

This record covers only the current dirty source wheel archives and a new
Python 3.12.12 installation. It is not a v0.25.0 release claim. Root and
Bridge package metadata remain `0.24.2`; no version surfaces, lock files,
production services, Cloudflare configuration, Git state, or existing project
venvs were changed.

The source archive static gate and post-fix byte parity evidence are recorded
in [the dirty-source wheel archive acceptance](v0250-macos-dirty-source-offline-wheel-acceptance-2026-11-16.md).

## Scratch and inputs

Fresh task-owned scratch (parent mode `0700`):

`deployment/macos/.serverfs-v0250-freshvenv-63P0cK/`

It contains independent `core-venv` and `bridge-venv` environments, exported
requirements, and an empty `e2e/` directory. Both venvs were created with
`/Users/lx/.local/bin/python3.12` (Python 3.12.12). They were retained for
follow-up runtime work. No other scratch was cleaned or changed.

Input wheels and SHA-256:

- Root: `deployment/macos/.serverfs-v0250-dirty-wheel-8c3lj1q_/root/serverfs_mcp-0.24.2-py3-none-any.whl`
  — `7799c992a0022467c66a9195bbd4f5f4430aaa62a3d220f88a7817a9fcaebc63`.
- Bridge: `deployment/macos/.serverfs-v0250-dirty-wheel-8c3lj1q_/bridge/serverfs_agent_bridge-0.24.2-py3-none-any.whl`
  — `19e06bf9de5c2a298335eeb8fb1affdd1acd0319438efd9fe757e28a96ee199f`.

## Commands and results

All commands below ran from the repository root unless noted. Exit status was
0 unless stated otherwise.

1. Inspected `uv export --help` (exit 0). It supports `--frozen`, `--no-dev`,
   and requirements export with hashes. Inspected both project files and locks:
   dependency sources were the public `https://pypi.org/simple` registry and
   `https://files.pythonhosted.org` artifacts; no Git, direct URL, local, or
   private package source was found in the locked dependency set.
2. Exported the two frozen production dependency graphs without project/editable
   entries and without dev dependencies:

   ```sh
   uv export --frozen --no-dev --no-emit-project --format requirements.txt \
     -o <scratch>/root/requirements.txt
   uv export --directory "$PWD/agent_bridge" --frozen --no-dev --no-emit-project \
     --format requirements.txt -o <absolute-scratch>/bridge/requirements.txt
   ```

   Both exited 0; root export was 429 lines and Bridge export 616 lines. The
   output contains exact resolved versions and hashes. The first Bridge export
   used a relative output path, which `uv --directory` resolved under
   `agent_bridge/deployment/macos/`. That newly created task output was
   identified, confirmed to contain only the exported requirements, removed,
   and its empty task-created ancestor directories removed. The successful
   second export used an absolute output path.
3. Created the two environments:

   ```sh
   uv venv --python /Users/lx/.local/bin/python3.12 <scratch>/core-venv
   uv venv --python /Users/lx/.local/bin/python3.12 <scratch>/bridge-venv
   ```

   Both exited 0 and selected Python 3.12.12.
4. Installed requirements with hash enforcement and no dependency bypass:

   ```sh
   uv pip install --require-hashes --python <scratch>/core-venv/bin/python \
     -r <scratch>/root/requirements.txt
   uv pip install --require-hashes --python <scratch>/bridge-venv/bin/python \
     -r <scratch>/bridge/requirements.txt
   ```

   Both exited 0. Root installed 28 locked distributions; Bridge installed 39.
   Downloads used the configured public PyPI index and locked artifact URLs.
   No private URLs or credentials were probed or printed, and no proxy settings
   were changed.
5. Installed the local wheel archives into their respective venvs offline,
   allowing normal dependency resolution (no `--no-deps`):

   ```sh
   uv pip install --offline --python <scratch>/core-venv/bin/python \
     deployment/macos/.serverfs-v0250-dirty-wheel-8c3lj1q_/root/serverfs_mcp-0.24.2-py3-none-any.whl
   uv pip install --offline --python <scratch>/bridge-venv/bin/python \
     deployment/macos/.serverfs-v0250-dirty-wheel-8c3lj1q_/bridge/serverfs_agent_bridge-0.24.2-py3-none-any.whl
   ```

   Both exited 0. Each added its expected `0.24.2` distribution.
6. Isolated import provenance checks used `env -i PATH=/usr/bin:/bin`, each
   venv's Python, and `-I -B` from the task scratch. Exit 0 for both. The
   following Core modules resolved inside the new Core venv's
   `site-packages`, and metadata reported `serverfs-mcp==0.24.2`:
   `serverfs_mcp.instance_identity`,
   `serverfs_mcp.darwin_instance_shared_staging`, and
   `serverfs_mcp.core_bridge_client_v2`.
   The following Bridge modules resolved inside the new Bridge venv's
   `site-packages`, and metadata reported `serverfs-agent-bridge==0.24.2`:
   `serverfs_agent_bridge.core_grants_private`,
   `serverfs_agent_bridge.protocol`, and `serverfs_agent_bridge.main`.
   No module came from the repository or prior wheelhouse.
7. Both isolated CLI help checks exited 0 with output redirected:

   ```sh
   <scratch>/core-venv/bin/python -I -B -m serverfs_mcp.cli --help
   <scratch>/bridge-venv/bin/serverfs-agent-bridge --help
   ```

   These displayed help only and did not start a service.
8. `uv pip check --python ...` exited 0 for each venv: Core checked 29
   distributions and Bridge checked 40; both reported all installed packages
   compatible. Wheel SHA-256 values were re-read and matched the input values
   above. `git diff --exit-code -- uv.lock agent_bridge/uv.lock pyproject.toml
   agent_bridge/pyproject.toml` exited 0.
9. A single bounded attempt (`timeout 120`) was made to run
   `tests/test_darwin_v0250_installed_wheel_three_process_macos.py` against the
   new venvs, with random loopback ports and no public hold. The harness accepted
   `SERVERFS_V0250_WHEELHOUSE` and located both new interpreters, but its
   `_wheel_provenance()` check at line 79 also hardcodes the historical path
   substring `.serverfs-v0250-wheelhouse-CusylLCY`. It failed there before any
   Bridge/Core subprocess was started (`pytest` exit 1; command itself completed
   before the 120-second timeout). No test or product file was modified to
   bypass that provenance assertion, and no retry was made. The task-owned
   `e2e/` directory remains empty.

## Service and process safety

Read-only `launchctl list` snapshots after the attempt showed these same
production owners and successful last exit statuses:

| Label | PID | Last exit |
| --- | ---: | ---: |
| `com.ntlx.serverfs.tunnel` | 1779 | 0 |
| `com.ntlx.serverfs.bridge` | 1790 | 0 |
| `com.ntlx.serverfs.http-origin` | 40516 | 0 |
| `com.ntlx.serverfs.cloudflare` | 40543 | 0 |

The E2E attempt failed at preflight, before creating child processes or opening
ports. No production process/service was stopped, started, or restarted.

## Result and remaining gates

**Fresh installation and runtime provenance: PASS.** Both dirty wheels were
installed into independent fresh Python 3.12.12 venvs from frozen, hashed,
public-PyPI dependency exports. Required import origins, `0.24.2` metadata,
CLI help, and dependency consistency were verified.

**Real three-process Core + Bridge loopback E2E: NOT VERIFIED.** The existing
harness retains a historical wheelhouse path assertion even when its wheelhouse
environment variable points at the fresh venvs. It requires a narrowly scoped
harness/test adjustment or a safe task-owned compatibility layout before a
future attempt.

This work establishes no v0.25.0 release claim. The wheels are dirty-source
`0.24.2` candidates. Exact-SHA platform and release gates remain outstanding.
