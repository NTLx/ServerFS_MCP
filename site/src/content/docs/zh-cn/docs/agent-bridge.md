---
title: Agent Bridge
description: 可选的宿主机 Agent 委派与确定性 Repo Ops。
---

Agent Bridge 在宿主机运行，独立于 MCP Core 和 Connector，负责 Agent 执行、任务状态、写入协调与 Repo Ops。

## 选择接入模式

| 模式 | 适用场景 |
| --- | --- |
| `disabled` | 纯文件服务，无需 Bridge。 |
| `owned` | 一个 Core 拥有专用 Bridge。 |
| `shared` | 独立管理的 Bridge 为同一宿主机上显式授权的 Core 提供服务。 |

Linux 命名实例使用宿主机用户级 systemd 服务及签名 Unix Socket 接入。macOS 使用原生 SMAppService 和 Unix Socket；owned 模式使用私有 manifest、策略与 IPC 校验，而不是共享 v2 grant。Windows 使用原生计划任务和 Named Pipe 授权。部署见 [Linux 指南](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/linux/README.md)、[macOS](../macos-native/) 或 [Windows](../windows-native/)。

共享 Core grant 限制允许的方法与 workdir 别名。共享 Core 无权启动、停止或退役 owner。相同 UID/SID 不代表授权；独立 owner 的可写目录不得重叠。owner 的凭据、状态与配置必须位于对外 workdir 之外。

## 按需启用能力

Core 默认提供 11 个文件工具。二进制传输增加 2 个，Agent 委派增加 10 个，[Repo Ops](../repo-ops/) 独立增加 4 个。仅启用 Repo Ops 也需要 Bridge，但不需要可用的 Agent Provider，更不会创建 Agent 任务。

Agent 委派不是 Shell 或任意 argv 接口。Codex、Claude、Qoder 及已配置的 [ACP profile](../acp/) 需要授权的可写 workdir 和 `workspace-write` 策略。并非各平台的所有接入方式都支持全部能力，例如 macOS 命名共享 Core 在 v0.25.0 支持一个 Agent workdir，在 v0.26.0 候选版支持 1–16 个 owner 授权的 Agent workdir；仍不支持 Repo Ops。

## 提交与跟踪任务

1. 调用 `list_workdirs` 确认别名、访问权限、Agent 策略与 runtime 白名单；通过 `list_agent_runtimes` 查看当前能力。发现操作不会安装或启动 Provider。
2. 可选调用 `list_agent_models`。Codex 使用 runtime 模型目录，Qoder 使用当前账户目录；Claude 和 ACP 明确报告不支持发现。发现操作不启动推理。
3. 使用 `submit_agent_task` 提交一个已授权目标，写清允许变更范围、停止条件与验收证据。可选 `model` 只影响本次任务，不传则沿用原生默认或续接会话行为。无效选择不会被静默替换。
4. 通过 `get_agent_task` 查询状态，通过 `read_agent_task_events` 按游标读取有界事件。遇到实际 Provider 请求时使用 `respond_agent_approval` 或 `answer_agent_question`；ServerFS 不自动批准。只有明确支持实时 steering 的 runtime 才使用 `send_agent_message`。
5. 读取结果并独立验证目标是否完成；确需取消时使用 `cancel_agent_task`。

兼容 v1 的接入路径在提交响应丢失时，可使用相同的可选 `idempotency_key` 和相同语义重试，返回保留期内的原任务，不重复启动推理轮次。同 key 对应不同内容返回 `AGENT_IDEMPOTENCY_CONFLICT`。签名 v2 Core grant（Linux 命名、macOS shared、启用 grant 的 Windows）目前拒绝 `idempotency_key` 和 `continue_from_task_id`，即使 runtime 声明支持会话续接也不例外。此类路径省略两字段，提交响应不确定时不可盲目重试。可选 `correlation_id` 用于关联诊断，不用于去重。任务记录执行 manifest，标准化事件使用 v1 envelope。

