---
title: ACP 指南
description: 在 ServerFS 中配置和运维通用 ACP v1 profile。
---

# ACP 指南

ServerFS 通过可选的宿主机 Agent Bridge 支持由运维人员配置的通用 Agent Client Protocol（ACP）v1 profile。ACP 是标准协议适配器，不是新的原生 Provider 集成。现有原生 runtime 仍为 Codex、Claude Code 和 Qoder；需要其原生集成能力时应使用对应 runtime。ACP profile 对外的 runtime ID 为 `acp-<id>`。

## 前置条件与认证

先按部署平台安装并验证 Agent Bridge，显式启用 Bridge，并确保 ACP 兼容程序安装在 Bridge 所属的同一用户环境中。ACP 不会从 Registry 安装 Agent。使用 Agent/Provider 自己支持的 CLI 或 UI 登录并管理凭据。Provider 状态、凭据和仅供 shell 使用的环境变量应留在 Provider 自有私有位置；不要写入 ServerFS profile 配置、workdir、任务事件或日志。

## 配置 profile

使用绝对可执行文件路径和固定参数。任务调用者不能改变 command、参数或环境变量。Linux 命名 owner 将以下条目加入私有 Bridge JSON 的 `acp_profiles` 数组，而非 Core 环境变量：

```json
{"id":"opencode","command":"/absolute/path/to/opencode","args":["acp"],"use_proxy":false}
```

仅旧 Linux 单实例渲染器使用 `SERVERFS_ACP_PROFILES_JSON`。原生 macOS 与 Windows 在对应 owner 策略的 `serverfs.toml` 中配置，并按平台指南完成 provision：

```toml
[[agent.acp_profiles]]
id = "opencode"
command = "/absolute/path/to/opencode"
args = ["acp"]
use_proxy = false
```

使用 Bridge 用户可访问的实际绝对路径及普通可执行文件。Unix 可通过 `command -v opencode` 定位；Windows 使用实际可执行文件路径。OpenCode 自行管理模型，不通过 ServerFS 覆盖。最多允许 16 个 profile；ID 唯一、1–28 字符，以小写字母开头，仅含小写字母、数字、下划线或连字符，且不可使用保留 runtime 名。固定参数最多 32 个，每个最多 4096 字符；编码后的 profile 策略不超过 65536 字节。初始化和关闭超时默认各 5 秒，允许大于 0 且不超过 60 秒。代理默认关闭，Agent 代理必须无凭据。

对 `project` workdir 启用写入并明确授权该 profile：

```toml
[[workdirs]]
alias = "project"
path = "/absolute/path/to/project"
read_only = false
agent_mode = "workspace-write"
agent_runtimes = ["acp-opencode"]
```

Linux 命名部署在 owner JSON 和所选授权 Core 槽位中配置匹配的策略（v0.25.0 为 slot 01，v0.26.0 候选版为 slot 01–16）。以下以 slot 01 为例（`WORKDIR_01_READ_ONLY=false`、`WORKDIR_01_AGENT_MODE=workspace-write`、`WORKDIR_01_AGENT_RUNTIMES=acp-opencode`），grant 必须允许所需 runtime/task 方法和别名。macOS/Windows 使用实际平台路径及对应原生接入策略。ACP 不会自行获得授权或扩大 workdir 策略。

## 兼容性证据

