# macOS v0.25.0 — Shared Core 三进程、授权轮换及资源所有权验收

日期：2026-11-15  
作用域：macOS Apple Silicon、ServerFS 源码工作树与完全隔离的测试实例。  
结论：**Shared v2 源码/签名包预检、独立进程 HTTP+IPC、owner 重启/授权轮换/撤销通过；SMAppService 实际注册与公网端到端尚未通过验收。**

## 实际完成的开发

1. **运行时可执行性门禁**：`src/serverfs_mcp/darwin_instance_shared_staging.py` 新增 `_probe_core_python_runtime`，在 staging 写入状态前，用指定的无符号链接、用户拥有的 Python 解释器在隔离模式 `-I -B` 实际导入 `serverfs_mcp.cli`、`mcp`、`httpx2`。测试发现把 `.venv/bin/python` 解析为 uv 基础 Python 会丢失虚拟环境依赖，单独拷贝可执行文件也不能携带其 `libpython` 与 `site-packages`。没有将测试用 `.pth` 借用依赖的做法当成生产部署标准。
2. **Swift 原生可执行文件路径**：`native/macos/service_manager/Sources/ServiceLauncher/main.swift` 增加 Shared Core 专用物理路径检查，避免 Foundation `standardizedFileURL` 将 `/private/tmp` 重写为 `/tmp` 造成严格路径不一致，同时依然拒绝符号链接、非规范路径与非用户拥有的可执行文件。现有其他服务的通用路径解析保持独立。
3. **双 Core 实例注册表**：`src/serverfs_mcp/darwin_instance_runtime.py` 严格识别 `v0.25-named-shared-core`，避免已 staging 的第一个 Shared Core 被第二个 Core 的端口预检误判为未知实例；Shared Core 只声明 `http-origin`，不能伪装拥有 Bridge/Connector 资源。Cloudflare/OpenAI 凭据碰撞扫描亦可识别合法 Shared peer，继续拒绝无效身份。
4. **公共 Agent 查询适配**：`src/serverfs_mcp/darwin_shared_agent_adapter.py` 接纳不带显式 `workdir` 的任务查询/交互方法；签名 v2 客户端始终在每个 IPC 请求中绑定 grant 的工作目录。显式提供不同 workdir 仍拒绝，Repo Ops 仍不可通过 Agent 接口使用。
5. **独立 Bridge writer 保护**：`src/serverfs_mcp/darwin_instance_bridge_identity.py` 在新建 owned Bridge 的只读预检中，解析已有 Shared Core 的私有绑定及 owner v2 配置，检查跨 Bridge 可写路径重叠；多个 Core 引用同一个真实 owner 只计算一次，缺失、权限不安全或身份不一致均拒绝。Shared Core 不被赋予 Bridge 生命周期。

## 可复现验收证据

| 门禁 | 结果 | 实际边界 |
| --- | --- | --- |
| Shared staging/manifest/interpreter 定向回归 | 45 passed | 假解释器与可执行性门禁 fail-closed |
| 本轮签名 App + 复制测试 venv + 原生 launcher | **2 passed** | Swift 编译、codesign、正向/负向 `--validate-only`，随机 `shgate*` 安全卸载 |
| Shared 端口注册表 + bridge identity/staging 第一轮 | 53 passed | 发现并修复此前第二 Core staging 阻断 |
| 两个 Core 真实 HTTP + 同一个 pytest 进程中的 Bridge | **1 passed** | 真实两个 Core 进程、MCP HTTP、任务隔离、停止 Core A |
| **三个独立进程**（1 Bridge + 2 Core） | **1 passed** | owner 退出/重启；Core B HTTP 不停机且 IPC 自动恢复 |
| 三进程追加 owner grant 轮换及撤销 | **1 passed** | 旧密钥失效，更新 Core B 私有绑定并自行重启恢复，撤销后 Agent RPC 被拒绝但文件服务继续 |
| Shared owner writer + 端口/Connector inventory + existing bridge identity/staging | **61 passed** | 同 owner 去重、不同 Bridge 的可写工作目录冲突、无效 owner fail-closed；Ruff/diff 均通过 |

**最终扩大回归已通过：**选中 47 个测试文件，共 **534 passed、0 failed、4 skipped**（pytest exit 0，耗时 38.87 秒）；两个 opt-in 环境变量均保持关闭，单独的原生签名/三进程验收已在上表验证。16 个本轮涉及的 Python 文件 Ruff check、Ruff format --check 和 `git diff --check` 全部 exit 0。无生产 ServiceManagement 注册及启动操作；测试专属 basetemp 清理时遇到 fixture 文件权限限制，已只恢复该临时目录的 owner 写权限并确认完整清理。此前的 510 passed / 1 skipped 属历史基线。**测试数不能相加**：各定向用例与扩大回归有重叠。

隔离三进程测试使用 `/tmp/sfsh3p-*` 私有目录、随机 localhost HTTP 端口、Unix socket、合成 Core HMAC 密钥和 FakeAdapter。每个 Core 的私有 TOML、绑定、HTTP Bearer 认证及已签名 App 均独立，Bridge owner 的 `core-grants.json` 经显式 owner 操作生成。两个 Core 可访问同一物理 workspace，但 Core B 无法查询 Core A 的 Task ID。owner 修改 grant 后必须重启 Bridge 才生效；Core B 在自己的绑定更新及重启后才使用新密钥。测试自动终止子进程，核验 `sfsh3p` 临时目录清理，未修改用户生产数据或已注册服务。

## 未完成的 v0.25.0 发布门禁

- **真实 SMAppService**：尚未将两个签名 Shared Core 以各自 app identity/LaunchAgent 实际注册、启动、停止，也没有验证 macOS 原生安装/卸载与授权弹窗、自动恢复。
- **生产 runtime 打包**：测试通过的私有复制 venv 依赖已经安装的测试 site-packages 和只读源码路径；生产应提供受版本固定、可独立部署并可验证依赖完整性的解释器/虚拟环境，不可直接使用 uv 的基础 Python 路径或测试借用的 `.pth`。
- **Connector / 公网 E2E**：未完成 v0.25 Shared Core 与 Cloudflare Tunnel 连接层的原生完整隔离部署、ChatGPT MCP 端到端工具调用及断线恢复。
- **跨平台发布**：未核对 Windows/macOS/Linux 同一 commit SHA 的完整 0.25.0 回归、版本号与发布清单；本轮未 Git commit/push/tag，也未切换生产实例。

## 建议的后续验证顺序

先完成 production-grade Shared Core Python 环境安装/版本固定与 native launcher 消费，再使用唯一命名的临时签名实例开展 SMAppService 注册、双 Core 连接和安全清理；之后完成 Cloudflare 端到端验证及三平台同 SHA 的发布验收。Shared Core 只管理本 Core，Bridge grant 由 owner 管理，Connector 生命周期保持独立。不得把隔离子进程测试写成生产服务注册验收。
