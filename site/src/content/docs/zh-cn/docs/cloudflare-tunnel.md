---
title: Cloudflare Tunnel
description: 可选的 Linux Docker 出站隧道 MCP 接入模式（v0.24.0 正式支持）。
---

> **发布状态：**Linux Docker Compose Cloudflare Tunnel 已随 v0.24.0 发布。macOS 原生 Cloudflare 已随 v0.24.1 提供，适用于 Apple Silicon/macOS 27。v0.24.2 增加 Windows 原生 Cloudflare。两个独立、同用户 Task Scheduler owner 已通过公网 ChatGPT 工具与跨入口文件操作验收。真实公网 SSE 已通过（75.0、70.0、65.0 秒，约每 15 秒收到 keepalive；同一 session 的 `tools/list` 返回 200（27 个工具），新 session 返回 200）。Operator 于 2026-11-10 豁免本次整代 restart/recovery 测试；该测试未测试、未通过。不得声称 Connector-only restart 或 lossless SSE replay 已通过。每个 owner 只选择一个 provider；独立的 OpenAI 与 Cloudflare owner 可以并行。两者共享一个 Agent Bridge/Repo Ops 服务，Cloudflare 依赖期间必须保持 Bridge 可用。

Cloudflare Tunnel 为既有 Streamable HTTP `/mcp` 端点提供固定 Cloudflare 公网 HTTPS 域名，**无需在服务器开放公网入站端口**。它与 OpenAI Secure MCP Tunnel、普通 HTTP 反向代理是独立的接入方式，不改变 MCP 工具、Workdir 权限；**Tunnel Token 不能代替 MCP 客户端认证**。

## 网络路径

```text
MCP 客户端 -- HTTPS 443 --> Cloudflare 边缘
                                 |
                            出站隧道
                                 |
   cloudflared 容器 ----------- tunnel_egress
           |
    serverfs_internal
           |
    serverfs-mcp:8000/mcp
    （API Key / OAuth + 现有 Workdir 权限）
```

与主机直接运行 cloudflared 不同，Docker 容器内 Origin 必须使用 `http://serverfs-mcp:8000`，不能使用 localhost。MCP 容器还会加入现有的 `serverfs_file_ingress` 网络；这两个 MCP 网络都设置了 `internal:true`。只有 cloudflared 接入 `tunnel_egress`，也不加入 `serverfs_file_ingress`；网络存在不意味着自动启用可选文件入口服务。

## Linux Compose 部署步骤

1. 在 Cloudflare 创建或复用远程管理型 Tunnel。当前 VPS 测试使用已建立的 `serverfs-v024-dev`，拟保留的公网域名为 `vps-serverfs-mcp.ntlx.top`，HTTP Origin 为 `http://serverfs-mcp:8000`。不得重复创建 Tunnel。其他部署应使用各自的合法独立域名。
2. 将原始 Tunnel Token 保存到**仓库和所有 Workdir 以外**、权限 0600 的文件，且该文件可被 cloudflared 容器用户（`CLOUDFLARED_UID:CLOUDFLARED_GID`，默认 `10001:10001`）读取。若文件由当前非 root 用户持有，可将 `CLOUDFLARED_UID` / `CLOUDFLARED_GID` 设为 `id -u` / `id -g` 的实际数值，而不改变 ServerFS MCP 本身的 UID/GID。忽略版本控制的 `.env` 中只填写绝对文件路径 `CLOUDFLARE_TUNNEL_TOKEN_FILE`，不填写 Token 明文。
3. 在 `SERVERFS_HTTP_ALLOWED_HOSTS` 中追加公网域名，保留 `serverfs-mcp:8000`。独立启用现有 `SERVERFS_HTTP_API_KEY` 或 OAuth 2.1。推荐使用已验证标签 `cloudflare/cloudflared:2026.10.7`，允许通过 `CLOUDFLARED_IMAGE` 覆盖，不要求锁定镜像 Digest。
4. 先只验证 Compose 配置：

   ```sh
   docker compose -f compose.yml -f compose.cloudflare.yml config --quiet
   ```

