---
title: ServerFS MCP
description: 让 ChatGPT 与 AI Agent 在 Linux、Windows 与 macOS 上安全、受控地访问 workdir。
---

ServerFS MCP 通过 Model Context Protocol，将你明确配置的目录（Linux 容器为既有形态，v0.10.0 起支持 Windows 原生，v0.13.0 起支持 macOS 原生）暴露为**受控 workdir**。

当前稳定版本：**v0.13.0**。该版本新增 macOS 原生部署——**运行 macOS 27 Golden Gate 的 Apple M 系列 Mac、原生 arm64、无需 Docker 与 Rosetta**——包括 Darwin FD 文件系统后端（`fcopyfile` 元数据保留、FD 安全搜索）、launchd 用户代理下经 `getpeereid` 认证的 AF_UNIX Agent Bridge，以及原生 AF_UNIX file-ingress helper，同时保持既有 Linux 与 Windows 原生部署及公开工具面不变。

它**默认只读**。管理员可以按 workdir 显式启用受控文件写入、有界整文件二进制传输，以及隔离且独立门控的 ChatGPT 文件参数入口。宿主机 Agent Bridge 仍然是可选能力；v0.9.0 支持 Codex、Claude 和 Qoder，并在原生 runtime 支持时提供 provider-neutral 模型发现与单次任务模型覆盖，同时可按需启用 advisory-only 的 TypeSafe Jev 支持。v0.10.0 新增 Windows 原生部署：预构建 Rust/NTFS 内核 wheel 之上的 MCP stdio 服务，无需 Docker、WSL 或 MSVC。

## 能力面

| 能力面 | 工具数 | 启用方式 |
| --- | ---: | --- |
| 文件系统 | 11 | 基础部署 |
| 文件系统 + 二进制 | 13 | 启用二进制传输 |
| 文件系统 + Agent | 21 | 启用 Agent overlay |
| 完整能力 | 23 | 同时启用二进制 + Agent |

ServerFS 不提供 Shell、通用命令执行器、递归删除或无保护覆盖。

## 从这里开始

- [快速开始](./getting-started/) — 使用 OpenAI Secure MCP Tunnel 部署基础服务。
- [配置](./configuration/) — 定义 workdir 与每个 workdir 的最终生效策略。
- [架构](./architecture/) — 理解容器、Tunnel 与 Agent Bridge 的边界。
- [安全模型](./security/) — 查看纵深防御模型。
- [二进制传输](./binary-transfer/) — 启用有界 download/upload，以及可选的 ChatGPT 文件参数入口。
- [Agent Bridge](./agent-bridge/) — 按需启用结构化 Codex/Claude/Qoder 委派。
- [Windows 原生部署](./windows-native/) —— Windows 原生形态：两个隔离环境中的预构建 wheel、stdio 传输、pinned tunnel-client 启动链、fail-closed 健康报告，以及（v0.11）Codex/Claude/Qoder 的 Agent 委派。
- [Jev Advisors](./jev-advisors/) — 按需启用实验性的 task preflight、五路 runtime routing、提交前模型建议与 approval advice；v0.12 新增独立、显式的 Jev 代理路由。

实现细节与完整运维参考请查看仓库 [README](https://github.com/NTLx/ServerFS_MCP#readme)。
