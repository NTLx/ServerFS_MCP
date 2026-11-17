# macOS v0.25.0 Shared Core — 完整离线 runtime 与原生签名门禁验收

日期：2026-11-15。平台：Mac Apple Silicon，macOS 27.2.1，uv 0.9.7，CPython 3.12.12。状态：**离线安装 + 原生签名预检已通过；实际 SMAppService 双 Core 注册尚未验收通过**。

## 交付源码

- `src/serverfs_mcp/darwin_shared_runtime.py`：新增创建型（create-only）离线 runtime installer 和 CLI。
  - 读取项目现有 `uv.lock`，使用 `uv export --locked --offline --no-dev --no-emit-project`、`uv build --offline --wheel`、`uv venv --offline`、`uv pip sync --offline --link-mode copy` 与离线 wheel 安装；不修改项目 `.venv`，不连接索引下载。
  - 独立复制真实用户持有的 CPython 可执行文件及匹配 `libpython3.12.dylib`，放入新 venv，暴露无符号链接的 `bin/serverfs-core-python`。
  - 在私有 0700 安装父目录中准备 candidate，记录 `uv.lock`、`pyproject.toml`、requirements、wheel、Python、libpython 的 SHA-256 至私有 0600 `runtime-provenance.json`，用 `-I -B` 验证模块来自新 venv `site-packages`，不借用测试环境 `.pth` 或源码目录。
  - 本机 macOS SDK `renamex_np` + `RENAME_EXCL=0x4` 原子发布，禁止覆盖存在的目录（包含空目录）；发布后再次验证，失败只回滚新候选；不对已有实例、Bridge、Connector 或生产凭据执行任何操作。
- `src/serverfs_mcp/darwin_instance_shared_lifecycle.py`：修复 `shared-start` 原生签名解释器身份判定。管理命令可使用与 Core 不同的 Python；只有签名 App Info.plist 中经过 instance identity / codesign 校验的 `ServerFSSharedCorePythonPath` 才能作为离线 manifest `expected_executable`。真正 Core 执行时仍必须由自身 `sys.executable` 匹配 pin，不能使用用户参数替代签名身份。
- `tests/test_darwin_v0250_shared_runtime_install.py`：离线安装/回滚/隔离环境/动态库与原子独占发布测试。
- `tests/test_darwin_v0250_shared_lifecycle.py`：签名解释器 pin 先于 manifest、Bridge 认证和 ServiceManagement 操作；缺签名、错误 pin 等安全用例。
- `tests/test_darwin_v0250_shared_native_signed_gate_macos.py`：在真实 macOS 上安装完整离线 runtime，构建与签署独立随机 `shgate*` Core App，运行原生 `--validate-only`、只读 `register-preflight` 及正式 strict manifest 校验，随后只删除能证明未注册的测试候选。
- `tests/test_darwin_v0250_shared_smappservice_live_macos.py` 和 `tests/test_darwin_v0250_shared_smappservice_safety.py`：提供仅显式 opt-in 的双 Core SMAppService 真实注册/注销脚本和无注册副作用的清理安全测试。严格记录测试候选三份私有配置 SHA-256、签名 App Python pin、完整 manifest、注册尝试与原生三态注册状态。状态不明/篡改时不盲目注销或删除。

## 已执行验收

| 门禁 | 实测结论 | 说明 |
| --- | --- | --- |
| Runtime 安装与回滚合成测试 | **9 passed、1 skipped** | 含真实 macOS libc `RENAME_EXCL` 独占空目标的拒绝测试；opt-in 安装单独执行 |
| 完整离线 runtime 真安装 | **1 passed，退出 0** | 从锁定 wheel 安装依赖并验证物理 Python + dylib + isolated site-packages，发布后检查成功 |
| 双实例原生注册的 cleanup 安全测试 | **20 passed、1 skipped** | 未启用真实注册，验证未知状态保留现场、私有输入哈希与完整 manifest |
| 修复 `shared-start` 后的生命周期与 manifest 定向回归 | **79 passed、2 skipped** | 修复管理 Python 与 Core Python 不同导致的预检拒绝 |
| 真实签名 App + 独立解释器原生预检 | **2 passed，退出 0** | 构建签名 App、原生 launcher、signed pin 与 manifest 比对；不注册 |
| **真实离线 locked wheel runtime + 签名 App 联合预检** | **2 passed，退出 0，38.96s** | 全程 offline，随机 `shgate*` 已卸载，无 launchd 注册/残留 |

**上一阶段扩大回归（新安装器与 E2E 测试之前）：570 passed、6 skipped、0 failed，pytest exit 0。** 四个 native opt-in 环境变量均明确关闭；20 个涉及的 Python 文件 Ruff check / format --check 均通过，`git diff --check` 通过。扩大回归只使用私有临时目录；本轮唯一 basetemp 因测试设置只读权限，已仅恢复该临时目录 owner 写权限并清理，未发现残留测试进程。未修改生产注册、Bridge、Tunnel、凭据或其他工作树改动。