Qoder 支持原生会话续接、approval、question 与取消，但不支持实时 steering，也不能在 Bridge 重启后重新附着到旧的运行中进程。以实际声明的 runtime 能力为准，不从其他 Provider 类推。

## 限制与恢复

| 配置项 | 默认值 |
| --- | --- |
| 任务超时 | 2 小时 |
| Approval/question 超时 | 30 分钟 |
| 活跃任务上限 | 4 |
| 终态保留期 | 7 天 |
| 内联结果阈值 | 256 KiB |
| 持久化结果上限 | 8 MiB |

这些 Bridge 策略不同于 Core RPC 超时与 Provider event-idle 超时。命名 owner 使用平台对应的私有配置；Linux 兼容渲染器将相应的 `SERVERFS_AGENT_*` 环境变量映射到 Bridge 配置。

结果先标准化并对 workdir 路径脱敏，再持久化，不是 Provider 原始日志。超过内联阈值时，`get_agent_task` 返回有界预览、字节数和 SHA-256；通过 `read_agent_task_result` 分块读取存储的准确 UTF-8 文本（每块最多 64 KiB）。超出结果上限返回 `AGENT_RESULT_TOO_LARGE`。按需读取相关区块即可。

断开客户端或停止轮询不会取消健康的任务。交互超时后变为 stale，任务以 `AGENT_INTERACTION_TIMED_OUT` 中断。取消（含 approval 的 `cancel_task`）在 RPC 返回前持久化，Provider interrupt 为尽力执行，限时 10 秒。终态不代表 Provider 已退出或写入租约已释放。

workspace-write 任务、Repo Ops 与文件修改通过 writer lease 协调。异常退出后，持久化 recovery guard 可拒绝写入并返回 `WORKDIR_RECOVERY_REQUIRED`。只有 reconciliation 证明 Provider 已停止，才能清除 guard；保留的非终态记录会以 `AGENT_PROVIDER_INACTIVE` 中断。状态未知时失败关闭。不要删除 guard、重启无关 Provider 或盲目重跑中断任务。

## 出站代理与可选顾问

Provider 认证由原生 runtime 管理。Linux 命名 owner 使用私有 Bridge JSON（`proxy` 与 runtime 的 `use_proxy`）；旧单实例渲染器使用 `SERVERFS_AGENT_USE_PROXY`。Agent 代理端点必须无凭据；认证上游需经无凭据本地 broker 转接。Linux Codex 代理模式使用 Bridge 专属的 standalone app-server，不改动用户共享 daemon；Claude 和 Qoder 接收受控代理设置。v0.26.0 候选版还支持 Windows 命名 Bridge owner 通过受保护的无凭据 `--agent-proxy-env-file` 对 runtime/ACP 显式启用代理；直连 runtime 不继承环境中的代理变量。Tunnel 与 Jev 的出站独立配置。

可选 [Jev 顾问](../jev-advisors/) 在提交前、带显式任务上下文的原生模型发现阶段或真实 approval 请求出现时提供建议。建议始终为 `automatic=false`，不能改变 runtime、model、策略、writer lease 或 Provider 决策。未配置 Key 时，Agent 流程不依赖 Jev。

## 验证与诊断

先验证目标 owner、接入方式、workdir 授权和 runtime 发现，再在授权范围内执行小任务。命名实例按平台指南诊断；通用 `serverfs diagnostics snapshot --format json` 和 `serverfs diagnostics task <task-id> --format json` 依赖兼容的本地端点，不能作为签名命名接入的验收证据。

诊断通过私有 RPC 提供有界投影，不新增公开工具，也不是持久化分布式追踪系统。任务诊断可能返回 `available`、`unknown_task`、`not_retained` 或 CLI 的 `bridge_unavailable`。重启不会重建丢失的内存历史。勿对外暴露私有日志或凭据。

委派任务只需写清目标、精确范围、允许变更、停止条件和所需证据；按真实能力边界拆分。ServerFS 原样转发 prompt，不得通过伪装或碎片化指令绕过 Provider 安全检查，也不得削弱授权及审计。
