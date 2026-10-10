---
title: macOS 原生部署
description: macOS 原生部署 —— 仅限 Apple M 系列、原生 arm64、macOS 27 Golden Gate；Darwin FD 后端、getpeereid AF_UNIX Bridge、launchd 生命周期，无需 Docker 或 Rosetta。
---

自 v0.13.0 起，ServerFS 可以在 **运行 macOS 27 Golden Gate 的 Apple M 系列 Mac** 上作为 MCP **stdio** 服务原生运行。平台边界是精确的：仅限原生 **arm64** 执行（不支持 Rosetta、不支持 x86_64）、仅支持 macOS 27.x、仅支持 **本地 APFS** 工作目录。Intel Mac、Hackintosh、macOS 26 及更早版本、macOS 28 及更新版本都会被启动时的实测门控以 `NATIVE_PLATFORM_UNSUPPORTED` 拒绝——绝不静默执行。

运行链路：

```text
serverfs tunnel
  -> 官方固定版本 tunnel-client               （仅 darwin-arm64 资产）
       -> 消毒 supervisor
            -> serverfs serve                 （MCP stdio 子进程，实测 darwin 门控）
                 -> Darwin FD 后端             （纯 Python + 描述符相对 POSIX）
            -> file-ingress helper            （独立进程，私有 AF_UNIX socket）
       -> Agent Bridge（launchd 用户代理）     （com.ntlx.serverfs.agent-bridge）
            -> AF_UNIX + getpeereid           （0700 runtime 目录，不伪造 peer PID）
            -> 真实 provider CLI              （codex / claude / qoder；仅原生 arm64）
```

没有 Docker、虚拟机、Rosetta 依赖，也没有编译版 macOS 内核包：Darwin 后端是纯 Python 的描述符相对 POSIX 原语，加上三个极窄的 libc 绑定（`fcopyfile`、`getpeereid`、`confstr`）。不存在 `serverfs-macos-native` wheel。

## 文件系统语义

- 全程使用 `O_NOFOLLOW` 的描述符相对遍历；symlink 与 Linux 一样直接拒绝。
- 原子发布（同目录临时文件 → fsync → `renameat`），revision/CAS 与硬链接保护与 Linux 契约完全一致。
- 替换文件时通过 `fcopyfile(COPYFILE_METADATA)` 将原文件的 mode、用户 xattr 与扩展 ACL 携带到新 inode，任何丢失都会在发布前失败。
- `search_text` 不使用 ripgrep 或 `/proc`：FD 安全目录遍历 → 有界读取 → 字面 UTF-8 扫描，与 Linux/Windows 搜索契约一致。

## launchd 上的 Agent Bridge

Bridge 以每用户 LaunchAgent（`com.ntlx.serverfs.agent-bridge`）运行，使用现代 `launchctl bootstrap/kickstart/bootout` 管理：

```bash
serverfs agent-bridge configure --config serverfs.toml --env-file .env
serverfs agent-bridge install --bridge-config "$HOME/Library/Application Support/ServerFS/agent-bridge/bridge.json"
serverfs agent-bridge start | stop | status | uninstall
```

私有 `bridge.json` 位于所有暴露 workdir 之外，并可能包含 Jev/proxy 私有材料；它是**派生/私有状态，不是第二份用户配置**。Workdir、runtime 与生命周期 policy 仍只由 `serverfs.toml` 定义。`configure` 从这些 policy 与 `.env` 中限定的 Agent/Jev/proxy 私有值创建或刷新 0600 私有配置。仅修改 TOML policy 时，执行 `serverfs agent-bridge restart --config serverfs.toml` 会保留现有私有材料；若 Jev/proxy 私有值也发生变化，再加 `--env-file .env`。`serverfs doctor --config serverfs.toml` 会在发生漂移时报告 FAIL，新启动的原生 `serve`/tunnel 也会拒绝在陈旧 Bridge policy 下运行。

生成的 plist 只包含路径——绝不包含机密。launchd 是服务管理器而非包含内核：不声称 Job Object 等价语义，保留 recovery guard，provider 状态未知时 workspace-write 失败关闭。

## 安装

```bash
uv sync
uv sync --project agent_bridge
serverfs bootstrap tunnel-client        # 仅 darwin-arm64 资产
serverfs doctor --config serverfs.toml
serverfs serve --config serverfs.toml   # 或通过 tunnel 链路
```

数据位于 `~/Library/Application Support/ServerFS`（可用 `SERVERFS_DATA_HOME` 覆盖）；socket 位于 OS 提供的每用户 runtime 目录（`_CS_DARWIN_USER_TEMP_DIR`，经验证——绝不信任继承的 `$TMPDIR`）。ServerFS 绝不绕过 macOS TCC 隐私控制：受保护路径会干净失败，`serverfs doctor` 会指出可能的 TCC 拒绝。

## 验收证据

`docs/phase-0-macos27-arm64-capability-probe-2026-10.md`、`docs/phase-macos-native-acceptance-2026-10.md`、`docs/phase-macos-agent-acceptance-2026-10.md`、`docs/phase-macos-live-chatgpt-e2e-2026-10.md`。
