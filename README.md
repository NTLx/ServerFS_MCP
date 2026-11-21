# ServerFS MCP

**简体中文** · [English](README_EN.md)

[![Release](https://img.shields.io/github/v/release/NTLx/ServerFS_MCP)](https://github.com/NTLx/ServerFS_MCP/releases/latest) · [官网](https://ntlx.github.io/ServerFS_MCP/zh-cn/) · [文档](https://ntlx.github.io/ServerFS_MCP/zh-cn/docs/) · [Releases](https://github.com/NTLx/ServerFS_MCP/releases)

**本文档面向 v0.25.0，正式发布尚未完成。** [最新稳定版本](https://github.com/NTLx/ServerFS_MCP/releases/latest)。

ServerFS MCP 是一个跨平台、自托管的 MCP 文件系统服务。它将明确配置的本地目录（Workdir）开放给 AI 客户端，默认只读，写入具有 revision 保护；按需增加二进制传输、确定性仓库操作（Repo Ops）和宿主机 Agent 委托。不提供 Shell 或通用命令执行接口。

## 按需组合，而非整套安装

```text
MCP 客户端 ── 直接 HTTP / 可选 Tunnel Connector ── MCP Core ── Workdir
                                                    │
                                              可选 Agent Bridge
                                                    │
                                           宿主机 Agent / Repo Ops
```

- **MCP Core** 管理 MCP 会话、HTTP 调用方准入、Workdir 策略和文件操作。原生本地 stdio 不需要持久 HTTP 服务。
- **Connector** 是可选的 OpenAI 或 Cloudflare Tunnel 入口，独立管理其入口凭据与生命周期；直接 HTTP 不是 Connector。
- **Agent Bridge** 是可选的宿主机执行边界。Agent 委托和 Repo Ops 分别按 Workdir 授权，不会因为安装 Bridge 而自动开启。

持久部署可以有多套命名 Core（`instance_id`）、各自的入口（平台支持时以 `connector_id` 区分）和独立 Bridge owner（`bridge_id`）。Core 的 Bridge 模式是：

| 模式 | 选择理由 |
| --- | --- |
| disabled | 只需要文件能力，无需 Agent Bridge 或 Provider 凭据 |
| owned | 需要专属 Bridge，隔离任务、状态和运行时 |
| shared | 多个 Core 显式接入同一宿主机上已有的独立 Bridge owner |

Shared Core 不拥有 Bridge owner 的生命周期，也不能通过共享用户或 IPC 获得额外授权。独立 Bridge 不得占用重叠的可写根目录。组件独立不意味着没有依赖：启动与退役须符合各平台就绪、活动任务和停止门禁。

## 选择平台

| 平台 | 部署方式 | 指南 |
| --- | --- | --- |
| Linux | Docker Compose 管理命名 Core 和可选 Connector；可用直接 HTTP 或内部网络；可选 Bridge 为宿主机用户级 systemd 服务 | [Linux 部署](deployment/linux/README.md) |
| macOS | Apple Silicon、原生 arm64、macOS 27、APFS；签名 SMAppService / LaunchAgents 与认证 Unix IPC | [macOS 部署](deployment/macos/README.md) |
| Windows | Windows 11 x64、本地 NTFS、匹配 Native Wheel；当前用户 Task Scheduler、Job Object 和 Named Pipe，无需 WSL/VM | [Windows 部署](deployment/windows/v0250-native-operations.md) |

统一的是公开 MCP 契约与安全结果，不强求三个平台共用内部实现。具体组件组合、命名 Connector 支持范围、安装与升级限制以平台指南为准。不引入中央服务管理器。

## 能力与安全

基础文件系统提供 11 个工具。启用二进制传输增加 2 个；Agent 委托增加 10 个；Repo Ops 增加 4 个。这些能力独立启用，完整组合共 27 个工具。Repo Ops 不启动 Agent；Agent 使用宿主机的原生 Codex、Claude、Qoder 或显式配置的 ACP profile，Jev 仅提供建议。

Workdir 限制、deny/reserved 路径策略、revision 检查与写入授权不因入口变化而扩大。HTTP Core 配置精确 Host/Origin 准入；公网接入须有调用方认证。Tunnel 凭据与 HTTP 调用方凭据分离。Bridge 接入需要逐 Core 的显式授权，不将同 UID/SID 视为可信身份。

## 开始使用

从最小纯文件实例开始，先验证 MCP 文件能力，再按需增加 Connector、Bridge 和其他能力。

- [架构与部署选择](https://ntlx.github.io/ServerFS_MCP/zh-cn/docs/architecture/) · [快速开始](https://ntlx.github.io/ServerFS_MCP/zh-cn/docs/getting-started/)
- [HTTP API Key](docs/api-key.md) · [OAuth](docs/oauth.md) · [Cloudflare Connector](docs/cloudflare-tunnel.md)
- [Agent Bridge](agent_bridge/README.md) · [Agent Skills](skills/README.md)（七个 Skills 与仅包含 Skills 的 Plugin）
- [维护者指南](docs/maintainers/README.md)

使用与目标版本匹配的源码、文档及安装产物，避免隐式迁移现有实例。[发布说明](docs/releases/v0.25.0.md)与[发布清单](docs/v0.25.0-release-checklist-2026-11-21.md)记录实际验收及未完成门禁，历史证据保留于 `docs/releases/`、`docs/diagnostics/` 和架构决策记录。
