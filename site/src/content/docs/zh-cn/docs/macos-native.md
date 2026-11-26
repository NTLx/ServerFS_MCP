---
title: macOS 原生部署
description: 在 Apple Silicon 上选择原生 stdio 或命名 HTTP 实例。
---

ServerFS 支持 Apple Silicon、原生 arm64、macOS 27.x 与本地 APFS，遵循当前用户的文件权限和 macOS 隐私保护。持久 HTTP 服务通过签名的 SMAppService / LaunchAgents 管理，无需 Docker、Rosetta 或 root LaunchDaemon。

## 选择合适的模式

| 模式 | 适用场景 |
| --- | --- |
| 本地 stdio | MCP 客户端在本机，无需持久服务 |
| 纯文件 Core | 需要认证后的 HTTP 文件访问，不需要 Bridge |
| Owned Bridge | Core 需要专属 Agent 或 Repo Ops 执行边界 |
| Shared Bridge | 多个 Core 接入已有的 v2 Bridge owner；每个 Core 支持 1–16 个 owner 授权的 Agent Workdir；不支持 Repo Ops |

纯文件与 owned Core 可按需接入 OpenAI 和 Cloudflare Connector。Shared Core 目前没有 Connector staging 接口。Owned pair 默认使用 v1 专属本机 socket 和策略校验。新建 Owned 实例可通过 `owned-stage --agent-protocol v2` 显式选择签名 v2，逐别名授予 Agent/Repo Ops 权限；不能原地切换已有实例协议。共享接入使用独立签名 v2 grant。

## 部署步骤

完整命令和可参考的配置示例见 [macOS 运维手册](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/macos/README.md)。

1. 准备版本匹配的 Core 环境（如需 Bridge 则另外准备其环境），构建签名模板 App。
2. 创建定义目标 Workdir 的最小原生 TOML。若使用持久 HTTP，准备独立端口和私有 API Key 或 OAuth 配置。Host 白名单必须精确匹配所配置的 hostname 及其后的 loopback authority。
3. 使用命名 CLI（`python -m serverfs_mcp.darwin_instance_cli --instance <id>`）准备所选模式，再显式启动。Shared Core 还需经过验证的独立 runtime 及 owner 签发的 binding，并在启用前执行 preflight。
4. 如系统要求，在系统设置中批准登录项。先验证 Core，再启动可选 Connector。

同一 Provider 的额外 Connector ID 需在纯文件/owned Core staging 时预声明。OpenAI HTTP 使用独立控制面密钥文件与静态 Core bearer 文件；Tunnel 凭据不能替代调用方认证。

## 验证与运维

检查匿名请求拒绝、认证后 MCP initialize、安全的 Workdir 访问及显式开启的 Agent/Repo Ops 能力。Job 注册成功不能证明公网可达。按精确身份操作 Connector，确认无关组件未停止，并在重启后建立新会话。

先停 Connector，再停相应 Core 或 owned pair。Shared Core 不管理 Bridge owner。命名 staging 命令用于新建实例，已有部署应制定明确的迁移或回滚方案。发布验收及未完成限制见[发布清单](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.26.0-release-checklist.md)。
