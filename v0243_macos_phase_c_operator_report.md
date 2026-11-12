# v0.24.3 macOS Phase C Operator Report

- 执行时间与 macOS/架构：2026-11-10 16:03–16:25（GMT+8）；macOS 27.2.1（Build 26B131），Apple Silicon arm64
- 仓库 HEAD / 工作树摘要：`5e952cb9f5b787670a0d11fecea2b69720f16083`，分支 `main`；任务开始时工作树 9 项未提交条目（7 项修改：`deployment/macos/README.md`、`site/src/content/docs/docs/macos-native.md`、`site/src/content/docs/zh-cn/docs/macos-native.md`、`skills/serverfs-tunnel/references/macos.md`、`src/serverfs_mcp/darwin_service.py`、`tests/test_darwin_cli.py`、`tests/test_darwin_cloudflare_service.py`；2 项未跟踪：`dev_plan_v0.24.3.md`、本任务书）。任务结束时上述条目原封未动，HEAD 未变，未执行任何 Git 写操作。本报告为任务书授权的唯一新增持久产出。
- 实际 ServerFS 包版本与模块路径：发行元数据 `serverfs-mcp 0.24.2`（importlib.metadata）；模块路径 `/Users/lx/Projects/ServerFS_MCP/src/serverfs_mcp/__init__.py`（工作树可编辑安装，运行的是当前 checkout 的源码，其中包含未提交的 v0.24.3 变更）。未执行 `uv sync` / `pip install`。
- 结果：**PARTIAL**（现场核查全部完成且 PASS；隔离本机 HTTP E2E 的产品行为经服务端审计证据全部符合预期，但两项客户端侧断言的原始结果因本地验收编排脚本的输出收集缺陷未保留，按“不以推测充当 PASS”记为未验证/部分）

## 生产状态（仅非敏感元数据）

| 服务 | 启用/运行 | PID（前/后） | 受管归属已确认？ |
| --- | --- | --- | --- |
| OpenAI Tunnel | enabled / running | 1779 / 1779 | 是（`cli tunnel --config <仓库>/serverfs.toml --env-file <数据目录>/tunnel-proxy.env`，属主 lx，service status processes 映射一致） |
| Agent Bridge | enabled / running | 1790 / 1790 | 是（`serverfs-agent-bridge --config <数据目录>/agent-bridge/bridge.json`，`legacy_bridge_plist=false`） |
| Cloudflare Connector | enabled / running | 1784 / 1784 | 是（`cloudflared tunnel --no-autoupdate run --token-file <数据目录>/credentials/cloudflare/macbook-serverfs.token`） |
| HTTP Origin | enabled / running | 1786 / 1786 | 是（`cli serve --transport streamable-http --config <数据目录>/macbook-serverfs-origin/serverfs.toml --http-port 8765`） |

补充观测（未触碰）：PID 2719 `serverfs_mcp.supervisor --config <仓库>/serverfs.toml` 及其子进程 PID 2731 `cli serve --config <仓库>/serverfs.toml`（stdio，08:27 启动）构成既有受管隧道侧进程链，不属于四项被核对服务，本任务未做任何操作。`service status` 中 `handover` 字段为 2026-11-09 10:00 的历史记录（`ok=false`，detail 为当时“tunnel is enabled but no managed process became active”），仅作历史记录，当前 tunnel 进程 1779 存活且受管，不作为当前故障结论。

- 原 OpenAI 与 Cloudflare 配置路径及有效 workdir 摘要（不含秘密）：
  - OpenAI Tunnel 配置：`/Users/lx/Projects/ServerFS_MCP/serverfs.toml`（属主 lx，0644；已按字段名脱敏读取，未输出任何 token/key 内容）。workdirs：`serverfs` → `/Users/lx/Projects/ServerFS_MCP`（read_only=false，agent_mode=workspace-write，runtimes=codex/claude/qoder）；`codexsatellites` → `/Users/lx/Projects/CodexSatellites`（同上）；`binary_transfer_enabled=true`（8 MiB 上限）；`[agent]` enabled（max_active_tasks=4，proxy source=env）。
  - Cloudflare HTTP Origin 配置：`/Users/lx/Library/Application Support/ServerFS/macbook-serverfs-origin/serverfs.toml`（0600）。单 workdir：`serverfs` → `/Users/lx/Projects/ServerFS_MCP`，**read_only=true，agent_mode=disabled** —— 与任务背景一致：生产 Cloudflare 仍是单工作目录只读 TOML，未做公网写入/Agent 放开。
  - 两个 workdir 目录均为 0755 属主 lx。
