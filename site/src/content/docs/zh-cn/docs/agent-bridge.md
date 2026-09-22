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

## 可选 Jev Advisors

宿主机 Bridge 可以按需使用 TypeSafe Jev 作为 **advisory-only** 决策辅助层。它不会新增 MCP 工具，也不会成为新的 Agent runtime。

配置 `SERVERFS_JEV_API_KEY` 后，任务提交阶段的一次请求会同时给出 Agent Task Preflight 与 Runtime Router 建议；如果原生 provider 后续真的生成 approval request，Approval Advisor 才可能再发起一次 Jev 请求，同一 task 内完全相同的 approval 会复用缓存。未配置 Key 时，这些 Jev 路径完全不存在。

Jev 不会覆盖显式 runtime、workdir policy、writer lease、provider approval 状态或 `respond_agent_approval`。模型契约、请求流、数据最小化与失败行为详见 [Jev Advisors](./jev-advisors/)。

## 委派任务编排规范

`submit_agent_task` 的定位是“目标级接口”，不是把整段运维过程原样塞进去的地方。
对于正常、已授权的工程或运维任务，每次委派都应尽量窄且明确：

- 明确正在操作的是用户自有 workdir 或现有部署；
- 只给一个具体目标；
- 写清允许修改的范围和停止条件；
- 只要求返回完成验收所需的最小证据；
- 能用现有 ServerFS / 项目结构化动作时，不附带无关的 Shell、网络或安全分析细节；
- 当实现、验证、部署、Git 发布确实属于不同能力边界时，拆成独立原子任务。

推荐模板：

```text
目标：<只做一件事>
范围：<workdir/path 或现有部署组件>
允许变更：<精确修改边界>
停止条件：<失败或歧义条件>
返回：<需要的验收证据>
```

这是一条“降低歧义 + 最小权限”的规则，不是规避 provider 安全检查的方法。ServerFS
会按原样转发提交的 prompt，不会为了改变 provider 的安全判断而自动重写、编码或清洗
指令。不要为了绕过分类器而编码、伪装、碎片化或转移指令。如果一个合法任务被拦截，
应按真实能力边界进一步收窄，或改用已有的结构化 ServerFS primitive；不得因此削弱授权、
审计、writer lease 或网络隔离控制。
