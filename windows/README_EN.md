# ServerFS MCP — Windows

**English** · [简体中文](README.md)

This is the independent Windows source tree for the v0.27.0 development candidate, not a released update. Operate existing deployments using the [v0.26.0 release](https://github.com/NTLx/ServerFS_MCP/releases/tag/v0.26.0) and matching source, artifacts and runbook.

## Native boundary

Windows 11 x64, local NTFS, Rust ABI3 filesystem kernel, current-user Task Scheduler, Job Objects and Named Pipe. No WSL, VM or administrator-owned service. Core owns MCP and workdir policy; optional Bridge owns host Agent execution and Repo Ops; Connectors have separate lifecycles and credentials. File-only mode requires neither Bridge nor Tunnel. Keep explicit grants, disjoint writable roots and private state.

## Develop and validate

From this Windows platform root in PowerShell, with native Python, uv and Rust installed:

```powershell
uv sync --frozen
cargo test --locked --manifest-path native/windows/Cargo.toml
uv tool run --from maturin==1.8.2 maturin build --release --features pyo3 --locked -m native/windows/Cargo.toml
$wheel = @(Get-ChildItem native/windows/target/wheels/serverfs_windows_native-0.26.0-cp312-abi3-win_amd64.whl)
if ($wheel.Count -ne 1) { throw 'Expected matching native Wheel' }
uv pip install --no-deps --python .venv/Scripts/python.exe $wheel[0].FullName
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build --wheel
Set-Location agent_bridge
uv sync --frozen
uv run pytest
uv build --wheel
```

Stop on every failed command. Full native lifecycle gates require an isolated Windows test account or host, not a production owner. Candidate metadata remains 0.26.0 during the mechanical split, but its artifacts must not be mixed with the released wheels.

## Local references

- [Native operations](deployment/windows/native-operations.md)
- [Agent Bridge](agent_bridge/README.md)
- [Configuration example](serverfs.toml.example)
- [Product documentation](https://ntlx.github.io/ServerFS_MCP/docs/)

Build and test inputs live in this tree. External contract and release gates are separate. Build success does not authorize native registration, migration or publication.
