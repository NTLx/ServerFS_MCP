---
title: Windows 原生部署
description: Windows 11 x64 + 本地 NTFS 原生部署——预构建 Rust 内核、v0.23.2 Deterministic Repo Ops、Codex/Claude/Qoder 原生 runtime 与可配置 ACP 委派，以及当前用户 Task Scheduler 持久生命周期。
---

自 v0.10.0 起，ServerFS 可以在 **Windows 11 x64 + 本地 NTFS** 上以 MCP **stdio** 服务原生运行。没有任何端口监听：官方 OpenAI `tunnel-client` 主动连出到控制面，并通过子进程的 stdin/stdout 传输 MCP 帧，因此默认原生 profile 不存在 localhost 监听器。Linux Docker 部署保持不变，仍是 Linux 的受支持形态。

v0.11 为该部署加入 **Agent 委派**：通过同一组十个 Agent 工具与 Named-Pipe Bridge 驱动 Codex、Claude Code 与 Qoder 三个运行时，并已通过真实 ChatGPT 隧道 E2E 验收。v0.23.2 新增已验收的 Windows 11 x64 原生 **Deterministic Repo Ops**，复用 Linux 与已验收 Apple Silicon 原生 macOS 所使用的独立门控、无 Agent fallback Bridge transport。

**v0.14.0** 已加入持久的当前用户生命周期，同时不引入管理员要求。Windows Task Scheduler 2.0 以交互用户的最低权限令牌持有一个无窗口 watchdog；watchdog 负责重启 managed runner，runner 再负责重启 Tunnel generation。

运行时链路：

```text
Windows Task Scheduler 2.0
  当前用户 · InteractiveToken · LeastPrivilege
  LogonTrigger + 每 1 分钟重复的 TimeTrigger
  MultipleInstancesPolicy = IgnoreNew
       -> 无窗口 ServerFS watchdog
            -> managed runner
                 -> 官方 tunnel-client（默认使用 ServerFS 实测版本）
                      -> 净化 supervisor
                           -> serverfs serve
                                -> serverfs-windows-native wheel
                      -> Agent Bridge
                           -> 真实 provider CLI（codex / claude / qodercli）
```

重复 TimeTrigger 是最外层 OS ownership heartbeat：健康 watchdog 存活时始终保持单实例；若 Task Action 意外消失，下一次 Windows 原生触发会重新拉起。正常路径不使用 SCM/Windows Service、LocalSystem、HKLM service template、UAC 提权或保存 Windows 账户密码。

Windows 用户只需 GitHub Release 上的预构建 wheel，完全不需要 Rust、Cargo、MSVC Build Tools 或 Windows SDK。内核 wheel 为 `cp312-abi3-win_amd64`（稳定 ABI，Python >= 3.12）。

## 安装

产品与 Agent Bridge 是**两个隔离虚拟环境中的两个独立发行包**，由冻结的进程边界连接：supervisor 通过 `SERVERFS_BRIDGE_PYTHON` 启动 Bridge。二者绝不会被装进同一个共享依赖图（实测：`serverfs-mcp` 冻结 `mcp==2.2.0`，而 pinned 的 Qoder SDK 声明 `mcp<2.0.0`）。