[v0.19.0 历史验收记录](https://github.com/NTLx/ServerFS_MCP/blob/main/dev_plan_v0.19.md) 记录了 OpenCode 2.1.2 与 `opencode/glm-5.3-flash-free` 在 Linux、macOS、Windows 的验收。所用 argv 为 `acp`，模型由 OpenCode 选择。这是历史证据，不代表 v0.25.0 签名接入验收，也不保证其他 Agent 版本兼容。

## 验证 Bridge 与 runtime

按 [Linux](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/linux/README.md)、[macOS](../macos-native/) 或 [Windows](../windows-native/) 命名部署指南验证 owner、接入与有效 workdir 策略。`list_agent_runtimes` 检查可执行文件是否可用并返回匹配 profile 的缓存能力，不会启动 Agent，也不能证明登录有效。成功的授权任务才完成协议协商并记录实测能力。在一次性授权 workdir 执行小任务，检查终态、事件及清理情况。旧单实例 verifier 不是命名接入验收证据。

## 任务操作语义

使用结构化 MCP Agent 工具：`list_agent_runtimes` 列出已配置的标准化 runtime 与能力；`submit_agent_task` 启动异步任务；`get_agent_task` 返回标准化任务状态/结果或待处理交互；`read_agent_task_events` 使用 cursor 读取标准化事件；`read_agent_task_result` 分块读取较大的 spool 结果。轮询与结果读取遵循 [Agent Bridge 任务流程](../agent-bridge/)。

在接入路径允许且 runtime 声明支持持久会话时，可通过 `continue_from_task_id` 续接同 runtime、同 workdir 的保留期内终态任务。适配器使用稳定 ACP `session/load`，调用方不直接传原始 session ID。但签名 v2 Core grant（包括 v0.26.0 候选版）（Linux 命名、macOS shared、启用 grant 的 Windows）拒绝 `continue_from_task_id` 与 `idempotency_key`，即使适配器声明可续接也不例外。在这些路径省略两字段，提交结果不确定时不可盲目重试。兼容 v1 路径可按策略及实测能力使用幂等提交和续接。Session 可恢复不代表先前进程已停止。`cancel_agent_task` 会先记录终态取消，再发出有界的尽力 Provider/进程停止请求。Bridge 会保留 writer lease/recovery guard，直到清理完成且确认停止；无法确认停止时保持 fail-closed。客户端断开或停止轮询不会取消任务。完成、取消或失败后，应先确认终态以及清理/lease 释放，再在同一 workdir 启动另一个写任务。

## 安全边界与能力限制

ServerFS 授权 workdir 和 runtime，为 workspace-write 任务持有共享 writer lease，并在运维配置中固定可执行文件、argv 和环境。ACP/Provider 的 sandbox 与 approval policy 仍由 Provider 管理；ServerFS writer lease 不是 sandbox。ACP 不提供 ServerFS 模型发现/覆盖、elicitation、ACP client filesystem 或 terminal 能力。也不支持实时 steering。交互 approval 与持久会话能力基于成功协议协商及匹配 profile 的缓存证据保守报告。不要通过 ACP task 暴露通用 shell、任意 argv 或环境透传，也不要自动安装或信任 Registry 条目。

## 常见错误与排障

- **Runtime 缺失/不可用：**检查 profile ID、绝对可执行文件路径、执行权限、Bridge 用户能否访问，以及目标 owner 的实际配置；变更需遵循平台支持的生命周期及 provision 流程。
- **ACP 启动/协议错误：**以 Bridge 用户身份检查配置的可执行文件、固定 argv 和 Agent 自身诊断；使用隔离测试环境，不在现有 workdir 另启 writer。
- **任务被拒绝：**检查有效 workdir 策略，确认 `read_only=false`、`agent_mode=workspace-write` 和准确的 `acp-<id>` allowlist。
- **认证/模型错误：**使用 Provider 自身工具登录或选择模型。ServerFS 无法发现或覆盖 ACP 模型。
- **取消后 workdir 仍 busy：**不要删除锁或重试写任务。检查 Bridge/Provider 进程状态并使用既定恢复流程；确认停止前 guard 会保留。
- **Linux 容器无法连接 Bridge：**按部署指南检查实测 peer UID/GID、socket mount、签名接入 overlay、grant 和宿主机 Bridge 健康状态。

生命周期与 IPC 配置见 [Agent Bridge](../agent-bridge/) 和各平台部署指南。
