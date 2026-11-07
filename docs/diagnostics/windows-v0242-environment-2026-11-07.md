# ServerFS v0.24.2 Windows 环境调查报告

**调查类型：** 只读；未修复、未启动测试进程、未改服务或计划任务。  
**采样结束：** 2026-11-07 15:20:44 UTC（23:20:44 Asia/Shanghai）。  
**目标：** 当前 WorkPC 交互用户与本仓库。用户姓名、SID、机器名、完整私有目录均未记录。

## 执行环境与采样方法

Windows 注册表只读字段为 `ProductName=Windows 10 Pro`、`DisplayVersion=25H2`、`CurrentBuildNumber=26200`、`UBR=9457`。此处保留系统原始字段，不据 `ProductName` 单独推断 Windows 代际。Shell 为 64 位 Windows PowerShell 5.1.26100.9444（Desktop）。`whoami.exe /groups /fo csv /nh` 成功；管理员组 SID 不存在，完整性级别为 `S-1-16-8192`（Medium）。身份名称与用户 SID 已脱敏。

当前用户 connector-env 中的 `serverfs.exe --version` 返回 **0.23.3**。PATH 中没有 `serverfs`，因此从当前用户安装位置运行了只读 `service status`。该已安装 CLI 早于本仓库的 v0.24.2 候选；源码行为说明需考虑这一版本差异。PATH 中没有 `cloudflared`，v0.24.2 计划版本对应的默认用户级候选文件也不存在。

实际执行的只读检查包括：

- `Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'`
- `whoami.exe /groups /fo csv /nh`
- `serverfs --version`（PATH 查询）；当前用户 `serverfs.exe --version`、`serverfs.exe service status`
- `schtasks.exe /Query /TN "\ServerFS Native Tunnel" /XML /HResult`
- `schtasks.exe /Query /FO CSV /NH`
- 按名称筛选 `Get-Service`、`Get-Process`
- `Get-Command cloudflared` 与产品默认候选文件存在性检查
- 对仓库根目录和 `native/windows/target` 执行 `Get-Acl`

本次 PowerShell 的 .NET 身份/注册表方法探查出现 `MethodInvocationNotSupportedInConstrainedLanguage` 和 `CannotCreateTypeConstrainedLanguage`。没有尝试解除语言限制。目录 ACL 显示目标存在且 DACL 未设为 protected，但未计算当前 token 的有效写权限，也没有进行写入试验。进程命令行未取得可安全记录的摘要。

## A. v0.24.2 独立 Cloudflare 测试进程未启动

### 观察

| 命令 / 来源 | 原始关键状态或错误 | 推论 | 信心 |
|---|---|---|---|
| [`docs/v0.24.2-release-checklist-2026-11-07.md`](../v0.24.2-release-checklist-2026-11-07.md) 中记录的 WorkPC 试验 | 曾在隔离的 `native/windows/target/` 尝试；Agent 报错：`workspace-write sandbox has no writable root capability SIDs`；bootstrap 命令启动前被拒绝 | 拦截发生在 Agent 的 workspace-write 沙箱/根 capability 建立阶段，早于 bootstrap 和目标子进程执行 | 高（拦截阶段）；中（错误发出组件） |
| `whoami.exe /groups /fo csv /nh`；当前 PowerShell 错误 | 当前交互 token 非管理员、Medium integrity；PowerShell 显示 ConstrainedLanguage 限制 | 本次交互 shell 的身份与能力受限有证据；不能据此确定此前远端 Agent 的 provider、SID 或 sandbox 配置 | 高（本次 shell）；低（此前 Agent 身份） |
| `Get-Acl` 仓库根及 `native/windows/target` | 目录存在、DACL 未设为 protected；有效权限未计算，也未尝试写入 | 不能确认 NTFS 的有效授权；目录 ACL 不足以解释或替代缺失的 sandbox root capability SID | 低 |
| `Get-Command cloudflared` 和默认用户级候选文件存在性检查 | PATH 未找到；默认候选文件不存在 | 当前用户常规位置未发现 Cloudflare 客户端；没有搜索其他目录，也未读取秘密配置 | 中 |

### 初步诊断

现有证据最支持 **Agent provider/执行沙箱的 workspace-write capability 配置层**：错误明确提到缺少 writable root capability SID，且历史记录指出进程启动前就被拒绝。没有证据表明 ServerFS MCP workdir 授权、NTFS 拒绝或 cloudflared 本身是此次启动拦截点。

