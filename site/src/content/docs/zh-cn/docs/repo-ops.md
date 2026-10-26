---
title: Deterministic Repo Ops
description: v0.23 Linux 开发能力：无需 Agent 委托即可执行有界 Git 检查、完整性固定的仓库动作、显式文件提交与当前 upstream 推送。
---

Deterministic Repo Ops 是 v0.23 开发源码新增的可选 **非 Agent 仓库能力**。它让 MCP 调用方可以完成机械性的仓库收尾步骤，而不获得 Shell，也不需要把这些步骤委托给服务器上的 AI Agent。

当前稳定版仍是 **v0.22.1**。在公共 MCP 端到端验收和正式 Release 完成之前，Repo Ops 仍属于仅 Linux 的 v0.23 开发能力。

## 产品边界

Repo Ops 复用已认证的 Host Bridge transport，是因为该进程已经掌握宿主机 Workdir 路径、当前用户的 Git/网络凭据上下文，以及共享 writer lease。它**不会**复用 Agent 执行路径。

Repo Ops 永远不会：

- 选择模型或 Runtime；
- 创建 Agent task 或 Provider session；
- 调用 Provider adapter；
- 暴露 Shell 或通用命令执行器；
- 接收调用方可控的 executable、argv、environment、cwd、remote、ref、force 或 Shell syntax；
- 自动回退到 `submit_agent_task`。

一个 Workdir 可以在 Agent mode 保持 disabled 的同时单独启用 Repo Ops。

## 公共 MCP 工具

只要至少一个 Workdir 显式启用 Repo Ops，就会额外注册 4 个工具：

| 工具 | 用途 | 是否修改 |
| --- | --- | --- |
| `inspect_repo` | 查看有界 Git status/diff 证据，或列出已配置的 named actions | 否 |
| `run_repo_action` | 执行一个管理员定义的固定仓库动作 | 作为 writer 操作串行化 |
| `commit_repo_changes` | 仅提交调用方显式指定的路径，并走固定的安全 Git 路径 | 是 |
| `push_repo` | 将当前分支推送到其已配置 upstream，并使用唯一精确 refspec | 是 |

`inspect_repo` 支持 `status`、`diff`、`staged_diff` 和 `actions`。Diff 视图必须提供显式的 Workdir 相对路径，并先通过 ServerFS 的路径/deny 策略。

`commit_repo_changes` 会拒绝预先存在的 staged changes，只 stage 指定路径，拒绝外部 Git content filter，并在这条确定性路径上关闭 hooks 和 commit signing，最后返回 commit SHA。它不会自动 push。

`push_repo` 会在 Host Bridge 内部解析当前 branch/upstream，并精确推送 `HEAD:<upstream-branch>` 到解析出的 remote。调用方不能选择 remote/ref，也不能请求 force。Git 以非交互方式运行，Provider/API secret 不会被转发到子进程环境。

## 按 Workdir 配置

Repo Ops 默认关闭，在 Linux/Compose 中按 Workdir 独立开启：

```text
WORKDIR_01_ALIAS=project
WORKDIR_01_PATH=/srv/project
WORKDIR_01_READ_ONLY=false
WORKDIR_01_REPO_OPS_ENABLED=true
WORKDIR_01_REPO_ACTIONS_JSON={...}
```

必须启用已认证的 Host Bridge transport，但不需要启用任何 Agent Runtime。`WORKDIR_XX_REPO_ACTIONS_JSON` 属于宿主机私有策略：它只会被渲染到 Bridge 配置，不会转发进入 MCP 容器。

Repo Ops named action 要求 Workdir 为 read-write。Git inspection 可以不依赖 named action；commit/push 和所有 named action 都会参与共享的独占 writer lease。

