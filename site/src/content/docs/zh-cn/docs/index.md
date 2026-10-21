---
title: ServerFS MCP
description: 面向 Linux、Windows 与 macOS 的安全、标准优先 MCP 文件系统服务。
---

ServerFS MCP 通过 Model Context Protocol，将你明确配置的目录（Linux 容器为既有形态，v0.10.0 起支持 Windows 原生，v0.13.0 起支持 macOS 原生）暴露为**受控 workdir**。

当前稳定版本：**v0.19.0**。ServerFS 现在以标准 MCP 为中心：兼容 MCP 的宿主可使用原生 stdio 或标准 Streamable HTTP；OpenAI Secure MCP Tunnel 继续作为可选的 ChatGPT 入口。Linux、Windows 与 macOS 继续共享同一套文件系统、二进制与 Agent 能力契约。

它**默认只读**。管理员可以按 workdir 显式启用受控文件写入、有界整文件二进制传输，以及隔离且独立门控的 ChatGPT 文件参数入口。宿主机 Agent Bridge 仍然是可选能力；Codex、Claude 和 Qoder 原生 runtime 及可配置 ACP v1 profile 提供 Agent 委托；provider-neutral 模型发现与单次任务模型覆盖仍取决于 runtime，同时可按需启用 advisory-only 的 TypeSafe Jev 支持。Windows 始终保持原生形态：预构建 Rust/NTFS 内核 wheel 之上的 MCP stdio 服务，无需 Docker、WSL 或 MSVC；v0.14.0 进一步让正常 Windows 生命周期不再依赖终端/会话存活。

## 能力面

| 能力面 | 工具数 | 启用方式 |
| --- | ---: | --- |
| 文件系统 | 11 | 基础部署 |
| 文件系统 + 二进制 | 13 | 启用二进制传输 |
| 文件系统 + Agent | 21 | 启用 Agent overlay |
| 完整能力 | 23 | 同时启用二进制 + Agent |

ServerFS 不提供 Shell、通用命令执行器、递归删除或无保护覆盖。

## 从这里开始

- [快速开始](./getting-started/) — 先使用标准 MCP 连接；仅在需要 ChatGPT Tunnel 路径时再启用 OpenAI Tunnel。
- [配置](./configuration/) — 定义 workdir 与每个 workdir 的最终生效策略。
- [架构](./architecture/) — 理解 MCP Core、传输/入口适配器、部署边界与 Agent Bridge。
- [安全模型](./security/) — 查看纵深防御模型。
- [静态 API Key](./api-key/) — 使用一个可选共享 bearer secret 保护 Linux 远程 Streamable HTTP。
- [OAuth 2.1](./oauth/) — 使用可选 OAuth、身份联合与精确稳定 principal 准入保护 Linux 远程 Streamable HTTP。
- [二进制传输](./binary-transfer/) — 启用有界 download/upload，以及可选的 ChatGPT 文件参数入口。
- [Agent Bridge](./agent-bridge/) — 按需启用结构化 Codex/Claude/Qoder 与可配置 ACP profile 委派。
- [Windows 原生部署](./windows-native/) —— Windows 11 x64 + 本地 NTFS 原生部署，包括当前用户持久服务生命周期、健康检查、Tunnel bootstrap 与 Agent 委派。
- [macOS 原生部署](./macos-native/) —— Apple M 系列 + macOS 27 原生部署，包括 Darwin 文件系统边界、launchd Agent Bridge 与平台门控。
- [Jev Advisors](./jev-advisors/) — 按需启用实验性的 task preflight、五路 runtime routing、提交前模型建议与 approval advice；v0.12 新增独立、显式的 Jev 代理路由。

本文档站是面向用户与运维的权威参考；仓库 README 有意保持为简洁的项目入口。

ACP 完整配置、验证、任务生命周期与排障请见 [ACP 指南](./acp/)。
