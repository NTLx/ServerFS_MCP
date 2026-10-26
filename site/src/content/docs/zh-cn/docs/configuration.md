---
title: 配置
description: 定义受控 workdir，并继承安全默认值。
---

ServerFS 在启动时为每个已启用 workdir 解析出一份不可变的最终生效策略。

## MCP 传输配置

Linux Streamable HTTP 使用以下显式配置：

```text
SERVERFS_HTTP_HOST=0.0.0.0
SERVERFS_HTTP_PORT=8000
SERVERFS_HTTP_PATH=/mcp
SERVERFS_HTTP_ALLOWED_HOSTS=
SERVERFS_HTTP_ALLOWED_ORIGINS=
SERVERFS_HTTP_API_KEY=
```

监听地址与对外发布彼此独立。`compose.http.yml` 默认只发布到 `127.0.0.1:8000`。DNS rebinding 防护始终保持启用：当部署通过其他主机名或反向代理访问时，只把确实允许的 Host authority 精确加入 `SERVERFS_HTTP_ALLOWED_HOSTS`。需要浏览器 Origin 时，通过 `SERVERFS_HTTP_ALLOWED_ORIGINS` 显式配置允许列表。不要为了让代理工作而关闭 Host 防护。任何公网或非 loopback 暴露都应在单独配置的入口边界终止 TLS。

默认 host/port/path 继续与既有 OpenAI Tunnel 的 Docker 内部路径一致，因此新增通用 MCP HTTP 支持不会强迫现有 Tunnel 部署迁移。

### 可选静态 API Key

v0.17 通过 `SERVERFS_HTTP_API_KEY` 提供一个共享 HTTP bearer secret。留空即关闭。客户端优先使用 `Authorization: Bearer <key>`；也支持 `?key=<key>` 以兼容只接受一行 MCP URL 的产品，但 query secret 可能被上游 URL 日志记录。API Key 与内置 OAuth 互斥，并且不适用于原生 stdio。详见[静态 API Key](../api-key/)。

### 可选 OAuth 2.1

远程 HTTP 可以在 v0.16 中通过 `SERVERFS_OAUTH_ENABLED=true` + `compose.oauth.yml` 增加隔离的 OAuth 层。OAuth 默认关闭；关闭时不要求任何 OAuth 配置，也不适用于 Windows/macOS 原生 stdio。Provider 凭据与 provider/CIMD egress 只进入 `serverfs-auth`；MCP 服务只接收 Resource Server 配置和私有 introspection 边界。

公开 URL 模型、稳定 principal allowlist、provider callback、CIMD/DCR、代理与运维流程详见 [OAuth 2.1](../oauth/)。完整版本化变量面仍以 `.env.example` 为准。

## Workdir 槽位

最多可配置 16 个 workdir：

```text
WORKDIR_01_ALIAS=projects
WORKDIR_01_PATH=/srv/projects
WORKDIR_01_DESCRIPTION="Projects"
WORKDIR_01_READ_ONLY=true
```

另一个 workdir 可以显式开放写入：

```text
WORKDIR_02_ALIAS=scratch
WORKDIR_02_PATH=/srv/scratch
WORKDIR_02_DESCRIPTION="Agent scratch space"
WORKDIR_02_READ_ONLY=false
```

## 策略继承

全局 `SERVERFS_*` 设置充当默认值。某个槽位中的 `WORKDIR_XX_*` 标量配置会覆盖全局值；留空则继承全局默认值。

这一模型适用于：

- 隐藏文件策略
- 读写限制
- 二进制传输
- Repo Ops 启用策略
- Agent 策略

额外 deny glob 更严格：全局规则与 workdir 规则会**取并集**，因此 workdir 可以增加限制，但不能移除全局 deny 下限。

## 重要默认值

