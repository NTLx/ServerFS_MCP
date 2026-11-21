# ServerFS MCP v0.25.0 — macOS owned 四组件原生组合验收（2026-11-14）

> **历史验收边界**：原始四组件合成 Connector 验收及第 4 节当日待办保留。后续 macOS Shared Core 与同 provider 命名 ID 有独立验收记录；跨 OS 私有协议不要求统一，遵循[原生优先 ADR](../architecture/v0.25.0-native-platform-boundaries.md)。当前未关闭门禁以[发布清单](../v0.25.0-release-checklist-2026-11-21.md)为准，本报告不证明真实双公网 Connector 或 Provider 执行。

**结论：** 在独立随机实例中，`owned` Core + 独立 Agent Bridge + Cloudflare Connector + OpenAI Connector 已通过真实 macOS SMAppService 注册/注销、独立进程生命周期和本机 MCP 调用。两个 Connector 是 `/bin/sleep` **无网络模拟程序**；**不代表双真实公网 Tunnel 或 Shared Bridge v2 通过**。本轮没有 Git 提交、推送、生产服务切换。

## 本轮先发现并修复的真实问题

- 首次在 Mac 实机运行 `deployment/macos/v0250_owned_connector_launcher_diagnostic.py --execute`，退出码 **1**，`result=NOT_ACCEPTED`，`signed_bridge_launcher_ready=false`。
- 签名 ServiceLauncher 报 `owned OpenAI argv mismatch at index 4`，即 control-plane API key 的 `file:<path>` 参数比较失败。Swift Foundation 的 `standardizedFileURL.path` 在测试机器将 `/private/var/...` 规范化为 `/var/...`，两者指向相同测试文件但字面字符串不同。
- 精确修复在 `native/macos/service_manager/Sources/ServiceLauncher/main.swift`：`verifyOwnedConnector` 的 argv **字面匹配**改用 manifest 已提供的 `controlRaw`、`bearerRaw`，而 `requireAbsolute`、无符号链接、regular file、当前用户所有者与 `0600` 检查继续在规范化 URL 上执行。没有取消安全校验，也没有把错误忽略。
- 首次失败后的测试实例、临时目录被脚本清理，生产 PID 不变。诊断任务 `agt_83d11a9128371baf1e56c5f6`；路径规范化隔离复现 `agt_09aa4287f7036ed01946fac1`。

## 修复后真实执行证据

执行记录：`agt_d33ad8542d57576ab0f4246a`，macOS 27.2.1 / Apple Silicon。

| 门禁 | 真实结果 |
|---|---|
| ServiceManager/ServiceLauncher companion build、签名及签名验证 | **exit 0** |
| `v0250_owned_connector_launcher_diagnostic.py --execute` | **exit 0；PASS**，仅直接启动签名 Bridge Launcher，不注册 launchd |
| `v0250_owned_dual_connector_acceptance.py --execute` | **exit 0；PASS**，真实 SMAppService 的四组件组合 |
| 本地 Core→Bridge MCP | **4 次往返通过**；只查询随机不存在的任务，无 Agent 提交 |
| 服务独立性 | 通过 Connector 停止/重启与 Core/Bridge PID 连续性校验；活动 Connector 阻止 Core/Bridge 停止 |
| 收尾 | `test_instance_removed=true`、`retained_instance=null`、`retained_diagnostic_dir=null`、`errors=[]` |
| 生产保护 | 两次验收 `production_pids_unchanged=true`；未修改生产 OpenAI/Cloudflare/Core/Bridge |
| `git diff --check` | **exit 0** |

## 后续定向与全量回归

- 新增 `tests/test_darwin_v0250_owned_lifecycle.py` 参数化回归：分别在 Cloudflare/OpenAI Connector 已注册时，确认 Core、Core+Bridge 组合及独立 Bridge 的停止操作拒绝越级注销（`unregister` 调用为零）。
- 验证任务 `agt_c47da67c1cae45cedc634daf`：三个 macOS 定向测试文件 **exit 0**；主仓库 `ruff check .`、`ruff format --check .`（**579 个文件**）、完整 pytest 均 **exit 0**；独立 `agent_bridge` Ruff lint、format（**112 个文件**）、完整 pytest 均 **exit 0**；`git diff --check` **exit 0**。两套 quiet pytest 都没有可靠的 passed/skipped 汇总数，不推断总数。
- 最后仅同步了 `deployment/macos/README.md` 的当前支持边界及命名 owned CLI 使用说明，并给 Swift 原样路径对比补充解释性注释；这些文档/注释变更不改变已通过测试的业务行为。后置只读核对 `agt_0878b9db7df45ee9abdcc06e` 确认 README 命令与 argparse 一致、`git diff --check` **exit 0**、Swift ServiceLauncher `xcrun swiftc -typecheck` **exit 0**。
- 一次额外的临时构建复查因提出无法充分核对内容的工作区外命令授权而被取消（`agt_a4861f0cd007bc28e68edeb5`）；不以它充当新的构建证据。上述独立的真实签名构建与组合验收成功记录仍有效。

关键验收 JSON：

```json
{
  "result": "PASS",
  "real_smappservice": true,
  "owned_core_bridge_connector_rpc": true,
  "mcp_missing_task_roundtrips": 4,
  "fixture": "synthetic no-network /bin/sleep Connectors",
  "production_pids_unchanged": true,
  "test_instance_removed": true,
  "retained_instance": null,
  "retained_diagnostic_dir": null,
  "errors": []
}
```

## 尚未通过的门禁

1. 真正独立于生产的 Cloudflare/OpenAI 公网双 Connector：DNS/Tunnel ID/密钥、HTTP MCP/SSE/会话、断线恢复、认证及真实远程访问。不能使用生产隧道资源冒充隔离验收。
2. macOS 与 Windows/Linux 的 **Bridge v2** 共用协议落地：逐 Core HMAC/grants、nonce、工作目录与任务归属隔离、撤销/轮换、两个获授权 Core 共享同一个 Bridge。当前 Mac `shared` 必须继续 fail-closed。
3. 同一 provider 下多个不同 `connector_id` 的服务命名、manifest、资源隔离及原生负面测试。
4. 全项目版本发布前，在统一源码集成后重新运行 Swift、Python 主项目和独立 Bridge 包完整回归，以及 Windows/Linux/macOS 全拓扑验收和 Git 收尾。

本次证据更新此前 `v0250-macos-connector-native-acceptance-2026-11-14.md` 与 `v0250-macos-owned-native-acceptance-2026-11-14.md` 的“尚未组合验证”阶段限制；其原始审计记录保留。
