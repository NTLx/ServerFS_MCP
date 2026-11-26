---
title: Deterministic Repo Ops
description: 通过显式授权的宿主机 Bridge 执行确定性仓库操作，无需 Agent 委托。
---

Deterministic Repo Ops 是可选的非 Agent 仓库能力，仅通过显式授权的宿主机 Bridge 提供有界仓库操作，不提供 Shell，也不回退到 Agent。

Repo Ops 按 workdir 启用，并须通过所选 Bridge 接入方式的授权。命名共享 Core grant 相互独立；macOS Owned v1 使用私有原生授权，显式配置的 Owned v2 使用逐别名 grant。macOS 命名共享 Core 不支持 Repo Ops。

## 产品边界

宿主机 Bridge 管理仓库路径、Git 凭据和 writer lease。Repo Ops 不选择模型、不创建 Provider 任务、不回退到 `submit_agent_task`；不接受 Shell 命令或调用方指定的 executable、argv、environment、cwd、remote、ref、force。Agent mode 可以保持 disabled。

## 公共 MCP 工具

只要至少一个 Workdir 显式启用 Repo Ops，就会额外注册 4 个工具：

| 工具 | 用途 | 是否修改 |
| --- | --- | --- |
| `inspect_repo` | 查看有界 Git status/diff 证据，或列出已配置的 named actions | 否 |
| `run_repo_action` | 执行一个管理员定义的固定仓库动作 | 作为 writer 操作串行化 |
| `commit_repo_changes` | 仅提交调用方显式指定的路径，并走固定的安全 Git 路径 | 是 |
| `push_repo` | 将当前分支推送到其已配置 upstream，并使用唯一精确 refspec | 是 |

先调用 `inspect_repo(workdir="project", view="status")` 和 `inspect_repo(workdir="project", view="actions")`（替换为实际授权别名），这两个视图不接收路径；`diff` 和 `staged_diff` 必须指定 1–64 个互不重复、符合策略的相对路径。没有配置 action 时，不得虚构动作或回退到 Agent 执行。

`commit_repo_changes` 仅接受 1–64 个互不重复的显式文件路径（不接受目录）和单行提交消息。拒绝已有暂存变更及外部 Git content filter，关闭 hooks 与 commit signing，返回 commit SHA。提交后检查状态；不会自动 push。

`push_repo` 会在 Host Bridge 内部解析当前 branch/upstream，并精确推送 `HEAD:<upstream-branch>` 到解析出的 remote。调用方不能选择 remote/ref，也不能请求 force。Git 以非交互方式运行，Provider/API secret 不会被转发到子进程环境。

## 按 Workdir 配置

Repo Ops 默认关闭。Linux 命名部署需在 Core slot 和对应的 owner 私有 JSON `workdirs` 条目（`repo_ops_enabled: true`）中同时启用，签名 grant 允许所需 `repo.*` 方法。详见 [Linux 指南](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/linux/README.md)。v0.26.0 签名 overlay 支持 1–16 个显式授权槽位，包括 Agent 禁用的 Repo Ops-only 槽位。可写仓库的 Core 侧设置如下：

```text
WORKDIR_01_ALIAS=project
WORKDIR_01_PATH=/srv/project
WORKDIR_01_READ_ONLY=false
WORKDIR_01_REPO_OPS_ENABLED=true
```

Named action 定义在授权 owner workdir 的私有 JSON `repo_actions` 中。只有旧 Linux 单实例渲染器才使用 `WORKDIR_XX_REPO_ACTIONS_JSON`，且不会将策略转发到 Core 容器。原生 TOML 使用 `repo_ops_enabled = true` 和对应 workdir 的 `repo_actions` 表，按 [macOS](../macos-native/) 或 [Windows](../windows-native/) 指南配置对应 owner 策略及接入。

授权只读仓库可以执行 inspection，不需要 named action。Commit、push 及所有 named action 均需要可写策略和 writer lease。不需要 Agent Provider。

