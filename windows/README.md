# ServerFS MCP — Windows

**简体中文** · [English](README_EN.md)

本目录是 v0.27.0 开发候选版本的 Windows 独立源码树，并非正式升级版本。现有部署请使用 [v0.26.0 正式版本](https://github.com/NTLx/ServerFS_MCP/releases/tag/v0.26.0)及匹配的源码、产物和运维手册。

## 原生边界

Windows 11 x64、本地 NTFS、Rust ABI3 文件系统内核、当前用户 Task Scheduler、Job Objects 和 Named Pipe。不依赖 WSL、虚拟机或管理员服务。Core 负责 MCP 与 workdir 策略；可选 Bridge 负责宿主机 Agent 与 Repo Ops；Connector 独立管理生命周期和凭据。纯文件模式不需要 Bridge 或 Tunnel。保留显式授权、不重叠的可写根目录与私有状态。

## 开发与验证

安装原生 Python、uv 和 Rust 后，在本平台根目录使用 PowerShell：

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

每步失败即停止。完整原生生命周期门禁需要隔离的 Windows 测试账户或主机，不能使用生产 owner。机械拆分阶段仍保留 0.26.0 元数据，但候选产物不可与正式版本 Wheel 混用。

## 本地参考

- [原生组件运维手册](deployment/windows/native-operations.md)
- [Agent Bridge](agent_bridge/README.md)
- [配置示例](serverfs.toml.example)
- [产品文档](https://ntlx.github.io/ServerFS_MCP/zh-cn/docs/)

构建与测试输入均位于本源码树；仓库级公共契约和发布门禁独立执行。构建成功不代表授权注册原生服务、迁移或发布。
