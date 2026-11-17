# macOS v0.25.0 dirty-source offline wheel acceptance — 2026-11-16

## Scope and result

This is packaging evidence for the current dirty worktree only. HEAD was
`da9c12e600d3b336f1fd7a3d5232702e9abbc19f`; both root and `agent_bridge/`
project metadata still declare `0.24.2`. These wheels are **dirty-source
0.24.2 candidates**, not v0.25.0 release artifacts. No version, source, or
release metadata was changed for this report.

Both wheels built with `uv 0.9.7` and `uv build --offline --wheel` using the
PEP 517 build environment. The root wheel was built into the task-owned private
scratch below. The Bridge build command exited successfully from
`agent_bridge/`, but its relative output path initially resolved under
`agent_bridge/deployment/macos/`; consequently the first static-gate invocation
reported `FileNotFoundError` for the expected Bridge path. After confirming the
actual command cwd and output, the wheel was copied into the authorized scratch
and the unique misplaced task scratch was removed. Both target files were
explicitly confirmed present before the successful archive gate. This was a
path error, not a malformed archive.

Retained inspectable outputs (parent mode `0700`):

- `deployment/macos/.serverfs-v0250-dirty-wheel-8c3lj1q_/root/serverfs_mcp-0.24.2-py3-none-any.whl`
- `deployment/macos/.serverfs-v0250-dirty-wheel-8c3lj1q_/bridge/serverfs_agent_bridge-0.24.2-py3-none-any.whl`

## Archive and source checks

The static archive gate passed for both wheels at expected metadata version
`0.24.2`. It verified member safety and complete SHA-256/size `RECORD` entries.

- Root: 113 members, 1,445,126 uncompressed bytes, SHA-256
  `7799c992a0022467c66a9195bbd4f5f4430aaa62a3d220f88a7817a9fcaebc63`.
- Bridge: 54 members, 767,102 uncompressed bytes, SHA-256
  `19e06bf9de5c2a298335eeb8fb1affdd1acd0319438efd9fe757e28a96ee199f`.

The following wheel payloads were byte-for-byte equal to the current source:

- `serverfs_mcp/darwin_instance_shared_staging.py` — SHA-256
  `5c6ebab3bd5fd745c0b8fb0cac64bbde44fb1588c239da4c18d80ae2c9363983`.
  The post-fix isolated `-I -B` runtime probe and staged `-B -m
  serverfs_mcp.cli` arguments are present.
- `serverfs_mcp/core_bridge_client_v2.py` — SHA-256
  `2fc43bae75264ea8643eff4b07fcb1d1486af20fcee679d57779aab7451c376f`.
- Bridge v2 modules `config.py`, `main.py`, `protocol.py`, `service.py`,
  `store.py`, `core_grants.py`, `core_grants_private.py`, and
  `macos_owned_config.py` were each byte-for-byte compared with
  `agent_bridge/src/serverfs_agent_bridge/` and matched.

## Validation

- Focused tests: **53 passed** across
  `test_darwin_v0250_wheel_archive_gate.py`,
  `test_darwin_v0250_shared_staging.py`, and
  `test_darwin_v0250_shared_swift_gate.py`.
- Ruff check and format check: **PASS** on only the currently changed Python
  files (103 root files and 15 Bridge files); both environments were invoked
  with `uv run --offline --no-sync`, so no project environment sync occurred.
- `git diff --check`: **PASS** for tracked changes.
- Root and Bridge isolated installation/import and real Core+Bridge HTTP/IPC:
  **NOT VERIFIED**. The active project environments use Python 3.12.12; the
  old retained wheelhouse uses Python 3.13 and was not used. The corrected
  offline preflight against the Python 3.12 site-packages reports every direct
  wheel requirement as installed-only and absent from `/Users/lx/.cache/uv`:
  root `mcp==2.3.0`, `pydantic>=2.7`, `starlette>=0.48`, `uvicorn>=0.30`; Bridge
  `agent-client-protocol==0.12.1`, `claude-agent-sdk==0.2.156`,
  `httpx2==2.13.0`, `qoder-agent-sdk==1.0.15`, `typesafe-sdk==0.7.1`, and
  `websockets==17.1`. No fresh venv was populated, and no `--no-deps` or
  existing-wheelhouse fallback was used. Therefore installed `-I -B` module
  provenance and runtime interaction are not claimed.

## Production invariants and scratch ownership

Read-only `launchctl list` snapshots before and after this work showed the same
four service PIDs and successful last exit statuses:

| Label | PID | Last exit |
| --- | ---: | ---: |
| `com.ntlx.serverfs.tunnel` | 1779 | 0 |
| `com.ntlx.serverfs.bridge` | 1790 | 0 |
| `com.ntlx.serverfs.http-origin` | 40516 | 0 |
| `com.ntlx.serverfs.cloudflare` | 40543 | 0 |

No production service, Tunnel, Cloudflare/API/DNS configuration, or network
state was changed. The maintainer separately reported successful connected
`list_workdirs` calls through both ChatGPT MCP connections. No listener-port
snapshot is used as proof of these launchd owners.

The authorized wheel scratch is retained for inspection. The unique misplaced
Bridge output directory was removed after verifying it contained only this
task's wheel and generated `.gitignore`; the empty ancestor directories under
`agent_bridge/deployment/` were left untouched because their pre-task ownership
was not established. Existing wheelhouse/import scratch and all other unknown
scratch were preserved. No acceptance child processes were started.

## Remaining release gate

No v0.25.0 release claim follows from this work. Release packaging still needs
the intended clean/exact release SHA with reconciled `0.25.0` version metadata,
then the required exact-SHA Linux/Container, Windows Native, Windows Agent,
macOS Native, and Pages gates and their applicable acceptance evidence. This
report establishes static dirty-source wheel integrity and source parity only.
