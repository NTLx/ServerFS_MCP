---
title: 配置
description: 定义受控 workdir，并继承安全默认值。
---

ServerFS 在启动时为每个已启用 workdir 解析出一份不可变的最终生效策略。

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
- Agent 策略

额外 deny glob 更严格：全局规则与 workdir 规则会**取并集**，因此 workdir 可以增加限制，但不能移除全局 deny 下限。

## 重要默认值

- Workdir 默认只读。
- 二进制传输默认关闭。`SERVERFS_MAX_BINARY_TRANSFER_BYTES` 是原生二进制传输与可选文件入口唯一公开的全局大小配置；workdir 覆盖值只能进一步收紧对应 workdir 的最终发布路径。
- ChatGPT 文件入口默认关闭，并且需要同时设置 `SERVERFS_FILE_INGRESS_ENABLED=true` 与启用 `file-ingress` Compose profile。
- 受限 OpenAI Blob 主机家族策略也独立默认关闭；只有确实需要 ChatGPT 文件参数时才设置 `SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS=true`。额外精确主机名通过 `SERVERFS_FILE_INGRESS_ALLOWED_HOSTS` 配置；通用通配符会被拒绝。
- Agent 策略只有在显式配置后才启用。
- v0.12.0 把 Tunnel、Agent runtime 与 Jev 的代理选择保持独立：`SERVERFS_OPENAI_TUNNEL_USE_PROXY`、`SERVERFS_AGENT_USE_PROXY`、`SERVERFS_JEV_USE_PROXY` 复用同一组 `SERVERFS_PROXY_HOST/PORT/USERNAME/PASSWORD` 端点字段，但不会互相隐式启用。
- v0.12 的 Linux Agent 代理仅支持无凭据代理；若启用 Agent 代理同时配置了共享代理用户名/密码，部署渲染会 fail closed。需要认证的上游应通过无凭据本地 broker 转接。
- `SERVERFS_AGENT_RESULT_SPOOL_THRESHOLD_BYTES` 配置 inline/spool 边界；默认仍为 256 KiB，最大 spool 结果仍为 8 MiB。
- Jev Advisors 只有在 `SERVERFS_JEV_API_KEY` 非空时才启用；该 Key 只进入宿主机 Agent Bridge 部署路径，不进入 MCP 容器。
- MCP 工具结果不会暴露宿主机路径。
- Workdir 路径拼写错误会明确失败，而不是静默创建目录。

## 可选 Jev Advisors

如需启用宿主机 advisory suite，在现有、未跟踪的 `.env` 中设置 TypeSafe API Key：

```text
SERVERFS_JEV_API_KEY=<your key>
```

留空即可完全关闭 Agent Task Preflight、Runtime Router、Model Advisor 与 Approval Advisor。正常 Agent Bridge 安装器只会把已配置的 Key 渲染到用户自有、权限为 `0600` 的配置中。v0.12 中，`SERVERFS_JEV_USE_PROXY=true` 只让 Jev 的显式 HTTP client 使用共享代理；Agent 与 Tunnel 的路由仍彼此独立。运行行为与安全边界详见 [Jev Advisors](./jev-advisors/)。

完整 Linux/Docker 环境变量面请以版本化的 [`.env.example`](https://github.com/NTLx/ServerFS_MCP/blob/main/.env.example) 为准。Windows 与 macOS 原生部署的非敏感策略使用 [`serverfs.toml.example`](https://github.com/NTLx/ServerFS_MCP/blob/main/serverfs.toml.example)；`.env` 仅承载各平台指南所说明的少量 secret / proxy / runtime 配置。
