---
title: Jev Advisors
description: 在 Agent 任务质量、运行时路由和 provider 审批中按需使用 TypeSafe Jev 提供结构化建议。
---

ServerFS 可以在**宿主机 Agent Bridge** 中按需启用 [TypeSafe Jev](https://docs.typesafe.ai/introduction)，作为结构化的决策辅助层。

Jev 属于 System One 模型：它不是生成面向人类阅读的长文本，而是针对共享 state 评估带类型的问题，直接返回 Choice 概率/置信度、Noul 等结构化结果。ServerFS 利用这一特性做窄范围 advisory judgment，并把真正的控制逻辑继续留在确定性代码里。

这套集成始终是 **opt-in、advisory-only、fail-open**。Jev 不是 Agent runtime，不是授权源，也不是安全边界。

> **状态：**这套 opt-in 实验能力在 v0.6.0 引入，并继续包含在当前 v0.7.2 稳定版中；它只修改宿主机 Agent Bridge，不改变 MCP 公共工具 schema。

## 启用方式

在仓库现有、未跟踪的 `.env` 中配置 TypeSafe API Key：

```text
SERVERFS_JEV_API_KEY=<your key>
```

留空即可关闭全部 Jev 能力。关闭时：

- 不构造 TypeSafe client；
- 不产生 Jev 网络请求；
- Agent task 与 approval 行为回到完全不依赖 Jev 的路径。

安装器只会把已配置的 Key 渲染到用户自有、权限为 `0600` 的 Agent Bridge 配置中；Key 不会进入 MCP 容器。

## 当前模型契约

ServerFS 当前固定使用：

```text
jev-1.13.0
typesafe-sdk==0.7.1
```

这里故意固定版本而不是使用 `jev-latest`，以便在升级前对评分与置信度进行可重复校准。

TypeSafe 当前文档说明 Jev 1.13 只接受文本输入，总请求预算为 64k，同时 state + 单个最长 question 还有 32k 上限；英语是主要训练语言、准确性最好，CJK 可以处理但应在自己的真实数据上验证。因此 ServerFS 始终把这些分数和推荐视为“证据”，而不是“权限”。

## 三项 advisory 能力

### Agent Task Preflight

每次启用 Jev 的 `submit_agent_task` 可以评估：

- 是否只有一个窄目标；
- 是否明确限定修改范围；
- 是否明确停止条件；
- 是否明确要求可验证证据；
- 任务更适合 ServerFS 结构化 primitive，还是需要原生 Agent。

### Runtime Router

同一次任务提交 Jev 请求还会给出一个路由建议：

- `direct_serverfs_tool`
- `codex`
- `claude`
- `human_review`

它**不会**增加 `runtime=auto`。调用者显式选择的 runtime，以及 workdir 上的确定性 runtime allowlist，仍然具有最终约束力。

### Approval Advisor

如果 Codex 或 Claude 后续真的生成 provider approval request，ServerFS 才可能针对这个具体 approval 再发起一次 Jev 请求。

它评估：

- 该权限是否确实是完成已授权目标所必需；
- 权限范围是否足够窄；
- 是否具有破坏性或不可逆风险；
- 是否涉及敏感访问；
- 是否会产生外部副作用；
- 给出 `approve_once`、`approve_session`、`deny`、`cancel_task` 或 `review_carefully` 等建议。

真正的 provider request 仍会保持阻塞，直到调用方通过原有 approval 工具显式响应。Jev 自己不会完成批准或拒绝。

## 请求数量

ServerFS 刻意减少 Jev 请求：

```text
submit_agent_task
  └─ 1 次 Jev 请求
       ├─ Preflight
       └─ Runtime Router

是否出现 provider approval？
  ├─ 否 -> 0 次额外 Jev 请求
  └─ 是 -> 1 次 approval-specific Jev 请求
              └─ 同一 task 内完全相同 approval -> 复用缓存
```

Preflight 与 Runtime Router 共用一次 `system_one`，因为 Jev 可以针对同一 state 独立并行评估多个 typed question。Approval Advisor 必须稍后运行，是因为任务提交时具体的 provider approval 对象还不存在。

## 数据最小化

启用 Jev 并不意味着 ServerFS 会把 workdir 内容发送给 Jev。

任务级评估只提供 advisory question 所需的任务上下文。Approval 评估使用已经过 Bridge redaction 的 approval 对象，只保留 allowlist 字段，并再次清洗明显的 secret/token/password/credential 字段和常见模式。

任何 Jev 结果都不能削弱：

- workdir/path 授权；
- runtime allowlist；
- writer lease；
- provider 原生审批语义；
- MCP 传输与网络隔离；
- provider 自身安全检查。

## 失败行为

如果 TypeSafe 不可用、超时、限流或返回异常结构，advisor 会返回 `status=unavailable`。

已经通过确定性授权的 Agent task 或等待中的 provider approval 仍走 ServerFS 原有路径。Fail-open 的含义只是“缺少 advisory context”，不是“获得更宽权限”。

## 当前验证状态

目前已经用真实调用验证过：

- 有界文件读取；
- Git / pytest 任务；
- 明确的 Codex / Claude provider 选择错误；
- 应交由人工判断的任务；
- 故意捆绑或宽泛的 prompt；
- 一次性有界写入；
- 重复的 session 范围写入；
- workdir 外部读取 approval。

项目目前仍然**没有**启用自动路由或自动 approval threshold。

上游资料：[Introduction](https://docs.typesafe.ai/introduction)、[Models](https://docs.typesafe.ai/models) 与 [API reference](https://docs.typesafe.ai/api)。

实现细节与实测数据见仓库：

- [Agent Task Preflight](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/jev-agent-preflight-experiment.md)
- [Runtime Router](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/jev-runtime-router-experiment.md)
- [Approval Advisor](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/jev-approval-advisor-experiment.md)
