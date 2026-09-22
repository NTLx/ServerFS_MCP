---
title: ServerFS MCP
description: 让 ChatGPT 与 AI Agent 安全、受控地访问 Linux workdir。
---

ServerFS MCP 通过 Model Context Protocol，将你明确配置的 Linux 目录暴露为**受控 workdir**。

它**默认只读**。管理员可以按 workdir 显式启用受控文件写入和有界整文件二进制传输；v0.5.0 还增加了隔离且独立门控的 ChatGPT 文件参数入口。用于调用 Codex 或 Claude 的宿主机 Agent Bridge 仍然是可选能力。

## 能力面

| 能力面 | 工具数 | 启用方式 |
| --- | ---: | --- |
| 文件系统 | 11 | 基础部署 |
| 文件系统 + 二进制 | 13 | 启用二进制传输 |
| 文件系统 + Agent | 19 | 启用 Agent overlay |
| 完整能力 | 21 | 同时启用二进制 + Agent |

ServerFS 不提供 Shell、通用命令执行器、递归删除或无保护覆盖。

## 从这里开始

- [快速开始](./getting-started/) — 使用 OpenAI Secure MCP Tunnel 部署基础服务。
- [配置](./configuration/) — 定义 workdir 与每个 workdir 的最终生效策略。
- [架构](./architecture/) — 理解容器、Tunnel 与 Agent Bridge 的边界。
- [安全模型](./security/) — 查看纵深防御模型。
- [二进制传输](./binary-transfer/) — 启用有界 download/upload，以及可选的 ChatGPT 文件参数入口。
- [Agent Bridge](./agent-bridge/) — 按需启用结构化 Codex/Claude 委派。

实现细节与完整运维参考请查看仓库 [README](https://github.com/NTLx/ServerFS_MCP#readme)。