Workdir 策略模型见[配置](../configuration/)；完整 Linux 环境变量以版本化 [`.env.example`](https://github.com/NTLx/ServerFS_MCP/blob/main/.env.example) 为准。

## Named actions

`run_repo_action` 是一个通用的**策略机制**，不是项目专用命令 API。每个 Workdir 都可以配置自己的管理员定义 action profile。调用方只能选择已配置的 action 名称，并且仅当 profile 允许时提供一个 Workdir 相对路径。

命令动作（`kind: "command"`，默认值）包含：

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

将每个占位符替换为已审核的 64 位小写 SHA-256 摘要。策略允许 1–32 个 argv 项（每项最多 4096 UTF-8 字节）、1–32 个完整性 pin。此例仅展示策略结构，不是可直接执行的动作。

Integrity pinning 是强制要求。原因是：即使 argv 固定，如果它执行的是可被调用方修改的仓库代码，也会退化成间接任意代码执行能力。Bridge 会在创建进程之前独立验证每个 pinned input 是否受 Workdir 约束、是否为普通文件、是否满足大小上限，并重新计算 digest。任何不匹配都会 fail closed。

不要在修改 pinned repository code 的同一个任务里自动更新 integrity pin。Pin 更新应作为独立的运维审核决定。

## Release tag 与固定 npm workflow 触发

对于 npm/`package.json` 发布型 workdir，运维者可在上述私有 owner 策略中配置两个内置动作类型，沿用原有工具、完整性校验及 writer lease。仓库由运维者固定；调用方仅提交动作名与 `release_tag`，不能指定 argv。以下 Linux 示例是专用 npm 发布契约，不是通用发布接口或其他平台的验收证明。

```json
{
  "tag_release": {
    "kind": "release_tag",
    "repository": "example/project",
    "argument": "release_tag",
    "mutates": true,
    "integrity": {
      "package.json": "<reviewed-lowercase-sha256>",
      ".github/workflows/publish.yml": "<reviewed-lowercase-sha256>"
    }
  },
  "publish_npm": {
    "kind": "publish_npm",
    "repository": "example/project",
    "argument": "release_tag",
    "mutates": true,
    "integrity": {
      "package.json": "<reviewed-lowercase-sha256>",
      ".github/workflows/publish.yml": "<reviewed-lowercase-sha256>"
    }
  }
}
```

审查输入后替换仓库和摘要占位符。这两个 pin 必填，可按需增加其他已审查的发布输入。被 pin 文件变更后，必须单独由操作者审查并更新摘要，不能自动刷新。此配置不会修改目标仓库或 workflow。

先用 `inspect_repo(workdir="project", view="actions")` 发现能力；仅在创建 tag 和触发 workflow 分别获得授权时，调用已配置动作：

```text
run_repo_action(workdir="project", action="tag_release", release_tag="v1.1.0-beta.3")
run_repo_action(workdir="project", action="publish_npm", release_tag="v1.1.0-beta.3")
```

Tag 必须完整匹配 `v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-beta\.(0|[1-9][0-9]*))?`，最长 128 字符。前导零、路径、分支名、空白、shell 语法和其他预发布渠道均被拒绝。`path` 与 `release_tag` 互斥；公共动作调用中的未知字段（包括 `force`、`commit`、`remote`、`ref`、`workflow`）直接报错，不会被忽略。

两种动作都要求可写 workdir、受约束的 Git 根目录及元数据、干净工作树和暂存区、`main` 跟踪 `origin/main`、HEAD 中根 `package.json` 的版本与 tag 一致，且实际远程 `main` 等于 HEAD。fetch/push URL 各自必须只有一个，且是配置仓库的标准 GitHub HTTPS/SSH 地址。禁用 Git hooks、SSH-command/askpass 覆盖、mirror/follow-tags/submodule push 和自定义传输 helper；子进程沿用有界执行及最小环境。

`tag_release` 使用 compare-and-create 创建轻量 tag，仅指向捕获的当前 HEAD，且只把该对象 push 到精确的 `refs/tags/<release_tag>`，随后核实远程结果。已有本地/远程 tag 会剥离到 commit：指向相同 HEAD 则幂等成功，指向其他 commit 则返回 `REPO_TAG_CONFLICT`，绝不覆盖。push 失败可能留下新建的本地 tag，处理错误后可重试同一动作；不会修改分支或 force ref。

`publish_npm` 要求远程 tag 已存在且指向当前 HEAD。管理员可通过可选 `github_cli` 绝对路径选择已安装的 GitHub CLI（默认 `gh`），解决 Bridge PATH 未包含它的情况；调用方不能传入这个字段，也不会改变其他 provider 的 PATH 或认证。使用当前用户已有的 GitHub CLI 认证，只触发固定仓库的 `publish.yml`，仅传入已校验 tag 作为 ref，不接受 workflow inputs。返回值表示 dispatch 受理，不代表 npm 发布成功。**Dispatch 不幂等**：网络结果不确定时，重试前先查询 GitHub runs。创建 tag 不会自动触发 dispatch，避免重复发布请求。现有 workflow 自身的 tag allowlist 继续有效；这里接受合法 semver 不代表 workflow 允许发布该版本。

此配置唯一正式 npm 发布路径仍是 Git tag → 现有 GitHub Actions → Trusted Publisher/OIDC → npm。ServerFS 不调用 npm，也不新增 PAT/npm token。任意 Git/workflow 命令可能扩大到分支重写、其他 refs 或凭据滥用，因此不属于此接口。ChatGPT 正常发布应优先使用确定性 Repo Ops，不再为这些步骤提交服务器 AI Agent 任务。

授权升级或策略变更后，按平台生命周期及 grant 配置流程操作。命名 owner provision 不是在线策略更新机制。验证目标 owner、Core、动作发现及当前 `release_tag` schema；客户端 schema 过期时刷新。不得重建无关组件或替换成 Agent 任务。

## Writer lease 与进程生命周期

Repo Ops commit、push，以及**所有 named action**，都会获取与 ServerFS 文件修改和 Agent workspace-write task 相同的独占 writer lease。即使 `mutates=false`，named action 仍会获取 lease；该标志无法证明操作绝对无副作用，也不能允许 integrity verification 与实际执行之间出现 TOCTOU 窗口。

Repo Ops 子进程在所有支持平台上都受时间和输出大小限制。POSIX 主机在 timeout、output overflow 或 cancellation 时终止整个 subprocess group，并在释放 writer lease 前收敛进程与 pipe。Windows 会先以 suspended 状态创建子进程，成功加入 Bridge 自有 Job Object 后才 resume；异常时必须终止并收敛完整 descendant tree 才能释放 writer lease。无法证明 cleanup 时，操作失败关闭，保留该 Bridge generation 的 lease。先核实实际进程状态，再按平台恢复流程操作，不得仅为释放 lease 而盲目重启。

## 安全不变量

Repo Ops 延续 ServerFS 现有信任模型：

- 所有调用方路径先通过 MCP 侧 path/deny policy，再发送 Bridge RPC；
- Bridge 在宿主机侧再次独立约束路径；
- 原生 Windows 会在仓库执行前拒绝 NTFS reparse point / junction，与原生文件系统边界一致，而不是把 Windows 路径当作 POSIX symlink 处理；
- hidden/credential-denied 路径不会出现在 status 中，也不能被请求用于 diff/commit；
- 子进程环境使用 allowlist，不会整体继承 Bridge/Provider secrets；
- Hardened deterministic Git 路径会关闭 hooks、askpass override、repository SSH-command override、external content filter 与 commit signing；Windows 在 Git-for-Windows 需要 hooks sink 时使用原生 `NUL`；
- Git transport 仅允许批准协议，自定义 remote helper 不能由调用方选择；
- 错误与结果不会泄露真实宿主机 Workdir 绝对路径；
- Repo Ops 代码不会 import 或调用 Agent provider adapter。

相邻信任边界见[安全模型](../security/)与[架构](../architecture/)。

## 工具数量

Repo Ops 独立增加 4 个工具；组合如下：

| 能力面 | 工具数 |
| --- | ---: |
| 文件系统 | 11 |
| 文件系统 + 二进制 | 13 |
| 文件系统 + Repo Ops | 15 |
| 文件系统 + 二进制 + Repo Ops | 17 |
| 文件系统 + Agent | 22 |
| 文件系统 + 二进制 + Agent | 24 |
| 文件系统 + Agent + Repo Ops | 26 |
| 文件系统 + 二进制 + Agent + Repo Ops | 28 |

11 个 Agent 工具包含 `list_agent_tasks`，有效调用需要支持该方法的签名 v2 接入。

实际验收以目标版本发布清单和所选平台独立组件的验证记录为准。
