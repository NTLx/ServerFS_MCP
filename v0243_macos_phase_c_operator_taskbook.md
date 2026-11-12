# ServerFS v0.24.3 · macOS Phase C 最小现场任务书

**状态：授权范围仅限现场核查与一次独立本机验证。** 本任务书用于交给 Mac 电脑上由用户直接操作的 AI Agent。执行完成后由 ChatGPT 读取报告、决定是否接手 Cloudflare 生产切换。**本任务不授权任何生产切换。**

## 0. 工作位置与交付

- 仓库：`/Users/lx/Projects/ServerFS_MCP`
- 必读：`AGENTS.md`、`docs/maintainers/invariants.md`、`docs/maintainers/workflow.md`、`dev_plan_v0.24.3.md`（只读）
- **唯一持久产出**：仓库根目录 `v0243_macos_phase_c_operator_report.md`；不存在时创建，存在时仅覆盖本次同名任务报告，不覆盖其他任何文件。
- 报告格式：UTF-8 Markdown；仅写脱敏结果，不写密钥、Bearer Token、原始环境文件、HTTP Authorization 头或生产私有日志。不得提交/推送。

## 1. 任务背景（用来界定目标，不作为当前状态结论）

v0.24.3 的 macOS Cloudflare 安装器源码已移除强制 `read_only=true` 的 staging 限制；全量 pytest 记录为 1766 passed / 408 skipped，静态检查、Skills 和站点构建已通过。**但生产 Cloudflare 仍使用原有单工作目录只读 TOML，尚未完成公网写入/Agent 验收。**

此前两次 Phase C 尝试分别出现：（a）独立临时 HTTP Origin 无法建立 localhost 连接，临时子进程日志未保留以供诊断；（b）受控切换预检看到 Bridge `active_task_count=1`（可能包含检查任务自身），以及已经消失的历史非受管 PID；切换在执行前已停止。当前不能把这些迹象直接判为生产故障，也不能将它们忽略为已验证通过。

## 2. 仅允许的必要操作

### A. 只读核实当前运行状态

1. 检查 Git 分支、HEAD、未提交文件，确认在预期仓库。不要执行 `git fetch/pull/checkout/reset/stash/commit/push`。记录当前 `.venv` 实际导入的 ServerFS 模块路径及安装包版本（不得 `uv sync`、`pip install`）。
2. 使用**现有受支持的只读接口**检查 `serverfs service status`、`serverfs service cloudflare status`、Agent Bridge 状态和 macOS 受管进程。只记录 OpenAI Tunnel、Agent Bridge、Cloudflare Connector、HTTP Origin 的状态/PID，以及它们是否仍为受管进程；不得执行 restart/stop/uninstall/install。
3. 对 `unmanaged_tunnel_pids` 只核对**当前仍存活**的条目；如非空，只做定向的 PID/可执行文件身份与所有权核对，不扫描全系统，也不终止进程。历史已退出 PID 仅记作历史，不作为当前故障结论。
4. 如果可以通过官方/本项目**已有只读状态接口**确认 Bridge 的活动任务数和本次检查任务身份，请区分“本任务自身”与“其他任务”。`active_task_count=1` 本身**不能证明存在其他冲突任务**；如无法确定身份，报告“无法判定”，不要改动任务、锁或 Bridge。
5. 只确认源配置路径、工作目录别名/权限与私有 token/env 文件的**存在、属主、权限、是否位于 workdir 之外**。不得读取或输出生产 Token、API Key、`.env` 值或 `bridge.json` 私有内容。

### B. 单次隔离本机 HTTP Origin 验收（前置条件满足才执行）

前置条件：可以在普通当前用户权限下创建仓库和所有暴露 workdir **之外**的 0700 临时目录，并且可安全管理自己启动的临时子进程；不要求停止任何既有服务。

