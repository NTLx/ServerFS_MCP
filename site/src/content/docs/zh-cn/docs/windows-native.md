---
title: Windows 原生部署
description: Windows 原生形态——预构建 Rust/NTFS 内核 wheel 之上的 MCP stdio 服务，无需 Docker 或 MSVC；v0.11 起支持 Codex、Claude、Qoder 的 Agent 委派。
---

自 v0.10.0 起，ServerFS 可以在 **Windows 11 x64 + 本地 NTFS** 上以 MCP **stdio** 服务原生运行。没有任何端口监听：官方 OpenAI `tunnel-client` 主动连出到控制面，并通过子进程的 stdin/stdout 传输 MCP 帧，因此默认原生 profile 不存在 localhost 监听器。Linux Docker 部署保持不变，仍是 Linux 的受支持形态。

v0.11（开发中）为该部署加入 **Agent 委派**：通过同一组十个 Agent 工具与 Named-Pipe Bridge 驱动 Codex、Claude Code 与 Qoder 三个运行时，并已通过真实 ChatGPT 隧道 E2E 验收。

运行时链路：

```text
serverfs tunnel
  -> 官方 pinned tunnel-client            （负责控制面连接）
       -> 净化 supervisor                 （剥离 tunnel/proxy 环境变量）
            -> serverfs serve             （MCP stdio 子进程，看不到任何密钥）
                 -> serverfs-windows-native wheel（HANDLE 相对定位的 NTFS 内核）
       -> Agent Bridge                    （独立 venv，按用户生命周期 lease）
            -> 真实 provider CLI          （codex / claude / qodercli）
```

Windows 用户只需 GitHub Release 上的预构建 wheel，完全不需要 Rust、Cargo、MSVC Build Tools 或 Windows SDK。内核 wheel 为 `cp312-abi3-win_amd64`（稳定 ABI，Python >= 3.12）。

## 安装

产品与 Agent Bridge 是**两个隔离虚拟环境中的两个独立发行包**，由冻结的进程边界连接：supervisor 通过 `SERVERFS_BRIDGE_PYTHON` 启动 Bridge。二者绝不会被装进同一个共享依赖图（实测：`serverfs-mcp` 冻结 `mcp==2.2.0`，而 pinned 的 Qoder SDK 声明 `mcp<2.0.0`）。

