---
title: 快速开始
description: 使用 Docker Compose 与 OpenAI Secure MCP Tunnel 部署 ServerFS MCP。
---

## 前置条件

- Linux 服务器
- Docker + Docker Compose
- OpenAI Secure MCP Tunnel
- 具备官方 Tunnel 客户端所需权限的 Runtime API Key

## 基础部署

```bash
git clone https://github.com/NTLx/ServerFS_MCP.git
cd ServerFS_MCP
cp .env.example .env
chmod 600 .env
```

至少在 `.env` 中配置一个 workdir 与 Tunnel 凭据，然后运行：

```bash
docker compose pull
docker compose up -d
docker compose ps
docker compose logs -f openai-tunnel
```

基础部署暴露 11 个文件系统工具。除非某个 workdir 显式设置 `WORKDIR_XX_READ_ONLY=false`，否则保持只读。

## Agent 部署

Agent 能力通过显式 overlay 启用：

```bash
docker compose -f compose.yml -f compose.agent.yml up -d
```

启用之前，请先按照仓库中的 [Agent Bridge 部署指南](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/agent-bridge/README.md) 安装并验证宿主机 Agent Bridge。

### 可选 Jev Advisors

如果需要 task preflight、runtime routing 与 provider approval 的 advisory context，可在安装/更新 Bridge 前向现有、未跟踪的 `.env` 中加入 TypeSafe API Key：

```text
SERVERFS_JEV_API_KEY=<your key>
```

留空即可让 Agent Bridge 完全不依赖 Jev。无需额外修改 MCP 容器配置，也无需刷新 ChatGPT 插件。详见 [Jev Advisors](./jev-advisors/)。

## ChatGPT 文件参数入口

v0.5.0 可以让 `upload_binary_file` 直接接收 ChatGPT/OpenAI `file` 参数，同时保持 MCP 主容器没有互联网出口。二进制传输与文件入口是两个独立 opt-in。常见 ChatGPT 场景可设置：

```text
SERVERFS_BINARY_TRANSFER_ENABLED=true
SERVERFS_FILE_INGRESS_ENABLED=true
SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS=true
```

然后通过 `file-ingress` profile 启动隔离 sidecar：

```bash
docker compose --profile file-ingress pull
docker compose --profile file-ingress up -d
```

如果同时启用 Agent Bridge，必须在同一次 Compose 调用中同时保留 Agent overlay 与 file-ingress profile：

```bash
docker compose --profile file-ingress -f compose.yml -f compose.agent.yml pull
docker compose --profile file-ingress -f compose.yml -f compose.agent.yml up -d
```

Sidecar 不挂载 workdir、不持有 Tunnel/OpenAI 凭据，也不发布端口。项目不支持通用主机名通配符；v0.5.0 的主机策略与网络边界详见[二进制传输](./binary-transfer/)和[安全模型](./security/)。

## 已发布镜像

ServerFS MCP v0.5.0 已发布到 GHCR，稳定标签如下：

```text
ghcr.io/ntlx/serverfs_mcp:latest
ghcr.io/ntlx/serverfs_mcp:0.5
ghcr.io/ntlx/serverfs_mcp:0.5.0
```

生产环境建议固定使用 `0.5.0`，不要长期跟随 `latest` 或 `edge`。
