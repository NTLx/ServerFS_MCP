---
title: macOS 原生部署
description: ServerFS v0.14.0 macOS 原生部署 —— Apple M 系列、macOS 27、Darwin FD 安全边界，以及基于 Apple SMAppService 的 Tunnel / Agent Bridge 持久化。
---

当前项目稳定版本为 **v0.14.0**；**v0.13.2** 引入的 macOS 原生支持形态在 v0.14.0 中保持不变。ServerFS 可在 **运行 macOS 27 Golden Gate 的 Apple M 系列 Mac** 上原生运行。支持边界保持严格：仅限原生 **arm64**，不支持 Rosetta/x86_64，仅支持 macOS 27.x 与**本地 APFS** workdir。Intel Mac、Hackintosh、macOS 26 及更早版本、macOS 28 及更新版本都会以 `NATIVE_PLATFORM_UNSUPPORTED` 失败关闭。

v0.13.2 保持 Darwin 文件系统和 Agent 协议实现为 Python，只将旧的手工 LaunchAgent 生命周期替换为 Apple 当前的 **ServiceManagement / `SMAppService`** 模式。一个极小的无界面 `ServerFSServiceManager.app` 负责两个每用户 LaunchAgent 的注册与状态：

```text
SMAppService / launchd
  +-- com.ntlx.serverfs.tunnel
  |    -> serverfs tunnel
  |         -> 固定版本 tunnel-client（darwin-arm64）
  |              -> sanitizer supervisor
  |                   -> serverfs serve
  |                        -> Darwin FD 后端
  |                        -> 可选 file-ingress helper
  |
  +-- com.ntlx.serverfs.bridge
       -> Agent Bridge
            -> 私有 AF_UNIX + getpeereid
            -> codex / claude / qoder
```

两个服务都不再依赖终端、Codex/Claude/ChatGPT 会话的生命周期。用户登录后的持久化由 `SMAppService` 管理；重启/重新登录后的 live 恢复验证明确留待 v0.13.2 之后。ServerFS **不**安装 LaunchDaemon，也不要求登录前运行。

没有 Docker、虚拟机、Rosetta 依赖或编译版 macOS 文件系统内核包。Darwin 后端仍是纯 Python 的描述符相对 POSIX 原语，加上极窄的 libc 绑定（`fcopyfile`、`getpeereid`、`confstr`）；不存在 `serverfs-macos-native` wheel。

## 文件系统语义

- 使用 `O_NOFOLLOW` 的描述符相对遍历，symlink 失败关闭。
- 原子发布（同目录临时文件 → fsync → `renameat`），保留 revision/CAS 与硬链接保护。
- 替换写入通过 `fcopyfile(COPYFILE_METADATA)` 保留 mode、用户 xattr 与扩展 ACL，否则在发布前失败。
- `search_text` 使用 FD 安全遍历、有界读取与字面 UTF-8 扫描，不依赖 `/proc` 或环境中的 shell 工具。

## 安装持久服务

先安装标准 Python 环境和官方 tunnel-client：

```bash
uv sync --frozen
uv sync --project agent_bridge --frozen
serverfs bootstrap tunnel-client   # 默认使用 ServerFS 实测的 v0.0.16
serverfs doctor --config serverfs.toml

# 如需使用其他官方版本，需同时提供版本号和该版本官方 SHA256SUMS.txt 的 SHA-256：
# serverfs bootstrap tunnel-client --version vX.Y.Z --manifest-sha256 <sha256>
```

然后安装持久化的用户服务：

```bash
serverfs service install --config serverfs.toml --env-file .env
serverfs service status
```

安装过程会使用 Apple 命令行工具在本机构建极小的 Swift companion，验证其代码签名与内嵌 LaunchAgent plist，将其安装到 ServerFS 私有 Application Support 目录，生成 0600 的生命周期私有状态，并通过 `SMAppService` 注册服务。

正常生命周期命令为：

```bash
serverfs service status
serverfs service start
serverfs service restart
serverfs service stop
serverfs service uninstall
```

如果 macOS 返回 `requires-approval`，ServerFS 会尊重用户授权状态，不绕过系统设置。可直接打开对应设置页：

```bash
serverfs service open-settings
```

随后在 **系统设置 > 通用 > 登录项** 中批准 ServerFS 后台项目。

## Agent Bridge policy

`serverfs.toml` 仍是 Agent workdir/runtime policy 的唯一用户配置来源。所有暴露 workdir 之外的私有 `bridge.json` 仍是派生状态，只保存 Bridge 所需的限定 Jev/proxy 私有材料。

```bash
serverfs agent-bridge configure --config serverfs.toml --env-file .env
serverfs agent-bridge restart --config serverfs.toml
# 若 Jev/proxy 私有值也变化：
serverfs agent-bridge restart --config serverfs.toml --env-file .env
```

v0.13.2 已淘汰 `serverfs agent-bridge install`。从 v0.13.0/v0.13.1 升级时，会检测旧的 `~/Library/LaunchAgents/com.ntlx.serverfs.agent-bridge.plist`，只在交接点停止旧 job，注册 app-associated 替代服务，健康检查通过后才删除旧 plist；注册失败时保留回滚路径。安装前也会检测旧版手工 tunnel，绝不会静默启动第二个 control-plane client。

受管 Tunnel 还会持有每用户实例锁，并将 launchd 的终止信号转发给 tunnel-client，避免遗留孤儿 supervisor/serve/file-ingress 进程树。

## 数据与安全边界

默认数据目录为 `~/Library/Application Support/ServerFS`；socket 位于 OS 提供的每用户 runtime 目录（`_CS_DARWIN_USER_TEMP_DIR`）。服务 manifest、私有环境映射与日志均位于暴露 workdir 之外并使用私有权限。LaunchAgent plist 不携带 provider 或 tunnel 凭据。

ServerFS 绝不绕过 macOS TCC 隐私控制。受保护路径会干净失败；`serverfs doctor` 会报告平台、policy、Service Manager 有效性、注册/授权状态、进程健康、legacy 残留与 unmanaged tunnel 检测结果。

## 验收证据

参见 `docs/phase-0-macos27-arm64-capability-probe-2026-10.md`、`docs/phase-v0.13.2-servicemanagement-probe-2026-10.md`、v0.13 系列 macOS 验收记录，以及 v0.13.2 开发/验收记录。
