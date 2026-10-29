---
title: 快速开始
description: 使用标准 Streamable HTTP 部署 ServerFS MCP；仅在需要时再启用 OpenAI Secure MCP Tunnel。
---

本页介绍 Linux/Docker 部署。原生部署请分别查看 [Windows 原生部署](../windows-native/) 或 [macOS 原生部署](../macos-native/)。

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

至少在 `.env` 中配置一个 workdir。启动前，在 `.env` 中为默认 loopback 端点设置 Host allowlist：

```text
SERVERFS_HTTP_ALLOWED_HOSTS=serverfs-mcp:8000,127.0.0.1:8000,localhost:8000
```

空 allowlist 仅允许 Docker 内部 authority。保留 Tunnel/内部路径所需的 `serverfs-mcp:8000`，并显式加入供本地客户端直接访问的 `127.0.0.1:8000` 与 `localhost:8000`。如果自定义端口，loopback authority 应使用实际发布端口，`serverfs-mcp` 应使用容器 HTTP 端口。对于外部客户端或反向代理，应将 ServerFS 实际收到的每个 Host authority 精确加入 allowlist，而不是关闭 Host 校验。然后启动标准 Streamable HTTP 端点：

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

仍然使用同一套 `compose.yml` + `compose.http.yml` 部署。客户端优先发送 `Authorization: Bearer <key>`。对于只接受完整 URL 的客户端，也支持 DingTalk 风格的 `https://mcp.example.com/mcp?key=<key>`，但 query credential 可能被上游 access log 记录。API Key 与内置 OAuth 互斥。详见[静态 API Key](../api-key/)。

## 远程 HTTP 可选 OAuth 2.1

v0.16.0 可以在不改变本地/原生 stdio 的前提下，为通用 Streamable HTTP 部署增加 OAuth：

启动前需要设置 `SERVERFS_OAUTH_ENABLED=true`、canonical 公网 MCP resource URL、`serverfs-auth` public/issuer URL、introspection secret、至少一个 provider，以及精确的稳定 principal allowlist。

启动 Compose 前，先为配置的 `SERVERFS_UID:SERVERFS_GID` 准备 auth state 目录。默认身份为 `10001:10001`、`SERVERFS_AUTH_STATE_HOST_DIR=./.serverfs-auth-state` 时，在仓库根目录运行：

```bash
sudo install -d -m 700 -o 10001 -g 10001 -- .serverfs-auth-state
```

将所有者设为 `10001:10001` 通常需要 `sudo`（或管理员操作）；容器仍以该非 root 身份运行。如果在 `.env` 中自定义了 `SERVERFS_UID`、`SERVERFS_GID` 或 `SERVERFS_AUTH_STATE_HOST_DIR`，请将上述数字 ID 与路径替换为实际值。Compose 会读取 `.env`，但这些值不会自动导出到当前 shell。

若采用当前用户身份部署，也可以将 `.env` 中的 `SERVERFS_UID` 与 `SERVERFS_GID` 分别设为 `id -u` 与 `id -g` 的输出，前提是该身份也符合 MCP workdir 的权限要求。对于由该用户拥有的新目录，无需特权：

```bash
install -d -m 700 -- .serverfs-auth-state
```

如果自定义了 state 路径，请使用实际路径。仅设置 `700` 不会让不同的容器 UID 获得写权限；启动前应确认目录所有者与配置的 UID 一致。保持目录私有，不要放宽权限。


```bash
docker compose --env-file .env \
  -f compose.yml \
  -f compose.http.yml \
  -f compose.oauth.yml \
  up -d
```

启动前需要设置 `SERVERFS_OAUTH_ENABLED=true`、canonical 公网 MCP resource URL、`serverfs-auth` public/issuer URL、introspection secret、至少一个 provider，以及精确的稳定 principal allowlist。公网部署应继续让 MCP/auth 容器只绑定 loopback，再通过 HTTPS 反向代理发布。Provider callback、CIMD/DCR、备份/撤销与 OpenAI Tunnel 兼容边界详见 [OAuth 2.1](../oauth/)。

## 可选 ChatGPT / OpenAI Tunnel 部署

在 `.env` 中设置 `CONTROL_PLANE_TUNNEL_ID` 与 `CONTROL_PLANE_API_KEY`，然后使用既有基础 Compose 路径：

```bash
docker compose pull
docker compose up -d
docker compose ps
docker compose logs -f openai-tunnel
```

