# macOS independent installed wheel runtime verification — 2026-11-15

## Result and scope

The requested isolated installed-wheel runtime checks **PASS** for the current
dirty-tree archives. These are `0.24.2` metadata wheels built from the current
worktree; this report does not claim a v0.25.0 release build or release
acceptance.

Core and Agent Bridge were installed into two independent venvs, each with its
own exported, frozen lock requirements. The initial combined-resolver probe
was invalid for the product architecture and is not used as a failure result.
No repository lock, dependency declaration, existing venv, global cache
configuration, or production service was changed.

## Artifacts and wheelhouse

- Host: macOS 27.2.1 arm64, CPython 3.13.9; uv 0.9.7.
- Scratch and wheelhouse: `deployment/macos/.serverfs-v0250-wheelhouse-CusylLCY/`
  and its `wheelhouse/`, both mode `0700`.
- Wheelhouse contains 45 wheel archives. The root and Bridge exports were
  generated separately from their existing `uv.lock` files using
  `uv --no-config export --frozen --format requirements-txt --no-dev
  --no-emit-project`. The exported requirements include hashes.
- Applicable lock artifacts were downloaded from the explicit official
  `https://pypi.org/simple` index using `pip download --require-hashes
  --no-deps`. No alternate index, credential, TLS bypass or package
  configuration was used. Separate exports preserve lock-specific versions
  such as root `idna==3.19` / `mcp==2.3.0` and Bridge `idna==3.20` /
  `mcp==1.30.0`.
- Root application wheel SHA-256:
  `de6ea0f188a608d3bc0e02b89a9e8716ecf5d37f490220c61536cedba9a06d58`
- Bridge application wheel SHA-256:
  `19e06bf9de5c2a298335eeb8fb1affdd1acd0319438efd9fe757e28a96ee199f`
- Root exported requirements SHA-256:
  `3d7e4c897426493e576bf2b9be4026103337ae685417269a600ab4424506bbb4`
- Bridge exported requirements SHA-256:
  `53113622e7c07bcfb9fc9496b9938fa4a29671ab5ab773d6b8b53f71a3d17f8a`

## Isolated installs and dependency integrity

Created fresh environments at:

- `deployment/macos/.serverfs-v0250-wheelhouse-CusylLCY/core-venv`
- `deployment/macos/.serverfs-v0250-wheelhouse-CusylLCY/bridge-venv`

For each venv, `uv pip sync --offline --no-index --find-links <wheelhouse>
--require-hashes --python <venv>/bin/python <its exported requirements>`
installed the exact locked dependency set; the corresponding application wheel
was then installed from the same wheelhouse with `uv pip install --offline
--no-index --find-links <wheelhouse> --python <venv>/bin/python <wheel>`.

- Core: 28 locked dependencies plus `serverfs-mcp==0.24.2`; `uv pip check`
  checked 29 installed distributions and reported all compatible.
- Bridge: 39 locked dependencies plus `serverfs-agent-bridge==0.24.2`;
  `uv pip check` checked 40 installed distributions and reported all
  compatible.
- No missing module or metadata dependency was reported.

## Isolated import and provenance checks

Both checks used their own interpreter with `-I -B`.

- Core imported `serverfs_mcp.cli`, `serverfs_mcp.darwin_shared_runtime`, and
  `serverfs_mcp.core_bridge_client_v2`. All three `__file__` paths were under
  `core-venv/lib/python3.13/site-packages/serverfs_mcp/`.
  Distribution metadata was `serverfs-mcp 0.24.2` at
  `core-venv/lib/python3.13/site-packages/serverfs_mcp-0.24.2.dist-info/`;
  installed distribution file listing contained 119 entries.
- Bridge imported `serverfs_agent_bridge.main`,
  `serverfs_agent_bridge.protocol`, and `serverfs_agent_bridge.service`. All
  three `__file__` paths were under
  `bridge-venv/lib/python3.13/site-packages/serverfs_agent_bridge/`.
  Distribution metadata was `serverfs-agent-bridge 0.24.2` at
  `bridge-venv/lib/python3.13/site-packages/serverfs_agent_bridge-0.24.2.dist-info/`;
  installed distribution file listing contained 59 entries.
- These imports resolved from their respective installed wheels, not the
  checkout source tree.

## Core-to-Bridge v2 process boundary

A short isolated AF_UNIX check started a `v2_only=True` Bridge service with the
Bridge venv interpreter (`-I -B`) and called `bridge.ping` using
`CoreBridgeClientV2` from the independent Core venv interpreter (`-I -B`).
Result: `CORE_V2_PING_OK keys=status`; the Bridge exited with code 0 and its
socket was removed. The test used `FakeAdapter`, a temporary 0700 directory
under `/tmp`, and no production registration, Tunnel, or external service.
An initial probe passed an invalid caller-owned `workdir` parameter; it was
corrected to the client contract (the client supplies the workdir alias), and
the final process-boundary check passed.

## Retained scratch

The new wheelhouse and both venvs remain in the unique scratch directory for
inspection. Existing scratch directories were left untouched. The archives
remain dirty-tree `0.24.2` metadata artifacts and must not be represented as
v0.25.0 release artifacts.