- Workdir 默认只读。
- 二进制传输默认关闭。`SERVERFS_MAX_BINARY_TRANSFER_BYTES` 是原生二进制传输与可选文件入口唯一公开的全局大小配置；workdir 覆盖值只能进一步收紧对应 workdir 的最终发布路径。
- ChatGPT 文件入口默认关闭，并且需要同时设置 `SERVERFS_FILE_INGRESS_ENABLED=true` 与启用 `file-ingress` Compose profile。
- 受限 OpenAI Blob 主机家族策略也独立默认关闭；只有确实需要 ChatGPT 文件参数时才设置 `SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS=true`。额外精确主机名通过 `SERVERFS_FILE_INGRESS_ALLOWED_HOSTS` 配置；通用通配符会被拒绝。
- 静态 API Key 默认关闭。非空 `SERVERFS_HTTP_API_KEY` 会使用一个共享密钥保护 Streamable HTTP，并且不能与内置 OAuth 同时启用。
- OAuth 默认关闭。启用时需要 OAuth Compose overlay；loopback 之外必须使用 HTTPS resource/auth URL；必须配置非空 introspection secret、至少一个身份 provider，以及精确的 default-deny principal allowlist。
- Repo Ops 默认关闭，通过 `WORKDIR_XX_REPO_OPS_ENABLED=true` 按 Workdir 独立启用。v0.23.0 发布实现仅声明 Linux 支持，并要求已认证的 Host Bridge transport，但不要求启用任何 Agent Runtime。
- `WORKDIR_XX_REPO_ACTIONS_JSON` 是 Repo Ops named action 的宿主机私有运维策略，只会被渲染到 Host Bridge 配置，不会转发进入 MCP 容器。Named action 要求 read-write Workdir，并强制配置 SHA-256 integrity pins。
- Agent 策略只有在显式配置后才启用。
- v0.12.0 把 Tunnel、Agent runtime 与 Jev 的代理选择保持独立：`SERVERFS_OPENAI_TUNNEL_USE_PROXY`、`SERVERFS_AGENT_USE_PROXY`、`SERVERFS_JEV_USE_PROXY` 复用同一组 `SERVERFS_PROXY_HOST/PORT/USERNAME/PASSWORD` 端点字段，但不会互相隐式启用。
- v0.12 的 Linux Agent 代理仅支持无凭据代理；若启用 Agent 代理同时配置了共享代理用户名/密码，部署渲染会 fail closed。需要认证的上游应通过无凭据本地 broker 转接。
- `SERVERFS_AGENT_RESULT_SPOOL_THRESHOLD_BYTES` 配置 inline/spool 边界；默认仍为 256 KiB，最大 spool 结果仍为 8 MiB。
- Jev Advisors 只有在 `SERVERFS_JEV_API_KEY` 非空时才启用；该 Key 只进入宿主机 Agent Bridge 部署路径，不进入 MCP 容器。
- MCP 工具结果不会暴露宿主机路径。
- Workdir 路径拼写错误会明确失败，而不是静默创建目录。

## 可选 Deterministic Repo Ops（v0.23.0）

Repo Ops 是按 Workdir 配置、默认关闭的仓库能力。最小 Linux/Compose 配置示例：

```text
WORKDIR_01_ALIAS=project
WORKDIR_01_PATH=/srv/project
WORKDIR_01_READ_ONLY=false
WORKDIR_01_REPO_OPS_ENABLED=true
WORKDIR_01_REPO_ACTIONS_JSON={...}
```

`WORKDIR_XX_REPO_ACTIONS_JSON` 把调用方可见的 action 名称映射到管理员拥有的固定 `argv`、`mutates`、`argument` 与 `integrity` 策略。调用方永远不能提供 executable/argv/environment/remote/ref/force。每个 named action 都必须配置非空 integrity map，并且即使 `mutates=false` 也始终获取独占 writer lease。

只有至少一个 Workdir 启用 Repo Ops 时，公共 Repo Ops 工具才会注册。Host Bridge transport 必须启用，因为它掌握宿主机 Git 凭据/路径上下文以及 writer-lease 协调；Agent mode 可以保持 disabled。完整安全边界与 action profile 契约见 [Deterministic Repo Ops](../repo-ops/)。

## 可选 Jev Advisors

如需启用宿主机 advisory suite，在现有、未跟踪的 `.env` 中设置 TypeSafe API Key：

```text
SERVERFS_JEV_API_KEY=<your key>
```

留空即可完全关闭 Agent Task Preflight、Runtime Router、Model Advisor 与 Approval Advisor。正常 Agent Bridge 安装器只会把已配置的 Key 渲染到用户自有、权限为 `0600` 的配置中。v0.12 中，`SERVERFS_JEV_USE_PROXY=true` 只让 Jev 的显式 HTTP client 使用共享代理；Agent 与 Tunnel 的路由仍彼此独立。运行行为与安全边界详见 [Jev Advisors](../jev-advisors/)。

完整 Linux/Docker 环境变量面请以版本化的 [`.env.example`](https://github.com/NTLx/ServerFS_MCP/blob/main/.env.example) 为准。Windows 与 macOS 原生部署的非敏感策略使用 [`serverfs.toml.example`](https://github.com/NTLx/ServerFS_MCP/blob/main/serverfs.toml.example)；`.env` 仅承载各平台指南所说明的少量 secret / proxy / runtime 配置。

ACP 完整配置、验证、任务生命周期与排障请见 [ACP 指南](../acp/)。
