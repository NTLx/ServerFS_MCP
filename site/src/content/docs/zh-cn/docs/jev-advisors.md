---
title: Jev Advisors
description: 为 Agent 任务、路由、模型选择与审批提供可选的 TypeSafe Jev 建议。
---

ServerFS 可在宿主机 Agent Bridge 中启用 [TypeSafe Jev](https://docs.typesafe.ai/introduction)，针对任务上下文进行结构化评估，而不执行 Agent 工作。它需要显式启用，仅提供建议，既不是 Agent runtime，也不是授权边界。

## 决定是否启用

启用 Jev 意味着允许任务上下文离开宿主机并发送至外部 TypeSafe API。未经授权不得启用。不要在任务 prompt 中放入凭据、私有文件内容或不必要的个人信息。Bridge 不会自动上传 workdir 文件，但任务提交时的 Preflight 会发送原始 prompt，不经过模型与审批顾问额外使用的文本清洗器。

禁用时不构造 Jev client、不发送顾问请求。Agent 执行及 Provider 显式审批不依赖 Jev。

## 配置 owner

在所选 Bridge owner 的私有配置中设置 Jev，配置必须位于对外 workdir 之外。Linux 命名 owner 使用如下私有 Bridge JSON 片段；它不是 Core 环境变量，也不是完整 owner 配置：

```json
{
  "jev": {
    "api_key": "<私有 TypeSafe API Key>",
    "use_proxy": false
  }
}
```

省略 `jev` 或留空 Key 即关闭顾问。按 [Linux owner provision 流程](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/deployment/linux/README.md)使用受保护的配置。旧 Linux 单实例渲染器使用私有 env 文件中的 `SERVERFS_JEV_API_KEY` 和 `SERVERFS_JEV_USE_PROXY` 并渲染至 Bridge JSON。macOS 旧版原生配置 helper 也识别这些变量，但命名 macOS owned staging 不传递该私有 overlay。不能因 Bridge parser 支持 Jev 就推断原生 helper 支持其 provision。遵循 [macOS](../macos-native/) 或 [Windows](../windows-native/) 实际支持的 owner 流程，不直接覆盖已安装 owner 快照或共享 grant。

当 `jev.use_proxy=true` 时，私有 JSON 必须配置有效共享 `proxy`。Jev 使用显式 HTTP client，不修改进程级代理变量。为 false 时 ServerFS 不传入显式 Jev 代理；涉及出站隔离时应核对 owner 环境与实际路由。仅 Jev 使用时可采用带认证的共享端点；如果 Agent 也使用同一端点，则必须无凭据。Tunnel 路由独立。

Bridge 固定 `jev-1.13.0` 与 `typesafe-sdk==0.7.1`。模型升级需要单独校准。Client 配置 5 秒超时与重试策略（`max_retries=1`，超时 8 秒）；一次逻辑顾问调用不一定只有一次网络尝试。

## 四项顾问能力

| 顾问 | 触发时机 | 输出与权限 |
| --- | --- | --- |
| Task Preflight | 已授权 Agent 提交 | 评估目标是否单一、范围与停止条件是否明确、是否可验证，以及执行方式是否适合。 |
| Runtime Router | 同一次提交请求 | 建议 `direct_serverfs_tool`、`codex`、`claude`、`qoder` 或 `human_review`；没有自动或 ACP 路由选项。 |
| Model Advisor | 模型发现时显式传入任务上下文 | 推荐符合条件的原生模型，不自动改变提交。 |
| Approval Advisor | 实际 Provider approval | 评估必要性、范围、不可逆风险、敏感访问和外部副作用，不代替审批。 |

Task Preflight 与 Runtime Router 在执行前共享一次逻辑评估。Router 的固定候选及判断规则不是当前能力发现，也不覆盖所有结构化操作（含 Repo Ops）。以实际工具、grant、策略及 Provider 能力决定执行路径。

需要模型建议时先选择已授权 runtime。调用 `list_agent_models` 时同时传入 `workdir` 与 `task_prompt`，可选相对 `path` 和 `profile`；仅查模型目录时不传任务上下文。Codex、Qoder 支持原生目录，Claude、ACP 不支持。顾问只考虑 1–32 个互不重复、未隐藏、未禁用的候选 ID，使用实际提供的元数据，不凭模型名称推断成本或质量。完成时返回 `recommended_model`、置信度/概率及 `automatic=false`。只有 runtime 支持时才由调用方显式提交所选模型。

只对真实 approval 提供审批建议。ACP 等采用的 Provider 专用 `response_options` 列表不适用此通用顾问。对支持的审批结构，可以建议 `approve_once`、`approve_session`、`deny`、`cancel_task` 或 `review_carefully`。须核对 `recommended_decision_available` 与实际待处理请求，不把建议当作可用选项或权限。调用方通过 `respond_agent_approval` 显式回应，或遵循交互超时/取消流程。

## 请求与数据边界

- Preflight/Router：runtime、别名、相对路径、profile、续接标记及原始任务 prompt。
- Model Advisor：任务上下文和标准化候选模型元数据；文本经过清洗与限界。
- Approval Advisor：任务上下文及已做路径脱敏的 approval 的白名单字段，再清洗常见敏感模式。仅同一任务内相同 approval 的已完成建议被缓存。

模型与审批顾问的清洗器将字符串限制为 4096 字符后添加截断标记，列表最多 50 项。这是尽力过滤，不保证保密。不同 approval 可分别产生请求；失败的评估不缓存。重试可能增加网络尝试次数。模型建议不需要 Provider 原始 payload 或 workdir 扫描。

## 失败行为与验证

顾问超时、限流、响应无效等失败返回 `unavailable`；没有候选目录或不支持的审批结构可返回 `not_applicable`，模型建议也可能返回 `disabled`。其他已授权 Agent 任务或待处理审批按正常流程继续。Fail-open 只代表缺少建议，不扩大权限或自动批准。配置校验是独立机制，仍可失败关闭。

按所选 owner 指南验证私有配置与就绪状态。在授权测试任务中检查 preflight/routing 和 approval advice 事件或 model advice 结果，不打印 Key。实际 Provider/网络验收须独立于源码支持记录。

历史评估记录：[Task Preflight](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/jev-agent-preflight-experiment.md)、[Runtime Router](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/jev-runtime-router-experiment.md)、[Approval Advisor](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/jev-approval-advisor-experiment.md)。它们不代表当前命名实例验收。任务操作见 [Agent Bridge](../agent-bridge/)，上游模型限制见 [TypeSafe 模型文档](https://docs.typesafe.ai/models)。
