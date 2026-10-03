---
title: Windows 原生部署
description: v0.10 的 Windows 原生形态——预构建 Rust/NTFS 内核 wheel 之上的 MCP stdio 服务，无需 Docker 或 MSVC。
---

自 v0.10.0 起，ServerFS 可以在 **Windows 11 x64 + 本地 NTFS** 上以 MCP **stdio** 服务原生运行。没有任何端口监听：官方 OpenAI `tunnel-client` 主动连出到控制面，并通过子进程的 stdin/stdout 传输 MCP 帧，因此默认原生 profile 不存在 localhost 监听器。Linux Docker 部署保持不变，仍是 Linux 的受支持形态。

运行时链路：

```text
serverfs tunnel
  -> 官方 pinned tunnel-client            （负责控制面连接）
       -> 净化 supervisor                 （剥离 tunnel/proxy 环境变量）
            -> serverfs serve             （MCP stdio 子进程，看不到任何密钥）
                 -> serverfs-windows-native wheel（HANDLE 相对定位的 NTFS 内核）
```

Windows 用户只需 GitHub Release 上的两个预构建 wheel，完全不需要 Rust、Cargo、MSVC Build Tools 或 Windows SDK。内核 wheel 为 `cp312-abi3-win_amd64`（稳定 ABI，Python >= 3.12）。

## 安装

1. 安装 [uv](https://docs.astral.sh/uv/)（或任意 Python >= 3.12）。
2. 把两个 release wheel 装进 venv：

```powershell
uv venv --python 3.12
.venv\Scripts\activate
uv pip install serverfs_mcp-<ver>-py3-none-any.whl serverfs_windows_native-<ver>-cp312-abi3-win_amd64.whl
```

3. 把 `serverfs.toml.example` 复制为 `serverfs.toml` 并配置 workdir。路径属于运维者配置——Agent 永远只看 alias。`read_only = true` 是默认值，除非 workdir 显式开启，内核会拒绝一切变更。`serverfs.toml` 中不放任何密钥。
4. 运行健康报告，直到 exit 0：

```powershell
serverfs doctor --config serverfs.toml
```

`serverfs doctor` 会探测原生 backend 导入/版本、每个 workdir 的根打开、文件系统类别、reparse 拓扑、一次真实的按策略过滤列表、不做任何变更的写能力检查、tunnel-client 版本，以及项目托管 HTTP 代理的可达性——代理与 tunnel 凭据永不显示。Windows GA 仅限本地 NTFS：网络盘、FAT/exFAT、ReFS 以及一切无法测定类别的存储都会报告 `FAIL`（fail closed，绝不静默放行）。

## 连接

```powershell
serverfs bootstrap tunnel-client   # pinned 官方 release，双重 SHA-256 锚定
serverfs tunnel --config serverfs.toml `
  --tunnel-id tunnel_... --api-key-file C:\Users\you\.config\serverfs\api-key
```

bootstrap 把客户端存放在用户自有的 ServerFS 数据目录，绝不修改机器 PATH；其验证链先以 pinned digest 校验官方 `SHA256SUMS.txt`，再按 manifest 复算下载包。`serverfs tunnel` 自动发现 bootstrap 安装的客户端，把 tunnel-client 的 health 服务绑到临时 loopback 端口，控制面 key 只以 `file:` 引用传递（key 文件必须位于所有 workdir 之外），并在派生 ServerFS 子进程前剥离全部 `CONTROL_PLANE_*`、`TUNNEL_CLIENT_*`、`OPENAI_*`、`MCP_*`、`SERVERFS_PROXY_*` 与代理变量。代理配置与 Linux 共用 `.env` 中同样的四个 `SERVERFS_PROXY_*` 字段（仅支持 HTTP；不支持也不宣称 SOCKS）。

上游明确不支持多个 tunnel-client 实例共享同一个 tunnel ID——不要并行运行两份原生 profile。

## 在 Windows 上依然成立的性质

每一条通道——read、list、stat、find、search 与全部五个变更工具——行为都与 Linux 一致：handle 相对定位遍历、reparse point 永不被跟随、基于 revision 守护的原子发布、fail-closed 的元数据保全、各通道完全相同的策略过滤，以及零内容结构化日志。两个平台可见差异均有文档记录：符号链接拒绝在 Windows 上表现为 `REPARSE_POINT_NOT_ALLOWED`（Linux 为 `SYMLINK_NOT_ALLOWED`）；Windows 目录 revision 是否随子项移动取决于卷设置，因此 `delete_directory` 依赖物理空目录扫描。