各测试集合部分重叠，不得相加当作唯一测试数。旧的 534 passed / 4 skipped 是本批次前的历史基线。

## 后续严格运行时验收（同日增量）

- 真实 locked-wheel Python runtime + 两个 HTTP MCP Core + 一个隔离内存 Bridge v2：**1 passed，1 deselected**；覆盖两个 Bearer token、MCP、跨 Core 任务隔离、停止 Core A 后 Core B/Bridge 存活，临时进程/socket 清理。
- 真实 locked-wheel Python runtime + 两个 HTTP MCP Core + 一个**独立 Bridge owner 进程**：**1 passed，1 deselected**；覆盖 owner 重启恢复、密钥轮换和撤销、文件与 Agent 服务独立性，测试目录与进程清理。
- 新增 `verify_shared_core_runtime()` 与 `--verify-only`：核查 0600 provenance、路径、权限、Python/libpython 哈希、项目和锁文件哈希及隔离导入；异常/篡改合成测试 **26 passed、1 skipped**，真实离线安装及安装后复核单项 **1 passed**。原有记录不代表完整 `site-packages` 的逐文件哈希。
- `uv 0.9.7` 的 `uv export --locked --offline` 已在当前真实仓库离线导出 40 个包，退出 0，`pyproject.toml` 与 `uv.lock` 前后 SHA 不变。随后已由 `--frozen` 切换为 `--locked`，对应合成测试 **26 passed、1 skipped**、离线完整安装 **1 passed**。未改现有项目锁文件。

**本阶段最终扩大回归：587 passed、8 skipped、0 failed，pytest exit 0（40.07s）。** 六个显式 opt-in 开关均关闭，10 个相关 Python 文件 Ruff lint/format、`git diff --check` 通过；唯一 pytest basetemp 在恢复其中测试专用只读目录权限后删除。原有 Bridge、Tunnel、cloudflared 与 Bridge socket 仍存活，未发现测试残留进程。增加的跳过项是仅在人工 opt-in 后运行的真实离线 HTTP/owner E2E，不属于回归失败。

**最终原生签名预检（在 `--locked` + provenance 验证改动之后）：2 passed、0 failed，36.95s。** 全部离线安装、签名 native App、`--validate-only`、只读 `register-preflight` 与 pin 校验通过；不存在遗留 `shgate*` 实例、socket、相关进程或测试临时目录。未注册/启动/注销任何 macOS 服务。

**后续增量（2026-11-15 同日）：**已增加产品 `shared-preflight`、托管 runtime 在 staging 和启动前的 provenance 门禁、双签名 Core + 实时 Bridge owner 的非注册授权 E2E，以及实际管理 CLI 子进程的授权/撤销 E2E。最新安全扩大回归是 **598 passed、9 skipped、0 failed**，而本报告中的 587/8 是增量之前的有效历史基线。详见 `docs/diagnostics/v0250-macos-shared-preflight-provenance-2026-11-15.md`，后续原生注册仍未通过验收。

上述试验均不调用 macOS SMAppService 注册；不能据此宣布双登录项生命周期完成验收。当前 broad regression 与最终签名预检分别针对安全无注册测试与签名 opt-in 测试，彼此不应合计为唯一测试总数。

## 真实 SMAppService 门禁未通过的具体情况

第一次执行 opt-in SMAppService 双实例测试前，生产四个默认任务均有 PID（tunnel 1779、bridge 1790、cloudflare 99279、http-origin 99271）。测试在首个 `start_shared_core` 的注册**之前**报 `shared Core preflight failed`；根因是管理 Python 与独立 Core Python pin 不一致。该逻辑已修复并经过上述源码与原生签名验证。失败收尾核实四个默认 PID 完全不变，随机测试实例与对应进程均无残留。

尝试发起**修复后的第二次真实 SMAppService 注册验收**时，执行环境的安全门禁直接拦截请求。因此第二次**没有运行**，不能报告真实 native 注册成功，也不能声称已证明双 Login Item 自主恢复。不会改换其他途径绕过执行拦截。

## 发布前剩余

1. 由获准的本机原生验收路径，真正注册/启动两个随机签名 Shared Core，校验 PID、HTTP MCP、Bridge owner 独立性，再可证实地注销和清理；遇到系统登录项批准需按正常 UI 授权，不可绕过。
2. 生产安装方案需要将创建型离线 runtime 放入正式用户级目录，绑定固定版本/commit 与可复核锁文件，避免 dev checkout wheel 与真实发布 SHA 不一致。
3. 独立 Cloudflare Connector 的公网 ChatGPT MCP E2E、断线恢复和三平台同 SHA 全量回归。
4. 版本号与 CHANGELOG/README 更新、发布与生产切换门禁；本次未做 Git commit/push/tag、未改变生产服务。
