# ServerFS MCP — Linux

**简体中文** · [English](README_EN.md)

本目录是 v0.27.0 开发候选版本的 Linux 独立源码树，并非正式升级版本。现有部署请使用 [v0.26.0 正式版本](https://github.com/NTLx/ServerFS_MCP/releases/tag/v0.26.0)及匹配的源码、产物与运维手册。

## 原生边界

Linux amd64/arm64；Core 和可选接入层使用 Docker Compose v2.24+，Bridge 使用宿主机 systemd 用户服务。

Core 负责 MCP 接口和 workdir 策略；可选 Bridge 负责宿主机 Agent 与 Repo Ops；Connector 独立管理生命周期和凭据。纯文件模式不需要 Bridge 或 Tunnel。保留显式授权、不重叠的可写根目录及私有状态，不引入通用 Shell 或中央管理器。

## 开发与验证

在本目录执行，独立源码副本与仓库内子目录使用相同命令：

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build --wheel
cd agent_bridge
uv sync --frozen
uv run pytest
uv build --wheel
```

Core 镜像验证在平台根目录使用 Docker Compose 和独立临时镜像标签，不覆盖正式发布镜像。

## 本地参考

- [平台部署手册](deployment/linux/README.md)
- [Agent Bridge](agent_bridge/README.md)
- [配置示例](.env.example)
- [产品文档](https://ntlx.github.io/ServerFS_MCP/zh-cn/docs/)

构建和测试输入均位于本源码树；仓库级公共契约和发布检查独立执行。候选版本构建成功不代表授权注册原生服务、迁移已有实例或发布。
