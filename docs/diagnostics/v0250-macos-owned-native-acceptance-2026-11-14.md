# ServerFS MCP v0.25.0 — macOS owned 原生验收

日期：2026-11-14  
范围：macOS `bridge_mode=owned`，一个 Core 独占一个 Bridge。  
结论：已通过真实 macOS ServiceManagement 注册、启动、Core→Bridge RPC、注销及清理验收；**不代表 v0.25.0 跨平台版本可以发布**。

## 1. 架构与操作边界

- 现有 default OpenAI Tunnel、Agent Bridge、HTTP Core、Cloudflare Connector 全程保留；未注册/注销/重启生产服务。
- 非默认实例使用独立 Bundle ID、LaunchAgent label、私有 0600 manifest/配置、0700 目录、独立 AF_UNIX socket、Bridge 状态和租约目录。
- `shared` 未授权前依然拒绝服务；不假设同 UID 下的 IPC 路径是完整的安全身份。
- macOS ServerFS 运行入口 `serve --macos-instance <id>` 显式绑定命名 owned 身份；拒绝回退到生产 Bridge。
- `darwin_instance_owned_binding.py` 验证实例清单、TOML SHA256、Core/Bridge 参数、私有环境、策略及 socket/锁目录归属。
- `darwin_owned_bridge_supervisor.py` 启动、监督和停止 Bridge 子进程；不伪造旧代 Provider 执行已完全收容的证明。
- Swift ServiceManager 只允许通过签名 App/私有清单检查的命名 owned Bridge 注册；`register-preflight agent-bridge` 提供不注册的只读资格检查。Swift ServiceLauncher 接受经过身份和目录检查的 named owned 清单，继续拒绝不受支持的 shared。
- `darwin_instance_owned_lifecycle.py`：Bridge→Core 启动、Core→Bridge 停止、局部失败反向回滚；验证 0600 socket、UID、Bridge RPC、Core 监听端口的 PID 和 HTTP 401。
- `darwin_instance_owned_unstage.py`：仅允许完全未注册、无运行 PID/socket、无日志/任务数据或未知文件的 pristine 实例卸载。业务实例的有状态清理必须另行归档。
- CLI：`owned-stage`、`owned-preflight`、`owned-policy-check`、`owned-status`、`owned-start`、`owned-stop`、`owned-unstage`。

## 2. 真实 Mac 验收

| 验收 | 结果 | 证据 |
|---|---|---|
| 独立签名 App + Bridge 私有配置与策略漂移校验 | PASS，退出码 0 | `v0250_owned_bundle_policy_smoke.py` |
| Swift ServiceLauncher 直接 exec 受监督 Bridge 与真实 Core，再通过 MCP 验证 | **PASS，退出码 0**；测试进程/owner/socket 均清理 | `agt_b60c8a5616b63afb7f748714` |
| 真实用户目录中签名 App 的只读 native `register-preflight` | `eligible=true`，未调用 register | `agt_056cccaabe4715275663a88d` |
| 原生 SMAppService 注册 Bridge→Core、MCP 验证、Core→Bridge 注销 | **PASS，退出码 0** | `agt_d7135a8786ce3e60279f6c7f` |
| 临时实例及生产进程连续性 | `errors=[]`、`test_instance_removed=true`、`retained_test_instance=null`、`production_pids_unchanged=true` | 同上 |

真实 SMAppService 验收脚本：`deployment/macos/v0250_owned_smappservice_live_acceptance.py --execute`。仅使用新随机命名实例、临时工作目录和合成 HTTP key，通过只读的不存在 task-ID 查询证明 Core→Bridge 到达；没有提交 Codex/Claude/Qoder 任务，没有使用公网 Connector。若需要 macOS 用户批准，脚本不会绕过审批，而是按自身回滚门禁处理。

历史问题已纠正：最初只读预检虽然 `eligible=true`，但 Python `_manager` 白名单未包含命名 owned `agent-bridge` 的状态查询，导致测试卸载拒绝。修复后对原残留实例 `v25-smoke-187899fd` 进行了精确保守卸载，退出码 0；真实用户的 `instances` 目录当时为空，生产服务 PID 仍存在。记录：`agt_c03fca79d122515b80ae5d52`。保留这一失败审计，不将其误报成首次即成功。

## 3. 最终回归

| 门禁 | 结果 |
|---|---|
| 新增 owned 生命周期、身份、staging、诊断、卸载定向测试 | **99 passed、0 skipped、0 failed**，`agt_c2abe5aba907930e722ad834` |
| 主项目完整 `ruff check .` | **退出码 0** |
| 主项目完整 `ruff format --check .` | **退出码 0，571 文件** |
| 主项目完整 `pytest -p no:cacheprovider -q` | **真实终态退出码 0，100%**，`agt_5488cf83933c4aff21a29bac` |
| 独立 Agent Bridge 包完整 Ruff lint / format | **退出码均为 0，105 文件** |
| 独立 Bridge 包完整 pytest | **真实终态退出码 0，100%**，`agt_7889f33defc2b2fa199f6833` |
| Swift ServiceManager / ServiceLauncher typecheck | **退出码 0**，例如 `agt_3023a71d8930a93e2bbd4532` |
| `git diff --check` | **退出码 0** |
| 生产默认服务 | 四个 PID 与真实 E2E 前一致；未操作生产服务 |

两套全量 pytest 的 quiet 输出未提供可靠的测试总数；不推断总数。先前主项目全量测试仅有 ~37% 进度、未获得退出码的问题，本次通过持久执行会话取得了明确 exit 0。

## 4. 尚未满足的 v0.25.0 整体发布门禁

1. **Shared Bridge**：必须实现 Bridge 端强制校验的逐 Core 授权、工作目录/操作范围、任务归属隔离、策略代次及撤销；不得仅凭相同 OS UID 或 socket 路径视为已授权。
2. **其他连接层组合**：owned Core 的真实 Cloudflare/OpenAI ingress、多 connector ID、资源碰撞/隔离矩阵尚未全部验收。
3. **跨平台**：Windows 与 Linux v0.25.0 需要根据相同实例模型各自完成原生/Compose 全量验收；本记录只证明 macOS owned。
4. **生产级卸载**：当前严格保留非空日志、数据库、租约文件。后续需要审计保留、归档和经授权的清理流程。随机合成验收脚本的受控清理不可直接当成业务卸载操作。
5. **用户级权限边界**：0600 和同 UID AF_UNIX socket 并非同一 Unix 用户下不可信进程间的强沙箱；shared 必须增加逻辑能力授权和撤销。
6. 未提交 Git、未推送、未打 tag、未发布、未切换任何生产 Connector。macOS owned 已具备阶段验收证据，但 `v0.25.0` **尚非 release-ready**。

下一步：先与 Windows/Linux 共享契约对齐 shared grants 的授权与撤销语义，再补 macOS named Connector E2E 和三平台总矩阵，最后统一完成发布收尾。**跨端差异已对照 Windows 2026-11-14 的 Bridge v2 公开 ADR 并制定集成门禁：**`docs/architecture/v0.25.0-macos-shared-v2-integration-gates.md`。Windows 已有独立 v2 HMAC/nonce/task-owner 代码与隔离试验；Mac 当前工作树没有相应公共模块，继续 fail-closed，不在 Mac 另行设计不兼容协议或直接重用 v1 socket。