该路径继续作为受支持的认证入口适配器，但不再是通用 MCP 客户端的前置条件。运维排障请使用 [Tunnel 诊断](../tunnel-diagnostics/)；仅看到容器 `Up` 并不能证明 Tunnel readiness 或 control-plane 连续可用。

Linux 主机如果受到 registry/网络访问限制，见[受限网络 ARM64 部署](../restricted-network-arm64/)，其中分别说明 Docker daemon proxy 与离线镜像转运路径。

如果只希望以最低权限向 AI 暴露生成后的宿主机状态，而不是授予宿主机权限，见[只读宿主机可观测性](../host-observability/)。

## Agent 部署

Agent 能力通过显式 overlay 启用：

```bash
docker compose --env-file .env -f compose.yml -f compose.agent.yml up -d
uv run --frozen --no-sync python deployment/agent-bridge/verify_host.py --require-runtimes
```

启用之前，请先按照仓库中的 [Agent Bridge 部署指南](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/agent-bridge/README.md) 安装并验证宿主机 Agent Bridge。后续每次重建都必须保留 Agent overlay；只用基础 Compose 会移除 Agent socket/lock 挂载。源码 checkout 中的 Python 命令应通过项目级 uv 环境执行，不要直接依赖宿主机未限定版本的 `python3`。

## 可选 Deterministic Repo Ops（v0.23.0）

Repo Ops 与 Agent delegation 相互独立，但会复用已认证的 Host Bridge transport 与 writer-lease 基础设施。在可写的 Linux/Compose Workdir 上设置：

```text
WORKDIR_01_REPO_OPS_ENABLED=true
WORKDIR_01_REPO_ACTIONS_JSON={...}
```

不需要启用任何 Agent Runtime。v0.23.0 增加 `inspect_repo`、`run_repo_action`、`commit_repo_changes` 与 `push_repo`；named action 使用管理员拥有的固定 argv 和强制 SHA-256 integrity pins，调用方不会获得 shell/argv/remote/ref/force 控制。发布验收覆盖 Linux bootstrap / 内部 E2E、公共 ChatGPT MCP E2E、完整 root/Agent Bridge CI、MCP conformance、Windows/macOS 回归以及冻结后的站点 build/deploy。启用前请阅读 [Deterministic Repo Ops](../repo-ops/)。

### 可选 Jev Advisors

如果需要 task preflight、runtime routing 与 provider approval 的 advisory context，可在安装/更新 Bridge 前向现有、未跟踪的 `.env` 中加入 TypeSafe API Key：

```text
SERVERFS_JEV_API_KEY=<your key>
```

留空即可让 Agent Bridge 完全不依赖 Jev。无需额外修改 MCP 容器配置，也无需刷新 ChatGPT 插件。详见 [Jev Advisors](../jev-advisors/)。

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

Sidecar 不挂载 workdir、不持有 Tunnel/OpenAI 凭据，也不发布端口。项目不支持通用主机名通配符；v0.5.0 引入的文件入口主机策略与网络边界详见[二进制传输](../binary-transfer/)和[安全模型](../security/)。

## 当前稳定版本：v0.23.3

已发布的 v0.23.3 使用不可变标签 `v0.23.3`，并提供以下 GHCR 稳定标签：

```text
ghcr.io/ntlx/serverfs_mcp:latest
ghcr.io/ntlx/serverfs_mcp:0.23
ghcr.io/ntlx/serverfs_mcp:0.23.3
```

生产环境应固定使用 `0.23.3`，而不是长期跟随 `latest` 或 `edge`。v0.23.3 GitHub Release 包含三个 Windows 安装 wheel（产品包 + `serverfs-agent-bridge` + `serverfs-windows-native` abi3）以及 skills-only portable Agent Plugin `serverfs-plugin-v0.23.3.zip`；发布证据见 [v0.23.3 发布清单](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.23.3-release-checklist-2026-10-29.md)；见 [Agent Skills](../agent-skills/)。macOS 直接使用产品包与 Bridge Python 包，不存在单独的文件系统 native wheel；v0.19.0 延续 v0.13.2 引入的小型 ServiceManagement companion，用于持久化 Tunnel/Bridge 服务。见 [Windows 原生部署](../windows-native/)和 [macOS 原生部署](../macos-native/)。

ACP 完整配置、验证、任务生命周期与排障请见 [ACP 指南](../acp/)。
