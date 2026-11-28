---
title: Cloudflare Connector
description: 为已有 HTTP Core 接入独立 Cloudflare Tunnel。
---

Core 需要公网 HTTPS 路由但不需要直接公网端口时，可选择 Cloudflare。它是可选入口，独立于 Core 与 Bridge。Tunnel token 认证 cloudflared，而非客户端；调用方仍需独立 Core 准入。

## 选择目标

| 平台 | Origin（示例） | 指南 |
| --- | --- | --- |
| Linux | `http://serverfs-mcp:8000` | [Compose](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/deployment/linux/README.md) |
| macOS | `http://127.0.0.1:18781` | [原生](../macos-native/) — 纯文件或 owned Core |
| Windows | `http://127.0.0.1:18781` | [原生](../windows-native/) — 精确 Core 与 Connector ID |

使用对应 Connector 实际可达的 origin。在外部创建独立远程管理 Tunnel 与公网 hostname，不复用无关路由及 token。

## 部署与验证

1. 验证目标 Core、[API Key](../api-key/) 或 [OAuth](../oauth/) 准入及精确 Host 白名单。
2. 准备经过验证的 cloudflared 可执行文件或镜像，以及 Workdir 外的私有 token 文件。
3. 按平台的组件级流程配置，Core 就绪后仅启动 Connector。
4. 验证 Provider 注册、TLS、认证后的 MCP initialize/tools/list、安全文件访问及调用方拒绝行为。

仅重启或退役选定 Connector，确认无关组件身份不变，之后建立新会话。公网可达和长连接 SSE 需要分别测试。

私有凭据、路由配置、轮换与退役见 [Cloudflare 运维手册](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/cloudflare-tunnel.md)，排障见 [Tunnel 诊断](../tunnel-diagnostics/)。