1. 安装 [uv](https://docs.astral.sh/uv/)（或任意 Python >= 3.12）。
2. 把 **ServerFS 环境**（product + native wheel）与 **Agent Bridge 环境**（bridge wheel）分别装进两个 venv：

```powershell
uv venv --python 3.12 .venv-serverfs
uv pip install --python .venv-serverfs\Scripts\python.exe serverfs_mcp-<ver>-py3-none-any.whl serverfs_windows_native-<ver>-cp312-abi3-win_amd64.whl

uv venv --python 3.12 .venv-bridge
uv pip install --python .venv-bridge\Scripts\python.exe serverfs_agent_bridge-<ver>-py3-none-any.whl
```

3. 在 `serverfs.toml` 旁的 `.env` 中设置 `SERVERFS_BRIDGE_PYTHON=C:\path\to\.venv-bridge\Scripts\python.exe`，把 supervisor 指向 Bridge venv（tunnel launcher 会把它注入 serverfs 进程环境）。Bridge 环境永远不需要 product 包，product 环境也永远不需要 Bridge 包。
4. 把 `serverfs.toml.example` 复制为 `serverfs.toml` 并配置 workdir。路径是运营者配置——agent 只能看到别名。`read_only = true` 是默认值，workdir 未显式开启时内核拒绝变更。`serverfs.toml` 中不放任何密钥。Agent 委派**默认关闭**：只有加入 `[agent]` 段并设 `enabled = true`、且 workdir 配置了 `agent_mode`/`agent_runtimes` 策略后才会启用；没有 `[agent]` 段的配置行为与 v0.10 完全一致。
5. 运行健康报告，直到退出码为 0：

```powershell
.venv-serverfs\Scripts\serverfs.exe doctor --config serverfs.toml
```

`serverfs doctor` 会探测 native 后端导入/版本、每个 workdir 根目录打开、文件系统类别、reparse 拓扑、真实策略过滤列表、非变更写能力检查、tunnel-client 版本以及项目托管的 HTTP proxy 可达性——proxy 与 tunnel 凭证永不显示。Windows GA 仅限本地 NTFS：网络共享、FAT/exFAT、ReFS 以及任何无法测量类别的存储都会报 `FAIL`（fail closed，绝不静默放行）。

启用 `[agent]` 后，doctor 还会以只读方式（绝不启动 Bridge 或 provider）额外报告：每个 workdir 的 Agent 策略、各 runtime 的启用状态与 proxy 路由、Agent data home、已存在对象的 private-state 安全性、Agent proxy endpoint 的接线与可达性（endpoint 本身永不显示）、用户身份推导，以及 Bridge 包可被配置的 `SERVERFS_BRIDGE_PYTHON` 解释器导入。

## Windows 上的 Agent 运行时

每个 runtime 都是一个真实的 provider 进程；能力按 runtime 逐一记录，不做统一宣传：

| Runtime | 模型发现 | 实时转向（live steering） | proxy 路由 |
|---|---|---|---|
| Codex | 目录 + 请求级覆盖 | 支持（其自有 loopback 控制通道按策略绕开 proxy） | 无直连 provider 路由的主机使用 `use_proxy = true` |
| Claude Code | 不支持（`model_discovery: unsupported`） | 不支持（`live_steer = false`） | `use_proxy = true` 时 provider 出站经注入的 proxy（实测：外部 CONNECT 归因于 claude 子进程，0 loopback）；直连主机用 `false` |
| Qoder | 目录（价格状态实时读取，永不硬编码） | 不支持（`live_steer = false`） | 与 Claude 相同的注入模型 |

Agent 出站 proxy 是运营者通过 `SERVERFS_AGENT_PROXY_URL` 提供的**无凭证 HTTP proxy**；带认证的上游 proxy 不在原生支持范围内。Claude/Qoder 的 provider 子进程消费注入的 `HTTPS_PROXY`/`NO_PROXY` overlay；Codex 的 SDK↔CLI 控制流量走 stdio，其 loopback 端点按策略豁免。

## 连接引导

```powershell
.venv-serverfs\Scripts\serverfs.exe bootstrap tunnel-client   # 官方 pinned 版本，SHA-256 双重锚定
.venv-serverfs\Scripts\serverfs.exe tunnel --config serverfs.toml `
  --tunnel-id tunnel_... --api-key-file C:\Users\you\.config\serverfs\api-key
```

bootstrap 把 client 存到用户所有的 ServerFS 数据目录下，绝不修改机器 PATH；其验证链先锚定官方 `SHA256SUMS.txt` 摘要，再把下载的档案重新哈希比对。`serverfs tunnel` 自动发现已引导的 client，把 tunnel-client 的健康服务绑定到临时 loopback 端口，只以 `file:` 引用传递控制面密钥（密钥文件必须位于所有 workdir 之外），并在 spawn ServerFS 子进程前剥离全部 `CONTROL_PLANE_*`、`TUNNEL_CLIENT_*`、`OPENAI_*`、`MCP_*`、`SERVERFS_PROXY_*` 与 proxy 变量。proxy 配置与 Linux 共用 `.env` 中同样的四个 `SERVERFS_PROXY_*` 字段（仅 HTTP；不支持也不宣称 SOCKS）。

多个 tunnel-client 实例共用一个 tunnel ID 属于上游不支持形态——不要把原生 profile 跑两份。

## Windows 上保持不变的契约

所有通道——读、列、stat、find、search 与全部五个变更工具——行为与 Linux 一致：句柄相对遍历且 reparse point 永不跟随、revision 保护的原子发布、fail-closed 的元数据保留、跨通道一致的策略过滤、内容无关的结构化日志。两个平台可见差异已文档化：符号链接拒绝在 Windows 上报 `REPARSE_POINT_NOT_ALLOWED`（Linux 为 `SYMLINK_NOT_ALLOWED`）；Windows 目录 revision 是否随子项移动取决于卷设置，因此 `delete_directory` 依赖物理空扫描。Windows 的 `revision` 仍是元数据推导的乐观并发令牌——设计上存在同 tick、同大小的盲写窗口，ServerFS 不宣称任意进程的 compare-and-swap。
