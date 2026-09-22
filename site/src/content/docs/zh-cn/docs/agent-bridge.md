---
title: Agent Bridge
description: 可选的结构化 Codex 与 Claude 原生运行时委派。
---

Agent Bridge 是一个**可选的宿主机边界**。它让 ServerFS 可以暴露结构化 Agent 任务工具，而无需把 Codex 或 Claude 放进 MCP 容器。

```text
ChatGPT
  │
  ▼
ServerFS MCP
  │  结构化 Agent RPC
  ▼
Unix socket
  │
  ▼
宿主机 Agent Bridge
  ├── Codex
  └── Claude Code
```

## 为什么要独立存在

基础 `compose.yml` 不感知 Agent。需要 Agent 的部署显式叠加 `compose.agent.yml`。

这样可以保证：

- 没有原生 Agent runtime 时，文件系统 MCP 仍可独立使用
- 宿主机 socket 与 writer lock 不进入基础部署
- 安全边界在部署配置中保持可见
- 现有纯文件系统部署保持向后兼容

## Runtime 行为

启用 Agent 策略后，ServerFS 会暴露 8 个结构化 Agent 工具，覆盖 runtime 发现、任务提交、状态/事件、approval/question 处理、受支持 runtime 的 steering，以及任务取消。

这些工具**不是** Shell、argv 透传或通用命令执行器。

当前部署可形成：

- 19 个工具：文件系统 + Agent
- 21 个工具：文件系统 + 二进制 + Agent
