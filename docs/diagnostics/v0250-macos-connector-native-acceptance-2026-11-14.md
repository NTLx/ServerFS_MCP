# ServerFS MCP v0.25.0 — macOS 命名 Connector 独立生命周期验收

**日期：** 2026-11-14  
**结论：** macOS 非默认 `bridge_mode=disabled` 文件型 Core 已分别通过合成 Cloudflare/OpenAI Connector 的**真实 SMAppService 原生生命周期**验收。未访问任何 Cloudflare/OpenAI 公网隧道；本阶段不是公开 MCP ingress E2E，也不是 `owned` Core 的 Connector 验收。

## 1. 修复和行为门禁

- `src/serverfs_mcp/darwin_instance_runtime.py` 的 `start_component` 原本在 `register` 返回 `requires-approval` 时直接退出，可能留下待批准的命名服务注册。现将该路径与就绪失败统一处理：**只注销本次新注册的组件，验证注销终态、确认该命名 PID 已退出**；若回滚失败则明确报错，不视为成功。
- `tests/test_darwin_v0250_instance_runtime.py` 新增拒绝审批时的回滚断言；本阶段该文件及实现的 Ruff lint/format 退出码均为 0，目标 pytest **28 passed，exit 0**（任务 `agt_d57c66884870c6e839f4fd58`）。
- 文件型 Core 停止前检查已注册的 Cloudflare/OpenAI Connector，不能自动越级停止已在使用其 HTTP 上游的 Connector。
- 既有默认 OpenAI Tunnel、Bridge、Cloudflare、HTTP Core 不经上述非默认组件管理入口触及。

## 2. 原生 SMAppService 验收

| 验收 | Cloudflare（合成） | OpenAI HTTP Connector（合成） |
|---|---|---|
| 实机脚本 | `deployment/macos/v0250_named_connector_lifecycle_acceptance.py --execute` | `deployment/macos/v0250_named_openai_lifecycle_acceptance.py --execute` |
| 命名 Core 和 Connector | 各自通过 macOS 原生 SMAppService 注册/启动 | 各自通过 macOS 原生 SMAppService 注册/启动 |
| 可执行文件 | 合成 shell wrapper，仅 `exec /bin/sleep 300` | 合成 shell wrapper，仅 `exec /bin/sleep 300` |
| 凭据 | 一次性 Cloudflare 测试 token 文件 | 独立随机 Tunnel ID、合成 control-plane key、匹配 Core HTTP key 的私有 Bearer 文件；清单只存路径，不存密钥内容 |
| 独立性 | Connector 停止和重启不改变 Core PID | Connector 停止和重启不改变 Core PID；Core 在 Connector 仍注册时不能直接停止 |
| MCP 访问 | 重启前后两次真实 Core 文件 CRUD | 重启前后两次真实 Core 文件 CRUD |
| 清理 | 注销测试命名服务、删除仅其生成的日志与私有候选状态 | 同左 |
| 最终证据 | **PASS，exit 0，errors=[]**，`agt_d57c66884870c6e839f4fd58` | **PASS，exit 0，errors=[]**，`agt_3984f729aff40e55baf62613` |

两次验收均返回 `named_state_removed=true` / `named_instance_removed=true`，`retained_diagnostic_dir=null`，`production_pids_unchanged=true`。Cloudflare 侧 `mcp_roundtrips=2`、`connector_only_stop_restart=true`；OpenAI 侧 `mcp_roundtrips=2`、`private_credential_binding_verified=true`、`connector_stop_restart_preserves_core=true`。

**不应混淆的验证边界：** 真正被启动的可执行文件是无网络行为的`/bin/sleep`，不是线上 cloudflared 或 tunnel-client。本测试证明 Mac ServiceManagement 注册/注销及进程生命周期边界，不证明 OpenAI control plane、Cloudflare 公网域名、消息重连、SSE、session、认证转发或真实远程 MCP 工具调用。

## 3. 同一 Core 上双 Connector 原生并行验收

- 脚本：`deployment/macos/v0250_dual_connector_lifecycle_acceptance.py --execute`；执行记录：`agt_1e8d426a40ab7b2f70a08ba1`。
- 使用一个随机 file-only Core 实例，同时通过真实 SMAppService 注册独立的 Cloudflare 和 OpenAI Connector。三个服务具有各自的 PID；两个 Connector 均执行 **无网络的合成 `/bin/sleep`**，不是实际云隧道客户端。
- 验证 Core 仍有 Connector 注册时拒绝停止；分别停止、重启 Cloudflare 和 OpenAI，另一个 Connector 与 Core 的 PID 保持不变。
- 真实本地 MCP 文件往返 **4 次通过**。测试进程和私有实例被清理，四个生产服务 PID 不变。
- **实际退出码 0，`result=PASS`，`errors=[]`**。这证明同一 file-only Core 的本机双 Connector 生命周期，尚不证明两个公网 ingress 同时工作或双路身份/会话策略一致。

## 4. 仍待完成

1. **双真实公网 Connector E2E**：使用独立非生产 Tunnel ID/域名与密钥，验证 HTTP MCP initialize、鉴权、工具/CRUD、长连接、会话、通知、重启与断线恢复；不可复用现有生产隧道。
2. **`owned` Core + Connector**：当前 Connector staging/runtime 入口仅接受文件型 Core，不可将本次结论外推到 owned Bridge 模式。
3. **同 provider 多 Connector ID**：模型中有 `connector_id`，Mac 当前仅每个实例一个 Cloudflare、一个 OpenAI 服务 label，尚未完成同 provider 的多实例服务拥有者逻辑与碰撞矩阵。
4. **跨平台 shared Bridge v2 协议**：Mac 继续拒绝未经 Core grants/HMAC 认证的 shared Core；按 `docs/architecture/v0.25.0-macos-shared-v2-integration-gates.md` 等待公共 v2 合并后再做 Darwin 适配。
5. **版本发布**：本轮补丁完成后，主项目完整 `ruff check .`、`ruff format --check .`、`pytest -p no:cacheprovider -q`（实际退出码 0，进度 100%，没有可靠总数）和 `git diff --check` 均通过；记录 `agt_aa578cce794aeab1b7bdfa8e`。Bridge 包此前完整回归退出码 0，本轮未修改 Bridge 包。仍未执行 Git commit/push/tag 或生产部署。

本记录独立于 `docs/diagnostics/v0250-macos-owned-native-acceptance-2026-11-14.md`。两者分别证明 owned Bridge 原生生命周期和 file-only Connector 原生生命周期，不代表同一实例的两能力已全部组合验收。