1. 创建**一个**临时测试目录和其子目录，编写仅包含以下策略的临时 `serverfs.toml`：**一个**指向临时目录的 `acceptance-v0243` workdir，明确 `read_only=false`，Agent 禁用，Repo Ops 禁用，二进制传输禁用；不得引用 `serverfs` 或 `codexsatellites` 真实目录。
2. 为该次测试在**进程内**生成独立随机 HTTP Bearer Key；配置只绑定 `127.0.0.1` 上一处可用端口，设置确切的测试 Host allowlist。不要使用生产 Token/Key，不要把临时 Key 写入任务输出、报告、命令行或持久文件。
3. 使用当前已有的项目虚拟环境，在本机启动**一个**受自身进程句柄控制的临时 `serverfs serve --transport streamable-http`。捕获该子进程的有限、脱敏退出状态与错误摘要，特别是监听失败时究竟属于权限、端口、启动校验、配置解析还是 HTTP Host/认证问题。**不使用 sudo、不关闭沙箱、不修改系统设置或服务器实现。**
4. 只有 HTTP 服务真正可达时才进行本机 MCP 协议验收：未认证/错误认证拒绝；正确认证的 initialize、tools/list、list_workdirs；允许在 `acceptance-v0243` 内创建、读取、带 revision 编辑、再读取、删除一次性文件；错误 revision、遍历/拒绝路径应被拒绝；Agent 和 Repo Ops 工具应不存在。只记录状态、通过/失败、简短脱敏错误信息，不能输出任何测试密钥。
5. **只允许一次可控测试启动。** 若首次测试失败，可基于实际捕获的错误定位原因，但不得通过重复盲试、修改产品代码或改用生产服务来继续。所有子进程都须在 `finally` 路径内退出并确认没有残留，然后删除本次创建的临时目录。
6. 完成后再次读取四项生产服务的状态/PID，并确认本次未更改任何生产配置、服务与 Git 已有文件；原有工作树未提交修改应原封保留。

## 3. 明确禁止

- **禁止操作**现有 OpenAI Tunnel、Agent Bridge、Cloudflare Connector、HTTP Origin 的 install/uninstall/start/stop/restart、launchctl、Login Items，以及任何生产服务重注册；也禁止操作 Cloudflare Dashboard/DNS/WAF。
- **禁止**直接连接公网 Cloudflare 进行写入、运行生产 Agent 任务、变更生产 workdir 权限、修改 `serverfs.toml` 生产配置/私有凭据、修改源码/测试/Skills/文档（除了下述报告）。
- **禁止**清理不属于本任务创建的目录、进程或历史状态；禁止 root/sudo、权限提权、关沙箱、绕过本项目服务状态/授权检查、调用其他 Agent 代替本任务。
- 如果任何关键安全前提不成立，**立即停止相应操作**，仍需写报告，明确“未执行/未验证”，不能以推测充当 PASS。

## 4. 报告模板（必须填充）

输出路径：`/Users/lx/Projects/ServerFS_MCP/v0243_macos_phase_c_operator_report.md`

```markdown
# v0.24.3 macOS Phase C Operator Report
- 执行时间与 macOS/架构：
- 仓库 HEAD / 工作树摘要：
- 实际 ServerFS 包版本与模块路径：
- 结果：PASS / PARTIAL / BLOCKED（不得用 PARTIAL 冒充 PASS）

## 生产状态（仅非敏感元数据）
| 服务 | 启用/运行 | PID（前/后） | 受管归属已确认？ |
| --- | --- | --- | --- |
| OpenAI Tunnel | | | |
| Agent Bridge | | | |
| Cloudflare Connector | | | |
| HTTP Origin | | | |

- 原 OpenAI 与 Cloudflare 配置路径及有效 workdir 摘要（不含秘密）：
- 当前 unmanaged_tunnel_pids 与存活身份核对：
- Bridge 活动任务数、能否识别为本任务、是否存在其他活动任务：
- 生产 Token/env 文件权限与隔离校验（仅布尔结论）：

## 单次隔离本机 HTTP E2E
| 检查 | PASS / FAIL / NOT VERIFIED | 非敏感证据 |
| --- | --- | --- |
| 临时私有目录/配置 | | |
| Loopback 子进程启动与监听 | | |
| 匿名/错误认证拒绝 | | |
| MCP initialize、tools/list、list_workdirs | | |
| 读写/修订保护/删除 | | |
| Agent/Repo Ops 仍禁用 | | |
| 拒绝遍历和未授权路径 | | |
| 临时进程与目录已清理 | | |
| 生产四服务前后不变 | | |

## 未通过事项与原因
- 精确失败阶段、子进程退出码与经脱敏的错误摘要：
- 确认的事实 vs 推断/不确定性：
- 是否存在遗留临时资源：

## 交接结论
- 下一步可由 ChatGPT 安全开展的操作：
- 仍然需要人工处理的阻塞：
- 明确未执行：生产服务操作、公开 Cloudflare 写入、Git 提交/推送、版本发布。
```

## 5. 完成定义

**只在**已经写入上述 Markdown 报告、确认本次临时资源已清理、生产四项服务未被本任务操作之后，向用户回复“已完成/受阻”和**报告准确路径**。不要自动推进 Cloudflare 生产切换、代码修改或 v0.24.3 发布；这些后续工作由 ChatGPT 读取报告后独立决定。