1. 安装 [uv](https://docs.astral.sh/uv/)（或任意 Python >= 3.12）。
2. 把 **ServerFS 环境**（product + native wheel）与 **Agent Bridge 环境**（bridge wheel）分别安装到两个稳定的当前用户运行时目录：

```powershell
$serverfsRoot = "$env:LOCALAPPDATA\ServerFS\connector-env"
$bridgeRoot = "$env:LOCALAPPDATA\ServerFS\bridge-env"

uv venv --python 3.12 $serverfsRoot
uv pip install --python "$serverfsRoot\Scripts\python.exe" serverfs_mcp-<ver>-py3-none-any.whl serverfs_windows_native-<ver>-cp312-abi3-win_amd64.whl

uv venv --python 3.12 $bridgeRoot
uv pip install --python "$bridgeRoot\Scripts\python.exe" serverfs_agent_bridge-<ver>-py3-none-any.whl
```

3. 在 `serverfs.toml` 旁的 `.env` 中设置 `SERVERFS_BRIDGE_PYTHON`，其值使用当前用户 `%LOCALAPPDATA%\ServerFS\bridge-env\Scripts\python.exe` 的绝对路径，把 supervisor 指向 Bridge venv（tunnel launcher 会把它注入 serverfs 进程环境）。Bridge 环境永远不需要 product 包，product 环境也永远不需要 Bridge 包。
4. 把 `serverfs.toml.example` 复制为 `serverfs.toml` 并配置 workdir。路径是运营者配置——agent 只能看到别名。`read_only = true` 是默认值，workdir 未显式开启时内核拒绝变更。`serverfs.toml` 中不放任何密钥。Agent 委派**默认关闭**：只有加入 `[agent]` 段并设 `enabled = true`、且 workdir 配置了 `agent_mode`/`agent_runtimes` 策略后才会启用；没有 `[agent]` 段的配置行为与 v0.10 完全一致。
5. 运行健康报告，直到退出码为 0：

```powershell
$serverfs = "$env:LOCALAPPDATA\ServerFS\connector-env\Scripts\serverfs.exe"
& $serverfs doctor --config serverfs.toml
```

`serverfs doctor` 会探测 native 后端导入/版本、每个 workdir 根目录打开、文件系统类别、reparse 拓扑、真实策略过滤列表、非变更写能力检查、tunnel-client 版本以及项目托管的 HTTP proxy 可达性——proxy 与 tunnel 凭证永不显示。Windows GA 仅限本地 NTFS：网络共享、FAT/exFAT、ReFS 以及任何无法测量类别的存储都会报 `FAIL`（fail closed，绝不静默放行）。

启用 `[agent]` 后，doctor 还会以只读方式（绝不启动 Bridge 或 provider）额外报告：每个 workdir 的 Agent 策略、各 runtime 的启用状态与 proxy 路由、Agent data home、已存在对象的 private-state 安全性、Agent proxy endpoint 的接线与可达性（endpoint 本身永不显示）、用户身份推导，以及 Bridge 包可被配置的 `SERVERFS_BRIDGE_PYTHON` 解释器导入。

## Windows 上的 Deterministic Repo Ops（v0.23.2+）

Repo Ops 按 Workdir 独立配置，可以在 Agent mode 保持 disabled 时单独开启。公开 MCP 能力面与其他受支持平台一致，固定为 `inspect_repo`、`run_repo_action`、`commit_repo_changes`、`push_repo` 四个工具；它不会启动 Provider runtime，也没有 Agent fallback。

Windows 专属 containment 保持 fail closed：

- 仓库路径在执行前拒绝 NTFS reparse point / junction，与原生文件系统边界一致。
- Repo Ops 子进程先以 suspended 状态创建，成功加入 Bridge 自有 Job Object 后才 resume。
- timeout、cancellation 或 output overflow 必须在共享 writer lease 释放前终止并收敛完整 descendant tree；如果 cleanup 无法被证明，操作会 fail closed，并保留 lease 直到外部重启 Bridge generation。
- Git-for-Windows hardened 路径保持非交互、最小环境，关闭 hooks/signing/askpass/SSH override，并在需要 hooks sink 时使用原生 `NUL`。

v0.23.2 Windows acceptance 使用已安装的 0.23.2 stdio MCP Server 通过受管 0.23.2 Bridge 对 disposable 本地 Git 仓库与 `file://` bare upstream 做了真实验收：27 个工具全部存在；Repo Ops 在 Agent mode disabled 时正常工作；Agent submission 被 `AGENT_DISABLED` 拒绝；`create → inspect → explicit-file commit → exact-upstream push → final clean status` 全程没有 Agent fallback。详见 [v0.23.2 Windows Repo Ops 验收](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.23.2-windows-repo-ops-acceptance-2026-10-28.md)。

## 持久的当前用户生命周期（v0.14.0+）

`doctor` 通过后，只从稳定的 ServerFS `connector-env` 注册任务，再显式启动：

```powershell
$serverfs = "$env:LOCALAPPDATA\ServerFS\connector-env\Scripts\serverfs.exe"
& $serverfs service install --config serverfs.toml --env-file .env
& $serverfs service start
& $serverfs service status
```

全新 `service install` 会把当前用户任务注册为 **disabled**，只写入并回读验证 Task Scheduler 契约，不会静默启动 ServerFS。`service start` 才会启用任务并启动无窗口 watchdog；重新安装已有 managed task 会保留运营者原有的 enabled/disabled 意图。v0.14 安装器还会拒绝从其他 Python 环境安装持久服务，并在注册持久化之前确认 `connector-env` 中的 `serverfs-mcp` 与 `serverfs-windows-native` 已安装且版本完全一致；仓库 `.venv` 被明确排除为持久运行时。

Task Scheduler 同时使用用户登录触发器与无限期的每 1 分钟 `TimeTrigger` heartbeat，配合 `MultipleInstancesPolicy=IgnoreNew` 保证健康 watchdog 始终单实例；外层 Task Action 丢失后由下一次 heartbeat 重新建立。watchdog 以短有界退避重启 runner，runner 再独立重启 Tunnel generation。Windows ownership mutex 会拒绝 managed lifecycle 与直接/manual native Tunnel 同时占用同一个生命周期槽位。

日常运维统一使用：

```powershell
& $serverfs service restart
& $serverfs service stop
& $serverfs service start
& $serverfs service uninstall
```

### 通用状态格式（v0.22.0）

稳定版 v0.23.0 保留此行为。默认 `service status` detail/JSON 保持不变。运营者可显式运行 `serverfs service status --format common-json` 或 `serverfs service status --format common-human` 请求有界、脱敏的通用投影。输出不包含路径、PID、任务名或私有 ID；无法确认时使用 `unknown`/`not-checked`，不会臆测健康状态。更深入的平台与配置诊断请使用 `serverfs doctor`。详见 [Phase F 验收与限制](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.22.0-phase-f-windows-acceptance-2026-10-22.md)。

`service stop` 会先 disable 计划任务，再结束当前 action，因此 heartbeat 不会把人为停止的服务重新拉起。v0.14.0 还支持从旧 v0.13.x 手工 launcher 受控 hand-over：先冻结并再次验证精确进程身份，再切换到 managed lifecycle，绝不会只按可执行文件名杀进程。`deployment\windows\start-native.ps1` 仅保留为兼容/应急路径，不能与 managed service 并行运行。

## Windows 上的 Agent 运行时

v0.19.0 提供通用配置型 `acp-<id>` profile。OpenCode 2.1.2 与 `opencode/glm-5.3-flash-free` 已通过 Windows 端到端验收，覆盖 prompt、持久 session continuation 和 active-tool cancellation/cleanup。进程约束实现先以 `CREATE_SUSPENDED` 创建子进程，将其分配到现有 supervisor Job Object 后再恢复；任务级停止会终止 ACP 进程树，不会创建第二个 Job Object。

每个 runtime 都是一个真实的 provider 进程；能力按 runtime 逐一记录，不做统一宣传：

| Runtime | 模型发现 | 实时转向（live steering） | proxy 路由 |
|---|---|---|---|
| Codex | 目录 + 请求级覆盖 | 支持（其自有 loopback 控制通道按策略绕开 proxy） | 无直连 provider 路由的主机使用 `use_proxy = true` |
| Claude Code | 不支持（`model_discovery: unsupported`） | 不支持（`live_steer = false`） | `use_proxy = true` 时 provider 出站经注入的 proxy（实测：外部 CONNECT 归因于 claude 子进程，0 loopback）；直连主机用 `false` |
| Qoder | 目录（价格状态实时读取，永不硬编码） | 不支持（`live_steer = false`） | 与 Claude 相同的注入模型 |

Agent 出站 proxy 是运营者通过 `SERVERFS_AGENT_PROXY_URL` 提供的**无凭证 HTTP proxy**；带认证的上游 proxy 不在原生支持范围内。Claude/Qoder 的 provider 子进程消费注入的 `HTTPS_PROXY`/`NO_PROXY` overlay；Codex 的 SDK↔CLI 控制流量走 stdio，其 loopback 端点按策略豁免。

## Tunnel 引导与连接

先一次性引导官方 tunnel-client，之后让持久 service 持有连接：

```powershell
& $serverfs bootstrap tunnel-client   # 默认使用 ServerFS 实测的官方版本
& $serverfs service install --config serverfs.toml --env-file .env
& $serverfs service start
```

bootstrap 把 client 存到用户所有的 ServerFS 数据目录下，绝不修改机器 PATH。当前 ServerFS 实测基线为 `v0.0.17`；它只是默认测试版本，不是兼容性锁定。可通过 `--version` 选择其他官方版本；选择非默认版本时还必须通过 `--manifest-sha256` 提供该版本官方 `SHA256SUMS.txt` 的 SHA-256，以保留同样的双重锚定校验链，再对下载档案重新哈希比对。v0.14 managed runner 自动发现已引导的 client，把 tunnel-client 的健康服务绑定到临时 loopback 端口，只以 `file:` 引用传递控制面密钥（密钥文件必须位于所有 workdir 之外），并在 spawn ServerFS 子进程前剥离全部 `CONTROL_PLANE_*`、`TUNNEL_CLIENT_*`、`OPENAI_*`、`MCP_*`、`SERVERFS_PROXY_*` 与 proxy 变量。proxy 配置与 Linux 共用 `.env` 中同样的四个 `SERVERFS_PROXY_*` 字段（仅 HTTP；不支持也不宣称 SOCKS）。

直接 `serverfs tunnel` 仍可用于有界诊断和兼容恢复，但不是 v0.14.0+ 的正常持久部署路径。当前用户 ownership guard 会拒绝 managed lifecycle 与 manual lifecycle 双重占用。

## Windows 上保持不变的契约

所有通道——读、列、stat、find、search 与全部五个变更工具——行为与 Linux 一致：句柄相对遍历且 reparse point 永不跟随、revision 保护的原子发布、fail-closed 的元数据保留、跨通道一致的策略过滤、内容无关的结构化日志。两个平台可见差异已文档化：符号链接拒绝在 Windows 上报 `REPARSE_POINT_NOT_ALLOWED`（Linux 为 `SYMLINK_NOT_ALLOWED`）；Windows 目录 revision 是否随子项移动取决于卷设置，因此 `delete_directory` 依赖物理空扫描。Windows 的 `revision` 仍是元数据推导的乐观并发令牌——设计上存在同 tick、同大小的盲写窗口，ServerFS 不宣称任意进程的 compare-and-swap。

ACP 完整配置、验证、任务生命周期与排障请见 [ACP 指南](../acp/)。