该错误来自远端 Agent 沙箱；本次无法观察其 provider、沙箱服务版本、执行身份或 capability SID 映射。具体由 provider 哪个内部组件发出仍待核验。不能通过关闭沙箱、提升权限或把仓库移动到未授权根目录来绕过。

### 隔离方案

1. **仅验证 bootstrap：** 由 Agent/provider 管理方为精确的 scratch 根正常配置 writable root capability SID，保留 `workspace-write` 沙箱和非管理员身份；scratch 不含生产配置或凭据。如果 provider 无法按正常能力授权提供该根目录，则停止此路径。
2. **完整 Connector 与公网 E2E（推荐）：** 使用独立的非生产 Windows 11 x64 VM 或工作站，以及专用非管理员用户、独立的 ServerFS 当前用户 Task Scheduler owner、隔离 workdir、测试 hostname 和专用测试凭据。该方案不改变当前 WorkPC 的生产组件，并满足计划要求的独立测试 owner。

不能把 `service cloudflare install` 当作当前 WorkPC 的隔离启动方式：v0.24.2 设计规定一个 Windows managed service owner 只允许一个 Tunnel provider，而当前生产 owner 的进程仍在运行。

## B. Tunnel / Agent Bridge 进程存活，但计划任务字段为 false

### 观察

| 命令 / 来源 | 原始关键状态或错误 | 推论 | 信心 |
|---|---|---|---|
| 当前用户 `serverfs.exe --version` 和 `serverfs.exe service status` | 版本 `0.23.3`；退出码 0；`backend=windows-task-scheduler`、`provider=null`、`registered=false`、`enabled=false`、`task_valid=false`、`task_error=""`、`task_query_error=""`、`manifest_installed=true`、`manifest_error=""`、`watchdog_alive=true`、`watchdog_pid=177060`、`runner_alive=true`、`runner_pid=184356`、`runner_unclean_exit=false` | 当前用户 manifest 可读，status 同时报告 watchdog/runner 存活和预期任务未注册。`provider=null` 不能证明当前 provider | 高（该次 CLI 输出） |
| `schtasks.exe /Query /TN "\ServerFS Native Tunnel" /XML /HResult` | 退出状态 `0x80070003`（PowerShell 显示十进制 `-2147024893`） | 与 status 的 `registered=false` 一致：本次查询上下文未返回预期根路径/名称的任务 | 高（精确名称/路径）；不能排除其他名称或路径 |
| `Get-Process` 名称筛选 | 采样时 `pythonw` PID `177060` 与 `184356` 存在，且与 status 返回的 PID 一致 | 两个进程当时存在；命令行、owner SID、实际角色未由本次采样独立确认 | 高（PID 存活）；低（角色/owner） |
| `Get-Service` ServerFS 名称筛选 | 没有匹配的 SCM 服务 | 符合 ServerFS 采用 Task Scheduler 而非 Windows SCM 的设计；不能证明计划任务存在 | 中 |
| `schtasks.exe /Query /FO CSV /NH` | 退出码 1；未得到可判读的任务列表 | 是否另有不同名称、路径或 scope 的 ServerFS 任务尚未确认 | 低 |

### 字段含义与初步诊断

- `backend=windows-task-scheduler` 表示生命周期后端类型，不表示当前任务已注册或正在运行。
- 在本仓库实现中，`registered=false` 表示查询认为预期任务缺失；查询权限或其他错误应填入 `task_query_error`，并使用未知状态。本次直接 `schtasks` 查询也返回 `0x80070003`，且 status 的 `task_query_error` 为空，因此现有证据不像是单纯把权限错误误报成 false。
- `enabled=false` 在 `registered=false` 时可由“任务缺失”投影得出，不等于找到了一个已禁用的任务。
- `task_valid=false` 在没有任务 XML 可供校验时保持默认值；它不证明某个已注册 XML 的契约校验失败。
- `watchdog_alive` 和 `runner_alive` 是独立的进程状态判断，因此可与 `registered=false` 同时出现。当前状态意味着进程仍在，但本次查询未证明其预期持久化 owner 存在。
- 可能原因包括任务被删除、改用不同名称/路径/scope，或 CLI 版本和查询上下文差异。当前没有证据区分这些原因。已确认 CLI 为 0.23.3，但这不足以解释直接任务查询返回的 `0x80070003`。

### 源码关联

