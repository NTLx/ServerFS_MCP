---
title: 架构
description: ServerFS 的独立组件、实例拓扑、原生平台边界与生命周期。
---

## 组件模型

ServerFS 通过 MCP 为客户端提供对显式配置的本地目录（Workdir）的受控访问。持久 HTTP 部署由 MCP Core、零个或多个入口 Connector，以及可选宿主机 Agent Bridge 组成。本地 stdio 是独立传输选择，不注册持久 HTTP 实例。

```text
MCP 客户端 ── 直接 HTTP ──────────────┐
           ├─ OpenAI Connector ──────┤
           └─ Cloudflare Connector ──┤
                                     ▼
                                HTTP MCP Core ── 受控 Workdir
                                     │
                                认证的本地 IPC
                                     │
                              可选 Agent Bridge ── 宿主机 Agent / Repo Ops
```

上图表示逻辑路径，不要求部署全部组件，也不意味着所有组合具有完全相同的平台命令接口。

| 组件 | 职责 | 所有权 |
| --- | --- | --- |
| MCP Core | MCP 会话、HTTP 调用方准入、Workdir 策略、文件访问与可选工具暴露 | 独立配置和运行时 |
| Connector | 通过外部 Tunnel 将 MCP 流量转发至指定 HTTP Core | 独立 Provider 凭据、身份和进程 |
| Agent Bridge | 在宿主机执行已授权的 Agent 任务与确定性 Repo Ops，协调 writer lease | 独立私有运行时、策略、IPC 与任务状态 |

直接 HTTP 是 Core 的暴露选项，不属于 Connector。本地或私有网络可达的 Core 不必使用 Tunnel。公网接入需要独立的调用方准入边界；Tunnel 凭据不是 MCP 调用方凭据。命名 OpenAI HTTP Connector 使用静态 Core bearer header，不支持将 OAuth-only Core 作为这一接入路径。

## 实例身份与 Bridge 模式

持久 Core 使用 `instance_id`；入口通过 Provider、目标 Core 和（在支持时）`connector_id` 选择。Bridge owner 拥有独立状态与 `bridge_id`。ID 是稳定标识，不是凭据，也不能证明授权。原生标识由 1–32 个 ASCII 小写字母、数字及内部连字符组成，首尾必须为字母或数字。兼容身份 `default` 保留既有资源，新命名部署不会隐式迁移它。

| Bridge 模式 | 含义 |
| --- | --- |
| `disabled` | 不引用 Bridge，仅提供文件能力，Agent 与 Repo Ops 均关闭；仍可独立启用二进制传输。 |
| `owned` | 部署拥有专属 Bridge；macOS/Windows 的 owner ID 与 Core ID 相同，Linux 使用独立 Bridge ID。 |
| `shared` | Core 引用同一宿主机上独立管理的 Bridge，只有 owner 管理 grant 与生命周期。 |

每个签名接入均需针对 Core 身份和允许的 Workdir/方法范围显式授权。同一 UID、SID、socket 或目录不构成授权。撤销或过期的 grant、重放、未授权任务及结果访问必须失败关闭。独立 Bridge owner 不得占用重叠可写根目录；同用户组件隔离并不是互不信任用户之间的安全沙箱。

## 部署拓扑

| 拓扑 | 适用场景 |
| --- | --- |
| A — 多 Connector → 单 Core → 单 Bridge | 多条入口访问同一 Core 策略与会话所有者。 |
| B — 多 Core → 共享 Bridge | Core 各自拥有策略与调用方准入，同时显式共享宿主机执行边界。 |
| C — 多套独立 Core/Bridge | 独立任务状态、运行时和生命周期；使用互不重叠的可写根目录。 |
| D — 一个或多个纯文件 Core | 无需 Agent Bridge、Agent runtime 或 Provider 凭据。 |

拓扑是产品模型。具体 provision 路径、限制及验收证据以平台指南为准；不能推断所有功能的任意组合都已支持。

## 平台实现

| 平台 | Core / Connector 生命周期 | Bridge 生命周期与 IPC |
| --- | --- | --- |
| Linux | 独立 Compose project；直接 HTTP 发布、内部网络或可选 OpenAI/Cloudflare 服务 | 可选宿主机用户 systemd unit、Unix socket、peer credentials 和签名 grant |
| macOS | 签名 App 关联 SMAppService / LaunchAgents；纯文件/owned 实例 staging 时预声明同 Provider Connector ID | owned 使用专属 Unix socket 与策略校验；shared 使用签名 v2 grant |
| Windows | 当前用户 Task Scheduler、独立组件身份与 Job 进程约束 | 独立 owner/worker 环境、Named Pipe、SID、受保护 ACL 与签名 grant |

当前实现限制：

- **Linux：** 每个 Compose project 中每种 Tunnel Provider 对应一个服务。v0.25.0 签名 overlay 在 slot 01 绑定一个启用 Agent/Repo Ops 的 Workdir；v0.26.0 候选版支持在 01–16 任意槽位配置 1–16 个独立授权的 Agent 和/或 Repo Ops Workdir，仍可配置互不重叠的纯文件目录。
- **macOS：** Shared Core 在 v0.25.0 绑定一个 Agent Workdir，在 v0.26.0 候选版绑定 1–16 个 owner 授权的 Agent Workdir；仍不支持 Repo Ops 或 Connector staging。纯文件与 owned 实例支持预声明的额外 Connector ID。
- **Windows：** 支持命名 Connector ID 和 owned/shared 签名接入。初始共享配置助手最多支持 16 个 Core；运行时和配置助手仅用于新建，不支持在线升级或凭据轮换。

具体步骤见[快速开始](../getting-started/)、[macOS](../macos-native/)和 [Windows](../windows-native/)。

统一的是公开 MCP 契约和安全结果，不要求各操作系统私有 IPC 编码、状态布局和服务实现完全一致。不引入中央服务管理器。

## 生命周期与故障边界

解耦意味着操作 Connector 不会隐式重启 Core、Bridge 或其他 Connector。先准备依赖并验证就绪，再显式启动所需组件。依赖组件仍活跃时，Core/Bridge 停止操作可能被拒绝；按平台规定的退役顺序执行，并先检查活跃任务和 writer lease。共享 Core 不得启停、升级、卸载或重新配置 Bridge owner。Bridge 故障不能扩大权限；文件能力可继续提供，而依赖 Bridge 的请求安全失败。入口重启不保证旧 SSE 会话重放。

## 可选能力

- **二进制传输：** 显式启用的有界整文件传输；可选文件入口采用独立隔离的 HTTPS 下载路径。见[二进制传输](../binary-transfer/)。
- **Repo Ops：** 四个有界仓库工具，固定 Git 语义与完整性固定的 named action。需要已授权 Bridge，但绝不启动 Agent task。见 [Repo Ops](../repo-ops/)。
- **Agent 委托：** 原生 Codex、Claude、Qoder 或显式配置的 ACP profile；Provider 原生认证与权限继续有效。见 [Agent Bridge](../agent-bridge/)与 [ACP](../acp/)。
- **Jev：** 可选建议能力，永远不获得执行或审批权。见 [Jev Advisors](../jev-advisors/)。

策略详见[配置](../configuration/)、[安全](../security/)与 [HTTP 认证](../api-key/)。发布级源码/构建门禁与真实原生、公网验收均单独记录在[发布清单](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.25.0-release-checklist-2026-11-21.md)中。
