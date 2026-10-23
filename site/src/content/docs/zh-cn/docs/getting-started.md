---
title: 快速开始
description: 使用标准 Streamable HTTP 部署 ServerFS MCP；仅在需要时再启用 OpenAI Secure MCP Tunnel。
---

本页介绍 Linux/Docker 部署。原生部署请分别查看 [Windows 原生部署](./windows-native/) 或 [macOS 原生部署](./macos-native/)。

## 前置条件

- Linux 服务器
- Docker + Docker Compose

OpenAI Secure MCP Tunnel 与 Runtime API Key 都是可选项，仅在需要 ChatGPT Tunnel 入口时使用。

## 标准 MCP 部署

```bash
git clone https://github.com/NTLx/ServerFS_MCP.git
cd ServerFS_MCP
cp .env.example .env
chmod 600 .env
```

至少在 `.env` 中配置一个 workdir，然后启动标准 Streamable HTTP 端点：

```bash
docker compose pull
docker compose -f compose.yml -f compose.http.yml up -d serverfs-mcp
docker compose -f compose.yml -f compose.http.yml ps
```

HTTP override 默认仅发布 `127.0.0.1:8000/mcp`，并保持 OpenAI Tunnel 不启动。11 个文件系统工具仍默认只读；除非某个 workdir 显式设置 `WORKDIR_XX_READ_ONLY=false`。如需超出 loopback 暴露 ServerFS，应配置 Host allowlist，并在明确的入口边界终止 TLS。

## 远程 HTTP 可选静态 API Key

如果只需要简单的内置共享密钥边界，在 `.env` 中设置：

```text
SERVERFS_HTTP_API_KEY=<long-random-secret>
```

仍然使用同一套 `compose.yml` + `compose.http.yml` 部署。客户端优先发送 `Authorization: Bearer <key>`。对于只接受完整 URL 的客户端，也支持 DingTalk 风格的 `https://mcp.example.com/mcp?key=<key>`，但 query credential 可能被上游 access log 记录。API Key 与内置 OAuth 互斥。详见[静态 API Key](./api-key/)。

## 远程 HTTP 可选 OAuth 2.1

v0.16.0 可以在不改变本地/原生 stdio 的前提下，为通用 Streamable HTTP 部署增加 OAuth：

```bash
mkdir -p .serverfs-auth-state
chmod 700 .serverfs-auth-state

docker compose --env-file .env \
  -f compose.yml \
  -f compose.http.yml \
  -f compose.oauth.yml \
  up -d
```

启动前需要设置 `SERVERFS_OAUTH_ENABLED=true`、canonical 公网 MCP resource URL、`serverfs-auth` public/issuer URL、introspection secret、至少一个 provider，以及精确的稳定 principal allowlist。公网部署应继续让 MCP/auth 容器只绑定 loopback，再通过 HTTPS 反向代理发布。Provider callback、CIMD/DCR、备份/撤销与 OpenAI Tunnel 兼容边界详见 [OAuth 2.1](./oauth/)。

## 可选 ChatGPT / OpenAI Tunnel 部署

在 `.env` 中设置 `CONTROL_PLANE_TUNNEL_ID` 与 `CONTROL_PLANE_API_KEY`，然后使用既有基础 Compose 路径：

```bash
docker compose pull
docker compose up -d
docker compose ps
docker compose logs -f openai-tunnel
```

该路径继续作为受支持的认证入口适配器，但不再是通用 MCP 客户端的前置条件。运维排障请使用 [Tunnel 诊断](./tunnel-diagnostics/)；仅看到容器 `Up` 并不能证明 Tunnel readiness 或 control-plane 连续可用。

Linux 主机如果受到 registry/网络访问限制，见[受限网络 ARM64 部署](./restricted-network-arm64/)，其中分别说明 Docker daemon proxy 与离线镜像转运路径。

如果只希望以最低权限向 AI 暴露生成后的宿主机状态，而不是授予宿主机权限，见[只读宿主机可观测性](./host-observability/)。

## Agent 部署

Agent 能力通过显式 overlay 启用：

```bash
docker compose --env-file .env -f compose.yml -f compose.agent.yml up -d
python3 deployment/agent-bridge/verify_host.py --require-runtimes
```

启用之前，请先按照仓库中的 [Agent Bridge 部署指南](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/agent-bridge/README.md) 安装并验证宿主机 Agent Bridge。后续每次重建都必须保留 Agent overlay；只用基础 Compose 会移除 Agent socket/lock 挂载。

### 可选 Jev Advisors

如果需要 task preflight、runtime routing 与 provider approval 的 advisory context，可在安装/更新 Bridge 前向现有、未跟踪的 `.env` 中加入 TypeSafe API Key：

```text
SERVERFS_JEV_API_KEY=<your key>
```

留空即可让 Agent Bridge 完全不依赖 Jev。无需额外修改 MCP 容器配置，也无需刷新 ChatGPT 插件。详见 [Jev Advisors](./jev-advisors/)。

## ChatGPT 文件参数入口

自 v0.5.0 起，ServerFS 支持让 `upload_binary_file` 直接接收 ChatGPT/OpenAI `file` 参数，同时保持 MCP 主容器没有互联网出口。二进制传输与文件入口是两个独立 opt-in。常见 ChatGPT 场景可设置：

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
docker compose --env-file .env --profile file-ingress -f compose.yml -f compose.agent.yml pull
docker compose --env-file .env --profile file-ingress -f compose.yml -f compose.agent.yml up -d
```

Sidecar 不挂载 workdir、不持有 Tunnel/OpenAI 凭据，也不发布端口。项目不支持通用主机名通配符；v0.5.0 引入的文件入口主机策略与网络边界详见[二进制传输](./binary-transfer/)和[安全模型](./security/)。

## 当前稳定版本：v0.22.0

v0.22.0 使用不可变标签 `v0.22.0`，并发布以下 GHCR 稳定标签：

```text
ghcr.io/ntlx/serverfs_mcp:latest
ghcr.io/ntlx/serverfs_mcp:0.22
ghcr.io/ntlx/serverfs_mcp:0.22.0
```

生产环境应固定使用 `0.22.0`，而不是长期跟随 `latest` 或 `edge`。同一 tag 在 GitHub Release 上发布三个 Windows 安装 wheel（产品包 + `serverfs-agent-bridge` + `serverfs-windows-native` abi3）并记录 SHA-256。同一 Release 还提供一个 skills-only portable Agent Plugin：`serverfs-plugin-v0.22.0.zip`，见 [Agent Skills](./agent-skills/)。macOS 直接使用产品包与 Bridge Python 包，不存在单独的文件系统 native wheel；v0.19.0 延续 v0.13.2 引入的小型 ServiceManagement companion，用于持久化 Tunnel/Bridge 服务。见 [Windows 原生部署](./windows-native/)和 [macOS 原生部署](./macos-native/)。

ACP 完整配置、验证、任务生命周期与排障请见 [ACP 指南](./acp/)。