- 当前 unmanaged_tunnel_pids 与存活身份核对：任务前后均为空数组 `[]`，无存活条目需要做定向 PID/属主核对；历史已退出 PID 不适用（接口未返回任何条目）。
- Bridge 活动任务数、能否识别为本任务、是否存在其他活动任务：`serverfs diagnostics snapshot`（16:05 左右）`active_task_count=0`。本次核查任务不经过 Agent Bridge（全部为本地只读命令与隔离临时 serve，且临时配置 Agent 禁用），本任务自身不会计入 Bridge 任务；检查时刻无其他活动任务。此前 Phase C 观察到的 `active_task_count=1` 本次未复现；该历史现象既不能证明存在冲突任务，本次也不对其作结论。
- 生产 Token/env 文件权限与隔离校验（仅布尔结论）：**通过**。`tunnel-proxy.env`、`credentials/cloudflare/macbook-serverfs.token`、`agent-bridge/bridge.json` 均为属主 lx、0600，`credentials/`、`credentials/cloudflare/`、`agent-bridge/` 目录 0700，全部位于两个暴露 workdir 之外；本次未读取其内容，未输出任何生产 Token、API Key、`.env` 值或 `bridge.json` 私有字段。

## 单次隔离本机 HTTP E2E

一次受控启动，未做第二次启动尝试。临时树：`$TMPDIR/v0243-phase-c-*`（0700，位于仓库与两个生产 workdir 之外），其子目录 `config/` 内放临时 `serverfs.toml`；workdir 指向临时目录本身。策略：仅一个 `acceptance-v0243` workdir，`read_only=false`、`agent_mode=disabled`、`repo_ops_enabled=false`、`binary_transfer_enabled=false`，无 `[agent]` 段，未引用 `serverfs`/`codexsatellites` 真实目录。Bearer Key 为进程内 `secrets.token_urlsafe(32)` 生成，仅经子进程环境变量 `SERVERFS_HTTP_API_KEY` 传递（未写入命令行、持久文件或任何输出，报告不含该 Key）；`SERVERFS_HTTP_ALLOWED_HOSTS=127.0.0.1:<临时端口>`。启动前用项目真实加载器 `load_native_config` 预检通过（`bridge_required=false`）。子进程为自身 Popen 句柄控制的 `.venv/bin/serverfs serve --config <临时> --transport streamable-http --port <临时端口> --path /mcp`，最小化环境，未用 sudo。

| 检查 | PASS / FAIL / NOT VERIFIED | 非敏感证据 |
| --- | --- | --- |
| 临时私有目录/配置 | PASS | 临时目录 0700，位于仓库与两个生产 workdir 之外；单 workdir 策略如上；`load_native_config` 预检：alias=acceptance-v0243、read_only=false、agent_mode=disabled、repo_ops=false、bridge_required=false |
| Loopback 子进程启动与监听 | PASS | 启动事件 `native_serve_requested{workdirs:1, read_write_workdirs:1, agent_bridge_enabled:false, file_ingress_enabled:false}`；`Uvicorn running on http://127.0.0.1:49496`；全程仅此一次 serve 启动 |
| 匿名/错误认证拒绝 | NOT VERIFIED | 已执行匿名、错误 Bearer、正确 Bearer 三次原始 initialize 请求（正确 Bearer 与后续 SDK 会话使用同一进程内随机 Key，服务端审计证实该 Key 被接受），但三次的 HTTP 状态码由客户端脚本采集、因下述编排缺陷未保留。服务端 `StaticAPIKeyMiddleware` 是应用前置 401 门，拒绝请求不产生审计记录属预期行为；无独立运行时证据，不记 PASS |
| MCP initialize、tools/list、list_workdirs | PARTIAL | initialize 与 list_workdirs 已由服务端审计证实：StreamableHTTP 会话建立（session 创建/终止各一次），`tool_call list_workdirs success=true, returned=1`，后续调用均带 `workdir=acceptance-v0243`；tools/list 的客户端工具清单（含 Agent/Repo Ops 缺席断言）未保留 |
| 读写/修订保护/删除 | PASS | `create_text_file` success（20B）→ `read_text_file` success → `edit_text_file` success（edit_count=1，revision 更新为新值）→ `read_text_file` success（内容已变更）；错误 revision 的 `edit_text_file` 被拒：`error_code=REVISION_CONFLICT`；`delete_file` success（19B，revision 与编辑后一致）→ 复读 `PATH_NOT_FOUND` |
| Agent/Repo Ops 仍禁用 | PARTIAL | 间接证据充分：配置层 `agent_mode=disabled`、`repo_ops_enabled=false`、无 `[agent]` 段，启动事件 `agent_bridge_enabled=false`，按注册契约（`main.py` 注册条件）Agent/Repo 工具不会被注册；但 tools/list 直接清单未保留，不作直接 PASS |
| 拒绝遍历和未授权路径 | PASS | `read_text_file ../escape-probe.txt` → `PATH_OUTSIDE_WORKDIR`；`create_text_file /tmp/v0243-escape-probe.txt` → `PATH_OUTSIDE_WORKDIR`（paths must be relative to the workdir） |
| 临时进程与目录已清理 | PASS | 子进程 SIGTERM 优雅退出（`Shutting down → Finished server process [34187]`，退出路径未再产生新请求）；无残留进程、无残留监听端口；临时目录（含 serve.log）已整树删除；`/tmp` 无本任务遗留文件 |
| 生产四服务前后不变 | PASS | 前后 `serverfs service status` / `serverfs service cloudflare status` 完全一致（PID 1779/1790/1784/1786 不变，enabled 状态不变，unmanaged_tunnel_pids 前后为空）；Git HEAD 与 9 项既有未提交条目前后一致 |

