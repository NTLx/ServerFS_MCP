---
title: ACP 指南
description: 在 ServerFS v0.19.0 中配置和运维通用 ACP v1 profile。
---

# ACP 指南

ServerFS v0.19.0 通过可选的宿主机 Agent Bridge 支持由运维人员配置的通用 Agent Client Protocol（ACP）v1 profile。ACP 是标准协议适配器，不是新的原生 Provider 集成。现有原生 runtime 仍为 Codex、Claude Code 和 Qoder；需要其原生集成能力时应使用对应 runtime。ACP profile 对外的 runtime ID 为 `acp-<id>`。

## 前置条件与认证

先按部署平台安装并验证 Agent Bridge，显式启用 Bridge，并确保 ACP 兼容程序安装在 Bridge 所属的同一用户环境中。ACP 不会从 Registry 安装 Agent。使用 Agent/Provider 自己支持的 CLI 或 UI 登录并管理凭据。Provider 状态、凭据和仅供 shell 使用的环境变量应留在 Provider 自有私有位置；不要写入 ServerFS profile 配置、workdir、任务事件或日志。

## 配置 profile

使用绝对可执行文件路径和固定参数。任务调用者不能改变 command、参数或环境变量。Linux/Docker 在根目录 `.env` 中设置 `SERVERFS_ACP_PROFILES_JSON`（通过显式 Agent Compose overlay 启用）：

```dotenv
SERVERFS_ACP_PROFILES_JSON=[{"id":"opencode","command":"/absolute/path/to/opencode","args":["acp"]}]
```

Windows 与 macOS 在原生部署使用的 `serverfs.toml` 中配置：

```toml
[[agent.acp_profiles]]
id = "opencode"
command = "/absolute/path/to/opencode"
args = ["acp"]
use_proxy = false
```

路径必须是该平台实际的绝对路径。Unix 系统可用 `command -v opencode` 定位，再将结果解析为绝对路径；Windows 应填写实际的绝对可执行文件路径。`opencode acp --help` 没有 model 参数，因此 argv 中不要传模型。OpenCode 自己管理所选模型。ServerFS ACP 不支持模型发现或请求级模型覆盖。

对 `project` workdir 启用写入并明确授权该 profile：

```toml
[[workdirs]]
alias = "project"
path = "/srv/projects/project"
read_only = false
agent_mode = "workspace-write"
agent_runtimes = ["acp-opencode"]
```

若 workdir 策略由环境变量提供，也必须具有相同的有效策略：`read_only=false`、`agent_mode=workspace-write`，并在 allowlist 中列出准确的 `acp-<id>` runtime。ACP 不会自行获得授权，也不会扩大 workdir 策略。

## OpenCode 2.1.2 示例与验收证据

验收 profile 使用 `args = ["acp"]`；模型 `opencode/glm-5.3-flash-free` 由 OpenCode 自行选择和管理，不传给 ServerFS。在 Linux 验收主机上，可执行文件为 `/home/lx/.opencode/bin/opencode`；这仅是该主机的记录示例。实际部署请使用 `command -v opencode` 或平台上的真实绝对路径。

OpenCode 2.1.2 与 `opencode/glm-5.3-flash-free` 已记录通过 Linux、macOS、Windows 的真实端到端验收，包括 prompt、持久 session continuation 和 active-tool cancellation/cleanup。该证据只适用于这组已测试的 OpenCode/版本/模型，不构成对所有 ACP Agent 或版本的兼容保证。

## 验证 Bridge 与 runtime

运行对应平台当前的 Bridge 验证流程（Linux 部署通常使用 `python3 deployment/agent-bridge/verify_host.py --require-runtimes`；Windows/macOS 使用原生部署验证以及 `serverfs doctor --config serverfs.toml`）。确认 Bridge 健康、有效 workdir 策略允许 `acp-opencode`，并且 runtime discovery/listing 显示配置的 runtime 可用。列举或探测不会为了发现能力而启动 Agent。随后在一次性且已授权的 workdir 中运行小任务，并检查终态结果和事件。

## 任务操作语义

使用结构化 MCP Agent 工具：`list_agent_runtimes` 列出已配置的标准化 runtime 与能力；`submit_agent_task` 启动异步任务；`get_agent_task` 返回标准化任务状态/结果或待处理交互；`read_agent_task_events` 使用 cursor 读取标准化事件；`read_agent_task_result` 分块读取较大的 spool 结果。对同一语义提交复用 idempotency key 会返回保留的原任务，不会另开一轮。

若 runtime 报告支持 continuation，应提交一个新任务并设置 `continue_from_task_id=<上一条已终态任务 ID>`。Bridge 会复用与该 ServerFS 任务关联并持久化的 ACP session；调用方不直接传原始 ACP session ID。Session 可恢复不代表先前进程已停止。`cancel_agent_task` 会先记录终态取消，再发出有界的尽力 Provider/进程停止请求。Bridge 会保留 writer lease/recovery guard，直到清理完成且确认停止；无法确认停止时保持 fail-closed。客户端断开或停止轮询不会取消任务。完成、取消或失败后，应先确认终态以及清理/lease 释放，再在同一 workdir 启动另一个写任务。

## 安全边界与能力限制

ServerFS 授权 workdir 和 runtime，为 workspace-write 任务持有共享 writer lease，并在运维配置中固定可执行文件、argv 和环境。ACP/Provider 的 sandbox 与 approval policy 仍由 Provider 管理；ServerFS writer lease 不是 sandbox。ACP 不提供 ServerFS 模型发现/覆盖、elicitation、ACP client filesystem 或 terminal 能力。其他能力也只按实测结果保守报告。不要通过 ACP task 暴露通用 shell、任意 argv 或环境透传，也不要自动安装或信任 Registry 条目。

## 常见错误与排障

- **Runtime 缺失/不可用：**检查 profile ID、绝对可执行文件路径、执行权限、Bridge 用户能否访问，以及 Bridge 配置是否已重新加载/启动。
- **ACP 启动/协议错误：**以 Bridge 用户身份用固定 `acp` 参数运行配置的程序；检查 Agent 自身诊断与 ACP 支持版本。
- **任务被拒绝：**检查有效 workdir 策略，确认 `read_only=false`、`agent_mode=workspace-write` 和准确的 `acp-<id>` allowlist。
- **认证/模型错误：**使用 Provider 自身工具登录或选择模型。ServerFS 无法发现或覆盖 ACP 模型。
- **取消后 workdir 仍 busy：**不要删除锁或重试写任务。检查 Bridge/Provider 进程状态并使用既定恢复流程；确认停止前 guard 会保留。
- **Linux 容器无法连接 Bridge：**按部署指南检查实测 peer UID/GID、socket mount、Agent overlay 和宿主机 Bridge 健康状态。

生命周期与 IPC 配置见 [Agent Bridge](./agent-bridge/) 和各平台部署指南。