5. 仅在**独立新部署**时，先确认专用 Compose 项目名、私有 `--env-file`、完整 Overlay、API Key/OAuth 认证和 Workdir 挂载，再遵循 [Linux 完整运维手册](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/cloudflare-tunnel.md)启动。**不要把通用的 `docker compose up` 直接复制到现有生产 checkout 执行。**

既有部署必须保留 Agent / Repo Ops / OAuth 相关 Compose Overlay。此方案不发布主机端口，OpenAI Tunnel 默认不启动。不得将 Token 粘贴到对话、日志或代码仓库。

**当前 VPS 禁止将上述通用启动命令用于生产 `serverfs_mcp` 项目。** 其运行中的 HTTP API Key/OAuth 未启用。现在已有独立且启用了 API Key 的 `serverfs_cf_staging` 项目，只挂载一个只读测试目录，不发布宿主机端口。无凭据与错误凭据返回 401，正确凭据的 MCP `initialize` 返回 200 并产生会话 ID。专用 Tunnel Token 已由操作员安全配置，Connector 已启动，Cloudflare Tunnel 状态为 healthy；公网 DNS、HTTPS TLS 和 JSON-RPC `initialize`、`tools/list`、只读工作目录查询及测试文件读取均已验收通过，写入和路径越界请求被拒绝。隔离环境中仅重启 cloudflared 后，Cloudflare Tunnel 重新恢复 healthy，公网 HTTPS 和 401 拒绝行为保持正常，生产容器未受影响。**后续验收均已通过**：ChatGPT 直接调用只读文件及搜索工具，隐藏 `.env` 与越界路径被拒；公网 SSE 至少保持 65 秒，收到事件/心跳且同一会话内三次 `tools/list` 成功；仅重启 Connector 后仍可用原 MCP Session ID 调用工具，无需重新初始化。该结论不等于生产工作目录验收，也不保证重启时正在传输的 SSE 消息无损重放。安全部署的具体 Compose 参数请遵循完整运维手册。

## VPS 测试域名的 HTTPS 准备状态

拟用于测试并保留为正式入口的 URL 是 `https://vps-serverfs-mcp.ntlx.top/mcp`。它属于 `ntlx.top` 的一级子域名，**已经由 Cloudflare 现有生效的 Universal SSL 通配符证书 `*.ntlx.top` 覆盖**，无需购买额外证书服务。该域名现已建立经 Cloudflare 代理的 CNAME，Tunnel Connector 已成功连接，且公网 HTTPS 证书链及使用 Bearer 凭据的 MCP JSON-RPC 工具调用实际通过。对外连接的是**只读隔离测试实例**，而非生产 ServerFS；ChatGPT 客户端在只读隔离环境的真实工具调用、SSE 与会话重连验收已经完成；生产工作目录和 Agent 相关能力尚未纳入这次验收。

## 认证与验收

远程 MCP URL 通常为 `https://your-host.example.com/mcp`，优先使用 OAuth 2.1 或 Bearer API Key。ServerFS 的 `/mcp?key=...` 兼容写法可能造成凭据出现在 Cloudflare 等上游日志，应当评估后使用。

分层验证 Cloudflare / DNS / Connector 状态、无凭据 MCP initialize 返回 401、带凭据 initialize 和会话、`tools/list`、`list_workdirs`、只读和敏感路径拒绝、流式响应和断线恢复，启用文件或 Agent 能力时再验证大文件与长任务。**Tunnel 建连成功不等于 ChatGPT 客户端验收通过。**

详见 GitHub 上的 [Linux 完整部署和验收手册](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/cloudflare-tunnel.md)、[静态 API Key](../api-key/) 和 [OAuth 2.1](../oauth/)。