## 未通过事项与原因

- 精确失败阶段、子进程退出状态与经脱敏的错误摘要：
  - 失败阶段位于**本地验收编排脚本（非产品代码、非服务端）**：子进程关停后的日志读取函数对已关闭的文件对象执行 `flush`，抛出 `ValueError: flush of closed file`，脚本在打印最终结果 JSON 之前中断。由此，客户端侧采集的状态码、tools/list 工具清单与 SDK 断言摘要未能输出保留。
  - 子进程退出走 SIGTERM 优雅路径（服务端日志记录完整 shutdown 序列）；由于同一编排缺陷，进程退出码未落入保留输出。本次不重复启动补测（任务书限定一次启动，且禁止盲试）。
  - 服务端审计日志（已随临时目录清理，关键行已摘录于上表）显示**产品侧无任何失败**：唯一失败均为按设计应被拒绝的请求（REVISION_CONFLICT、PATH_OUTSIDE_WORKDIR×2、PATH_NOT_FOUND）。
- 确认的事实 vs 推断/不确定性：
  - 已确认：临时配置策略与加载器预检；loopback 监听与单次启动；initialize/list_workdirs 会话成功；创建→读→带 revision 编辑→再读→删除全链路成功；错误 revision 被拒；两类越界路径被拒；子进程优雅退出且无残留；临时资源已清理；生产四服务、Git 工作树、unmanaged_tunnel_pids 前后不变；生产 Token/env 权限隔离（布尔）通过。
  - 未确认（证据未保留，不作 PASS）：匿名/错误 Bearer 被拒的运行时 HTTP 状态码；tools/list 工具清单（Agent 10 项、Repo Ops 4 项、二进制 2 项工具的直接缺席断言）。相关代码路径与配置证据与预期一致，但按任务书要求不以推断充当验证结果。
- 是否存在遗留临时资源：**无**。测试子进程已退出、临时目录与 serve.log 已删除、无残留监听；报告内证据为日志摘录副本。未清理任何不属于本任务创建的目录、进程或历史状态。

## 交接结论

- 下一步可由 ChatGPT 安全开展的操作：
  1. 基于本报告与 `dev_plan_v0.24.3.md` 独立决定是否进入生产 Cloudflare 切换评估；本报告不构成切换授权。
  2. 若决策要求完整直接证据，可在**新的授权**下安排一次补齐验证，仅覆盖两项缺口（匿名/错误认证 401 状态码、tools/list 工具清单）；本次任务书的“一次启动”额度已用尽，本次未做第二次启动。
  3. 产品侧无阻塞性缺陷被观测；v0.24.3 的 `read_only=false` 写路径在隔离 HTTP 面上行为符合预期（创建/编辑/删除/修订保护/路径围栏均正确）。
- 仍然需要人工处理的阻塞：无。唯一缺口是上述两项客户端侧证据未保留，属验收编排缺陷，不需要人工修复生产环境。
- 明确未执行：生产服务的 install/uninstall/start/stop/restart/launchctl/Login Items 操作、公开 Cloudflare 写入（Dashboard/DNS/WAF/隧道切换）、生产 Agent 任务、生产 workdir 权限或配置变更、生产 Token/env/bridge.json 内容读取、项目源码/测试/Skills/文档修改、Git 提交/推送、版本发布、sudo/提权/关沙箱。