Workdir 策略模型见[配置](../configuration/)；完整 Linux 环境变量以版本化 [`.env.example`](https://github.com/NTLx/ServerFS_MCP/blob/main/.env.example) 为准。

## Named actions

`run_repo_action` 是一个通用的**策略机制**，不是项目专用命令 API。每个 Workdir 都可以配置自己的管理员定义 action profile。调用方只能选择已配置的 action 名称，并且仅当 profile 允许时提供一个 Workdir 相对路径。

一个 action profile 包含：

- `argv`：管理员拥有的固定、非空 argv；
- `mutates`：描述/审计元数据；
- `argument`：`none`、`path` 或 `basename`；
- `integrity`：Workdir 相对文件 → 小写 SHA-256 digest 的非空映射。

示意结构：

```json
{
  "quality_gate": {
    "argv": ["uv", "run", "--frozen", "--no-sync", "python", "-I", "tools/check_repo.py"],
    "mutates": false,
    "argument": "none",
    "integrity": {
      "tools/check_repo.py": "<reviewed-lowercase-sha256>",
      "pyproject.toml": "<reviewed-lowercase-sha256>",
      "uv.lock": "<reviewed-lowercase-sha256>"
    }
  }
}
```

部署前必须把每个占位符替换为真实、经过审核的 digest；renderer 会拒绝 malformed action profile。

Integrity pinning 是强制要求。原因是：即使 argv 固定，如果它执行的是可被调用方修改的仓库代码，也会退化成间接任意代码执行能力。Bridge 会在创建进程之前独立验证每个 pinned input 是否受 Workdir 约束、是否为普通文件、是否满足大小上限，并重新计算 digest。任何不匹配都会 fail closed。

不要在修改 pinned repository code 的同一个任务里自动更新 integrity pin。Pin 更新应作为独立的运维审核决定。

## Writer lease 与进程生命周期

Repo Ops commit、push，以及**所有 named action**，都会获取与 ServerFS 文件修改和 Agent workspace-write task 相同的独占 writer lease。即使 `mutates=false`，named action 仍会获取 lease；该标志无法证明操作绝对无副作用，也不能允许 integrity verification 与实际执行之间出现 TOCTOU 窗口。

Linux 子进程执行同时受时间和输出大小限制。发生 timeout、output overflow 或 cancellation 时，会终止整个 Repo Ops subprocess group，并在释放 writer lease 之前完整收敛进程与 pipe 生命周期。

## 安全不变量

Repo Ops 延续 ServerFS 现有信任模型：

- 所有调用方路径先通过 MCP 侧 path/deny policy，再发送 Bridge RPC；
- Bridge 在宿主机侧再次独立约束路径；
- hidden/credential-denied 路径不会出现在 status 中，也不能被请求用于 diff/commit；
- 子进程环境使用 allowlist，不会整体继承 Bridge/Provider secrets；
- Hardened deterministic Git 路径会关闭 hooks、askpass override、repository SSH-command override、external content filter 与 commit signing；
- Git transport 仅允许批准协议，自定义 remote helper 不能由调用方选择；
- 错误与结果不会泄露真实宿主机 Workdir 绝对路径；
- Repo Ops 代码不会 import 或调用 Agent provider adapter。

相邻信任边界见[安全模型](../security/)与[架构](../architecture/)。

## v0.23 开发源码中的工具数量

稳定版 v0.22.1 仍保持文件系统、二进制与 Agent 组合下的 11 / 13 / 21 / 23 个工具。v0.23 开发源码独立增加 4 个 Repo Ops 工具：

| 能力面 | 工具数 |
| --- | ---: |
| 文件系统 | 11 |
| 文件系统 + 二进制 | 13 |
| 文件系统 + Repo Ops | 15 |
| 文件系统 + 二进制 + Repo Ops | 17 |
| 文件系统 + Agent | 21 |
| 文件系统 + 二进制 + Agent | 23 |
| 文件系统 + Agent + Repo Ops | 25 |
| 文件系统 + 二进制 + Agent + Repo Ops | 27 |

## 当前 v0.23 验收状态

截至 2026-10-26，Linux bootstrap / 内部 E2E 已 **CLOSED-PASS**，公共 ChatGPT MCP E2E 也已 **PASS**：

- 根项目测试：1780 passed，320 skipped；只有 1 个上游 Starlette deprecation warning；
- Agent Bridge：497 passed，15 skipped，0 warning；
- Ruff 与 format：PASS；
- documentation freeze 前的文档站构建：PASS（实际 bootstrap 中构建了 37 页）；freeze 后的站点重建仍待执行；
- scratch MCP image 构建：PASS；
- 用户级 Host Bridge verification：PASS；
- 内部 MCP Repo Ops surface verification：PASS；
- Host Repo Ops action-set verification：PASS；
- 公共 ChatGPT MCP E2E：PASS —— 完成 `actions → status → wiki_lint → explicit diff → explicit-file commit → current-upstream push`，全程未使用 Agent task；提交 `b3621bc5342e834990c170720de149d846d7de34` 已推送到 `LLM_WiKi` 的 `origin/main`，最终 worktree clean。

已执行证据记录在 [`docs/v0.23.0-linux-repo-ops-bootstrap-acceptance-2026-10-26.md`](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.23.0-linux-repo-ops-bootstrap-acceptance-2026-10-26.md)。

后续运维 Skills 审计又修改了 `skills/**` 与 `tests/test_agent_skills.py`，因此 bootstrap 时的 root suite 结果不再代表当前最终源码。剩余 Release gate 包括刷新 root/Agent-Skills 验证、documentation freeze 后重新执行站点 build/diff check，以及正常的版本化与发布流程。在这些验收完成并正式发布 v0.23 之前，本页描述的仍是开发能力，而不是稳定版保证。
