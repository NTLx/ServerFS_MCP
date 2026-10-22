# ServerFS MCP

**简体中文** · [English](README_EN.md)

[![Release](https://img.shields.io/github/v/release/NTLx/ServerFS_MCP)](https://github.com/NTLx/ServerFS_MCP/releases/latest)
[![Linux CI](https://github.com/NTLx/ServerFS_MCP/actions/workflows/container.yml/badge.svg)](https://github.com/NTLx/ServerFS_MCP/actions/workflows/container.yml)
[![Windows CI](https://github.com/NTLx/ServerFS_MCP/actions/workflows/windows-native.yml/badge.svg)](https://github.com/NTLx/ServerFS_MCP/actions/workflows/windows-native.yml)
[![macOS CI](https://github.com/NTLx/ServerFS_MCP/actions/workflows/macos-native.yml/badge.svg)](https://github.com/NTLx/ServerFS_MCP/actions/workflows/macos-native.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**一个安全、标准优先、按范围授权的 MCP 文件系统服务器，并可选支持原生 AI Agent 委托。**

只暴露你明确选择的目录，并默认保持只读。仅在需要的位置按需开启受限文件修改、基于 revision 的并发保护、有限制的二进制传输，以及结构化的 Codex / Claude Code / Qoder 原生 runtime 或运维配置 ACP Agent 委托能力。

**当前稳定版本：v0.19.0**

**v0.21.0 实现已验收，是 release-ready candidate，尚未发布。** v0.19.0 仍是稳定已发布版本；v0.20.0 是已验收但未发布的实现基线。v0.21 增加 secret-safe 内部观测契约、有界的进程内 Bridge task/queue/lease/recovery 指标，以及仅限原生 macOS/Windows、显式选择的脱敏 common status projection；不增加 exporter、网络监听、MCP tool、telemetry egress 或 authority。默认 `service status` 输出不变；Linux Compose 不提供原生 service status projection。部署中的 Linux v0.21 runtime health 与 macOS/Windows RSS 仍**尚未验证**。v0.21.0 尚无 tag 或 GitHub Release；详见 [Phase G 验收记录](docs/v0.21.0-release-acceptance-2026-10-22.md)及 [Phase F 原生平台证据](docs/v0.21.0-phase-f-native-acceptance-2026-10-22.md)。

v0.19.0 在 Agent Bridge 中加入通用 ACP v1 profile，与原生 Codex、Claude Code、Qoder runtime 并存。ACP profile 由运维人员配置绝对可执行文件路径和固定 argv；任务调用不能覆盖 command、argv 或 env，且 ACP runtime 要求 `workspace-write`。ServerFS 不会从 ACP Registry 自动安装或信任 Agent，Provider authentication 仍由 Provider 自己管理。模型发现/覆盖、elicitation、client filesystem 和 terminal 均不支持；能力只按实测结果保守报告。OpenCode 2.1.2 使用 `opencode/glm-5.3-flash-free` 已通过 Linux、macOS、Windows 的真实端到端验收，覆盖 prompt、持久 session continuation，以及 active-tool cancellation/cleanup。

[项目网站](https://ntlx.github.io/ServerFS_MCP/) ·
[文档](https://ntlx.github.io/ServerFS_MCP/docs/) ·
[快速开始](https://ntlx.github.io/ServerFS_MCP/docs/getting-started/) ·
[安全模型](https://ntlx.github.io/ServerFS_MCP/docs/security/) ·
[静态 API Key](https://ntlx.github.io/ServerFS_MCP/docs/api-key/) ·
[OAuth 2.1](https://ntlx.github.io/ServerFS_MCP/docs/oauth/) ·
[Releases](https://github.com/NTLx/ServerFS_MCP/releases)

---

## 为什么选择 ServerFS

给 AI Agent 开放整个文件系统或 Shell 很容易，真正困难的是让它**只获得完成当前任务所必需的能力**。

ServerFS 围绕这一更窄、更明确的安全边界设计：

- **显式 Workdir** —— 只有预先配置的目录可见，对外使用别名而不是主机真实路径。
- **默认只读** —— 写权限按 Workdir 单独显式开启，而不是全局默认开放。
- **受限文件操作** —— 不提供 Shell、通用命令执行器、递归删除或无保护覆盖。
- **并发安全的修改** —— 破坏性操作使用不透明 revision；文本修改采用精确匹配并原子提交。
- **纵深防御** —— 路径约束、符号链接拒绝、凭据路径拒绝规则、有界 I/O、审计事件，以及平台原生的文件系统安全机制。
- **可选能力保持可选** —— 二进制传输、ChatGPT 文件输入、Agent 委托和 Jev 建议能力均可独立启用。
- **Linux、Windows、macOS** —— 对外保持一致的能力模型，同时使用各平台原生的约束与部署边界。

## 支持的部署方式

| 平台 | 支持形态 | 文件系统边界 | Agent 委托 |
| --- | --- | --- | --- |
| **Linux** | Docker Compose，支持 `linux/amd64` 与 `linux/arm64` | Bind mount Workdir；容器默认只读 | 原生 Codex、Claude Code、Qoder；可配置 ACP v1 profiles |
| **Windows** | Windows 11 x64、本地 NTFS、原生 stdio；自 v0.14.0 起支持当前用户 Task Scheduler 持久化 | MCP stdio 上的原生 HANDLE-relative Rust 内核 | 原生 Codex、Claude Code、Qoder；可配置 ACP v1 profiles |
| **macOS** | Apple M 系列、原生 arm64、macOS 27 Golden Gate | MCP stdio 上基于 descriptor-relative 的 Darwin 原生后端 | 原生 Codex、Claude Code、Qoder；可配置 ACP v1 profiles |

macOS v0.13 的支持边界有意保持严格：目前不声明支持 Intel Mac、Rosetta/x86_64、macOS 26 及更早版本，以及 macOS 28+。**v0.13.2** 使用 Apple 原生 `SMAppService` 为 Tunnel 与 Agent Bridge 提供持久化，使托管服务不再依赖终端或会话生命周期。`SMAppService` 负责当前用户登录后的持久化；真实重启/重新登录恢复验证未纳入 v0.13.2 的发布验收范围。

**v0.14.0** 在不引入管理员权限要求的前提下，为 Windows 增加持久化的外层生命周期管理。Windows Native 使用当前用户、最小权限的 Task Scheduler 2.0 任务托管无窗口 watchdog；watchdog 负责恢复 managed runner，runner 再负责恢复 Tunnel generation。登录触发器与无限期每分钟 heartbeat 共同重建外层 owner，同时通过 `IgnoreNew` 保证只保留一个健康实例。官方运维入口为 `serverfs service install/start/stop/restart/status/uninstall`；安装、运行、恢复和维护始终保持非提权、当前用户范围。发布验收覆盖 Tunnel、runner、Task Scheduler 层恢复、主动停止语义、显式启动恢复、Agent Bridge 恢复、无窗口运行以及单一 Tunnel ownership；物理重启/重新登录未包含在该版本验收中。

原生平台部署请参考：

- [Windows Native](https://ntlx.github.io/ServerFS_MCP/docs/windows-native/)
- [macOS Native](https://ntlx.github.io/ServerFS_MCP/docs/macos-native/)

## Agent Skills

仓库现已提供面向任务的 [ServerFS Agent Skills](skills/README.md)，覆盖安装、配置、升级、诊断、OpenAI Tunnel、HTTP 认证以及 Agent Bridge/runtime 运维。这些 Skills 是建立在现有 CLI、配置模板、Compose/部署文件和产品文档之上的轻量工作流层，而不是另一套配置系统。

仓库根目录同时提供仅包含 Skills 的可移植 [`plugin.json`](plugin.json)。从 v0.18.0 开始，稳定标签会构建一个随版本命名的 GitHub Release 资产；v0.19.0 资产为 `serverfs-plugin-v0.19.0.zip`，其中包含 7 个运行时 Skill 及其打包引用。该 Plugin 不包含 ServerFS MCP URL、凭据或任何 Provider 私有配置。

## 快速开始

最短部署路径是 Linux Docker。

### 前置要求

- Docker + Docker Compose
- 至少一个希望暴露给 MCP 的目录

OpenAI Secure MCP Tunnel 是可选组件，仅在需要通过 ChatGPT Tunnel 接入时使用。

### 1. 克隆并配置

```bash
git clone https://github.com/NTLx/ServerFS_MCP.git
cd ServerFS_MCP
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，至少配置一个 Workdir：

```env
WORKDIR_01_ALIAS=projects
WORKDIR_01_PATH=/srv/projects
WORKDIR_01_DESCRIPTION="Projects"
WORKDIR_01_READ_ONLY=true
```

### 2. 启动标准 Streamable HTTP 端点

```bash
docker compose pull
docker compose -f compose.yml -f compose.http.yml up -d serverfs-mcp
docker compose -f compose.yml -f compose.http.yml ps
```

通用 HTTP override 默认将 ServerFS 发布到 `127.0.0.1:8000/mcp`，且不会启动 OpenAI Tunnel。任何非 loopback 或公网部署前，都应配置 `SERVERFS_HTTP_ALLOWED_HOSTS`，并在 ServerFS 前放置带认证的 TLS ingress。

如果要通过现有 OpenAI Secure MCP Tunnel 接入 ChatGPT，请配置 `CONTROL_PLANE_TUNNEL_ID` 和 `CONTROL_PLANE_API_KEY`，并使用基础 Compose 部署。Agent 委托、二进制传输、文件输入、代理路由或生产环境加固请继续阅读 [快速开始](https://ntlx.github.io/ServerFS_MCP/docs/getting-started/) 与 [配置](https://ntlx.github.io/ServerFS_MCP/docs/configuration/) 文档。

### 可选：为远程 HTTP 启用静态 API Key

v0.17.0 增加了轻量的内置认证模式，适用于不需要 OAuth 身份流程的场景。在 `.env` 中设置一个高熵密钥：

```env
SERVERFS_HTTP_API_KEY=<long-random-secret>
```

客户端优先使用 `Authorization: Bearer <key>`。同时支持钉钉等场景常见的一行式 URL，例如 `https://mcp.example.com/mcp?key=<key>`，但 URL 中的凭据可能被反向代理或基础设施日志记录。公网端点仍必须使用 HTTPS；API Key 模式与 `SERVERFS_OAUTH_ENABLED=true` 互斥。详见 [Static API Key](https://ntlx.github.io/ServerFS_MCP/docs/api-key/)。

### 可选：为远程 HTTP 启用 OAuth 2.1

v0.16.0 为通用远程 Streamable HTTP 增加了可选 OAuth 2.1 部署。OAuth 默认**关闭**，仅在 `SERVERFS_OAUTH_ENABLED=true` 且引入 `compose.oauth.yml` 时生效，不改变 Windows/macOS 原生 stdio 行为。

```bash
mkdir -p .serverfs-auth-state
chmod 700 .serverfs-auth-state

docker compose --env-file .env \
  -f compose.yml \
  -f compose.http.yml \
  -f compose.oauth.yml \
  up -d
```

推荐的公网架构是在反向代理后使用彼此独立的 HTTPS MCP origin 与 Authorization Server origin，并仅允许 `serverfs-auth` 承担 Provider/CIMD 出站访问。当前支持 GitHub、钉钉、飞书/Lark 和微信开放平台身份提供方。回调地址、稳定主体准入、CIMD/DCR、资源绑定、备份/吊销、代理配置以及 OpenAI Tunnel 当前的 OAuth 限制，详见 [OAuth 2.1](https://ntlx.github.io/ServerFS_MCP/docs/oauth/)。

## 工作原理

```text
Claude Code / Codex / Qoder / MCP hosts
             |               |
           stdio      Streamable HTTP
             |               |
             +-------+-------+
                     v
              +-------------+
              | ServerFS MCP |------> configured workdirs
              | Core        |------> optional host Agent Bridge
              +-------------+                |
                     ^                       +--> Codex
                     |                       +--> Claude Code
          OpenAI Secure MCP Tunnel           +--> Qoder
                     |
                  ChatGPT
```

MCP Core 与宿主平台解耦。标准 MCP 客户端使用 stdio 或 Streamable HTTP；OpenAI Secure MCP Tunnel 保持为可选的、带认证的 ChatGPT ingress 适配器。Linux 上的通用 HTTP 需要显式开启，并默认仅监听 loopback；原有 Tunnel 部署方式继续受支持。v0.17 在 Streamable HTTP 外增加可选的单密钥 API Key 边界；v0.16 则提供独立的 `serverfs-auth` OAuth 2.1 Broker，用于需要身份感知的部署。这两种认证方式都不会改变既有 Workdir 授权模型或原生 stdio。可选 file-ingress sidecar 仍维持独立的出站职责。

Windows 与 macOS 上，ServerFS 通过 stdio 原生运行，Agent Bridge 保持独立 Host Process 边界。自 v0.14.0 起，Windows 使用当前用户 Task Scheduler 作为持久化 OS owner，并保持托管进程树无窗口运行，不依赖 SCM/LocalSystem 服务或 UAC 提权。macOS v0.13.2+ 使用一个轻量 headless ServiceManagement companion，通过 `SMAppService` 注册当前用户范围的持久 Tunnel 与 Agent Bridge，不需要 root LaunchDaemon，也不需要手工安装 LaunchAgent。

完整部署与信任边界模型见 [Architecture](https://ntlx.github.io/ServerFS_MCP/docs/architecture/)。

## 能力边界

ServerFS 按层暴露能力，而不是一次性开启所有权限。

| 能力层 | 工具数 | 增加的能力 |
| --- | ---: | --- |
| 文件系统 | 11 | 6 个只读工具 + 5 个受控修改工具 |
| 文件系统 + 二进制 | 13 | 有大小限制的整文件下载/上传 |
| 文件系统 + Agent | 21 | 10 个结构化 Agent Bridge 工具 |
| 完整能力 | 23 | 文件系统 + 二进制 + Agent |

### 文件系统工具

**所有 Workdir 可用的读取工具：** `list_workdirs`、`list_directory`、`find_files`、`search_text`、`read_text_file`、`stat_file`

**仅 Read-Write Workdir 可用：** `create_text_file`、`edit_text_file`、`delete_file`、`create_directory`、`delete_directory`

**可选二进制传输：** `download_binary_file`、`upload_binary_file`

Agent 工具用于提交和观察结构化 Provider Task，**不是** Shell 或任意命令执行 API。详见 [Agent Bridge](https://ntlx.github.io/ServerFS_MCP/docs/agent-bridge/)。

## 安全模型

ServerFS 假设文件内容均为不可信数据，并通过多个独立防线实施安全约束，而不是依赖单一权限检查。

关键特性包括：

- Workdir confinement，且 MCP 返回结果不暴露主机真实路径；
- 默认只读，并由部署层/文件系统层进一步强化；
- 基于 descriptor 或 HANDLE 的相对路径遍历，并防御 symlink/reparse point；
- 破坏性操作使用 revision guard，并原子发布修改结果；
- 替换写入时保留文件元数据；
- 保留内部名称与内置凭据路径拒绝规则；
- 对读取、写入、搜索、二进制传输、任务结果及交互生命周期设置明确上限；
- 不提供递归删除、force、通用 `write_file`、Shell 或任意命令执行器；
- 使用隔离的 file ingress，而不是给 MCP 主服务开放通用下载出站能力；
- Agent runtime allowlist、writer lease、恢复保护、显式 Provider approval，以及仅提供建议的 Jev 集成。

实际信任边界和安全契约请以 [Security Model](https://ntlx.github.io/ServerFS_MCP/docs/security/) 与 [Architecture](https://ntlx.github.io/ServerFS_MCP/docs/architecture/) 为准。

## 可选 Agent Bridge

ServerFS 可通过独立的 Host-side Agent Bridge，将受限任务委托给原生 **Codex**、**Claude Code**、**Qoder** Runtime，以及公开 runtime ID 形如 `acp-<profile>` 的通用 ACP v1 profiles。ACP profile 由运维人员配置绝对可执行文件与固定 argv；这不是新的原生 Provider 集成。

Agent Bridge 提供结构化任务提交、状态/事件、取消、审批/提问、Provider 支持时的模型发现、请求级模型选择、结果 spool、幂等重试以及恢复语义。Provider 能力按 Runtime 实际上报，不假设不同 Provider 具有完全一致的功能。ACP runtime 不支持模型发现或覆盖、elicitation、ACP client filesystem 或 terminal；其余 runtime capabilities 也仅按实测结果保守报告。

Jev 集成为可选且仅提供建议：它可以提供任务 preflight、Runtime/模型建议和审批上下文，但不会扩展权限，也不会自动覆盖调用方选择的 Runtime、模型或审批决定。

- [Agent Bridge](https://ntlx.github.io/ServerFS_MCP/docs/agent-bridge/)
- [Jev Advisors](https://ntlx.github.io/ServerFS_MCP/docs/jev-advisors/)

## 二进制传输与 ChatGPT 文件输入

二进制传输是独立的可选能力，并有明确字节上限。覆盖已有文件时仍需 revision guard，且目标必须位于 Read-Write Workdir。

ChatGPT 文件参数通过可选的隔离 ingress helper 处理，而不是让 MCP 主服务获得不受限的互联网访问能力。

支持来源、限制和网络边界见 [Binary Transfer](https://ntlx.github.io/ServerFS_MCP/docs/binary-transfer/)。

## 文档

文档站是面向用户和运维人员的权威参考；README 有意保持在项目入口层级。

| 主题 | 文档 |
| --- | --- |
| 项目概览 | [ServerFS MCP docs](https://ntlx.github.io/ServerFS_MCP/docs/) |
| 首次部署 | [Getting Started](https://ntlx.github.io/ServerFS_MCP/docs/getting-started/) |
| Workdir 与策略 | [Configuration](https://ntlx.github.io/ServerFS_MCP/docs/configuration/) |
| 架构与信任边界 | [Architecture](https://ntlx.github.io/ServerFS_MCP/docs/architecture/) |
| 安全契约 | [Security Model](https://ntlx.github.io/ServerFS_MCP/docs/security/) |
| 远程 HTTP 静态 API Key | [Static API Key](https://ntlx.github.io/ServerFS_MCP/docs/api-key/) |
| 远程 HTTP OAuth 2.1 | [OAuth 2.1](https://ntlx.github.io/ServerFS_MCP/docs/oauth/) |
| 二进制传输 / 文件输入 | [Binary Transfer](https://ntlx.github.io/ServerFS_MCP/docs/binary-transfer/) |
| Agent 委托 | [Agent Bridge](https://ntlx.github.io/ServerFS_MCP/docs/agent-bridge/) |
| 可选 Jev 建议 | [Jev Advisors](https://ntlx.github.io/ServerFS_MCP/docs/jev-advisors/) |
| OpenAI Tunnel 故障诊断 | [Tunnel Diagnostics](https://ntlx.github.io/ServerFS_MCP/docs/tunnel-diagnostics/) |
| Host 只读状态发布 | [Host Observability](https://ntlx.github.io/ServerFS_MCP/docs/host-observability/) |
| 受限网络 Linux ARM64 | [Restricted-network ARM64](https://ntlx.github.io/ServerFS_MCP/docs/restricted-network-arm64/) |
| Windows 原生部署 | [Windows Native](https://ntlx.github.io/ServerFS_MCP/docs/windows-native/) |
| macOS 原生部署 | [macOS Native](https://ntlx.github.io/ServerFS_MCP/docs/macos-native/) |

版本历史和升级专用说明应放在 [GitHub Releases](https://github.com/NTLx/ServerFS_MCP/releases)，而不是持续堆积在 README 中。

## 发布版本

当前稳定版本为 **v0.19.0**。

- Linux 镜像：`ghcr.io/ntlx/serverfs_mcp`；v0.19.0 精确 pin：`ghcr.io/ntlx/serverfs_mcp:0.19.0`
- 稳定版本标签：`v0.19.0`
- 生产部署应固定到明确版本，不建议使用 `latest` 或 `edge`。
- 可移植 Agent Plugin：一个仅包含 Skills 的 `serverfs-plugin-v0.19.0.zip` Release 资产。
- Windows Release 资产包含产品本体、Agent Bridge 与原生 kernel wheel。
- macOS 直接使用产品与 Agent Bridge 的 Python distribution；v0.13 不提供单独的 macOS native wheel。v0.13.2 会在本地构建用于当前用户持久化服务的轻量 ServiceManagement companion。

具体资产、校验和与版本说明见 [最新 Release](https://github.com/NTLx/ServerFS_MCP/releases/latest)。

## 开发

Python/Linux 主路径：

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
docker compose config
SERVERFS_IMAGE=serverfs-mcp:dev docker compose build
```

Windows 与 macOS 原生开发还有额外的平台 Gate。维护者不变量、验证规则和 Release 纪律见 [AGENTS.md](AGENTS.md) 与 [docs/maintainers/](docs/maintainers/)。

## License

ServerFS MCP 基于 [MIT License](LICENSE) 发布。


ACP profile configuration and operation: [ACP Guide](https://ntlx.github.io/ServerFS_MCP/docs/acp/).