- [`windows_service.py`](../../src/serverfs_mcp/windows_service.py#L42) 定义固定任务名 `ServerFS Native Tunnel`；[`service_home`](../../src/serverfs_mcp/windows_service.py#L91) 按当前用户本地应用数据定位 manifest/state。
- [`_query_task_xml`](../../src/serverfs_mcp/windows_service.py#L426) 使用 `schtasks /Query /TN ... /XML /HResult` 查询；缺失 HRESULT `0x80070002/0x80070003` 在 optional 查询中映射为未注册。[`validate_registered_task`](../../src/serverfs_mcp/windows_service.py#L597) 将 principal SID、`InteractiveToken`、`LeastPrivilege`、触发器、`IgnoreNew`、执行限制及 action/工作目录与 manifest 校验。
- [`status`](../../src/serverfs_mcp/windows_service.py#L1278) 分开查询任务/manifest 和 watchdog/runner 存活；`enabled` 在任务缺失时投影为 false，`task_valid` 初始为 false。CLI status parser 见 [`cli.py`](../../src/serverfs_mcp/cli.py#L220)。当前安装 CLI 是 0.23.3，源码审查说明当前仓库行为，不代表已逐 SHA 核对该旧二进制。
- Windows Cloudflare 默认用户级 binary 路径规则见 [`windows_cloudflare_config.py`](../../src/serverfs_mcp/windows_cloudflare_config.py#L137)。

## 已确认、未确认与风险

**已确认：** 当前交互用户非管理员；当前用户 CLI 为 0.23.3；manifest 存在且 status 报告 watchdog/runner 存活；预期根路径/任务名查询未找到；没有匹配的 SCM 服务；默认 Cloudflare 客户端路径和 PATH 均未发现客户端。此前 Agent bootstrap 尝试在沙箱拒绝阶段停止。

**尚未确认：** 是否存在其他名称/路径/scope 的任务；两个 PID 的命令行、owner 和实际角色；Tunnel 与 Bridge 的端到端健康；此前 Agent 的 provider 与 capability SID；目录的有效 NTFS 写权限。当前 status 的 Tunnel liveness 不等同实际 OpenAI Tunnel 连接或 Agent Bridge RPC 健康。

**生产风险：** 本次未观察到进程中断；但预期 Task Scheduler 项未被找到，登录/心跳恢复持久性不能确认。若进程退出或用户会话/系统重启，自动恢复可能失效。不要立即注册第二个 owner、停止或重启现有进程，以免与仍存活的 managed tree 形成重复 owner。

**未执行：** 未读 `.env`、Token、secret 或授权头；未做网络请求、下载、隧道启动、MCP/生产写入测试；未启停、注册或删除服务/任务；未修改配置、环境变量或生产组件；未重跑被沙箱拦截的 bootstrap。

## 安全下一步

1. 以同一非管理员用户在 Task Scheduler Library 中只读核对 `ServerFS Native Tunnel` 的实际路径、principal、logon type、run level、Enabled 和 State；不要修改。
2. 若该视图也没有任务，先通过获准的只读方式确认两个 PID 的 owner 与命令行，再判断任务是否转移或删除。若确认任务确实缺失且进程仍由该 ServerFS 安装持有，修复候选应使用版本匹配的产品生命周期入口恢复同一 current-user Task Scheduler 契约，不能先结束进程。执行前须确认该版本会安全复用已存活进程并保证单 owner；本次证据不足以安全指定或执行会新建/启动任务的命令。
3. Cloudflare 实测采用独立非生产 Windows 用户环境，并先由 provider 正常修复 scratch root capability SID；不依赖读取生产凭据。

## Release Gate 结论

**HOLD / NOT PASS。** v0.24.2 仍为候选；WorkPC Agent 沙箱在 bootstrap 前拒绝，当前用户默认位置没有 Cloudflare 客户端，且没有独立非生产 Connector 生命周期、公开 HTTPS/ChatGPT MCP 与长 SSE/重连证据。Windows CI、签名校验和本机 loopback 验收不等同 WorkPC 公网 E2E。不得将现有 CI 或本报告视为 v0.24.2 Windows release gate 通过。参见 [`dev_plan_v0.24.2.md`](../../dev_plan_v0.24.2.md) 与 [`release checklist`](../v0.24.2-release-checklist-2026-11-07.md)。


---

**报告交付说明：** 调查正文由 WorkPC Windows Agent 生成。Agent 自身文件写入被沙箱拒绝，未授予绕过沙箱权限；结束 Agent 任务后，由主协调者使用 ServerFS MCP 授权的原子文件创建接口保存，随后回读核验。报告生成时尚未提交 Git。
