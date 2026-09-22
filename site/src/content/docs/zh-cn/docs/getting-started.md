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

## 已发布镜像

稳定版本发布到 GHCR：

```text
ghcr.io/ntlx/serverfs_mcp:latest
ghcr.io/ntlx/serverfs_mcp:0.4
ghcr.io/ntlx/serverfs_mcp:0.4.0
```

生产环境建议固定使用明确的 release tag。
