---
title: 静态 API Key
description: 使用一个可选的共享 API Key 保护远程 Streamable HTTP。
---

ServerFS v0.17.0 为**远程 Streamable HTTP** 增加了可选的静态 API Key 认证。它适合单个操作者或少量可信客户端，在不部署完整 OAuth 2.1 的情况下提供一个简单的应用层认证边界。

API Key 只证明调用方持有该实例的共享密钥。认证通过后，真正决定调用方能够做什么的仍然是现有 ServerFS workdir 权限策略。

## 配置一个 Key

在 `.env` 中设置一个高熵随机密钥：

```text
SERVERFS_HTTP_API_KEY=<long-random-secret>
```

留空或不设置即表示关闭。

静态 API Key 与内置 OAuth 互斥。如果 `SERVERFS_HTTP_API_KEY` 非空且同时设置 `SERVERFS_OAUTH_ENABLED=true`，ServerFS 会在启动时直接失败，而不是自行选择其中一种模式。

Windows/macOS 原生 stdio 不使用这个 HTTP 凭据。

## 客户端认证

推荐方式：

```http
Authorization: Bearer <key>
```

对于只接受一行完整 MCP URL 的产品，也支持兼容形式：

```text
https://mcp.example.com/mcp?key=<key>
```

每个请求只允许一种凭据来源。以下情况会被拒绝：重复 Authorization 头、重复 `key` 查询参数、空凭据，以及同时提供 Header 与 Query 凭据。

API Key 失败会在 HTTP 边界直接拒绝，不会暴露 OAuth discovery，也不会触发 OAuth 登录流程。

## Query 参数风险

`?key=` 很方便，但更容易在运维链路中泄漏。反向代理、WAF、浏览器历史、监控系统等都可能记录完整 URL。

API Key 模式下，ServerFS 会关闭其自身默认的 Uvicorn access log，因此受支持的 ServerFS HTTP runner 不会打印带密钥的 query URL。但 ServerFS 无法控制请求到达自身之前的上游基础设施日志。

只要客户端支持自定义 Header，就优先使用 `Authorization: Bearer`。如果必须使用 `?key=`，请确保上游 access log 不持久化 query string。

## HTTPS 与部署边界

静态 API Key 是 bearer credential。公网部署仍然必须使用 HTTPS。

ServerFS 不负责域名、DNS 或 TLS 证书管理。推荐公网形态仍然是：

```text
MCP client
   |
   | HTTPS
   v
反向代理 / 网关
   |
   | loopback / 私网 HTTP
   v
ServerFS
```

`compose.http.yml` 默认只把 MCP 端点发布到 loopback。

## 原有授权规则仍然生效

API Key 不会绕过：

- 已配置的 workdir 边界；
- 默认只读策略；
- 隐藏文件/凭据路径拒绝规则；
- revision-guarded mutation；
- 二进制传输开关与大小限制；
- Agent Bridge 开关与 runtime allowlist；
- 各平台原生文件系统隔离机制。

## 轮换

v0.17.0 刻意只支持一个当前有效 Key，不引入凭据数据库。

轮换方法：

1. 生成新的高熵随机密钥；
2. 替换 `.env` 中的 `SERVERFS_HTTP_API_KEY`；
3. 重建/重启 ServerFS；
4. 更新客户端配置。

新进程启动后，旧 Key 即失效。

## 如何选择认证边界

| 模式 | 配置 | 身份模型 |
| --- | --- | --- |
| 无内置认证 | API Key 为空、OAuth 关闭 | 私网、OpenAI Tunnel 或外部认证网关 |
| 静态 API Key | `SERVERFS_HTTP_API_KEY=<secret>` | 一个共享密钥持有者 |
| OAuth 2.1 | `SERVERFS_OAUTH_ENABLED=true` | 联邦身份 + ServerFS 资源 Token |

如果需要用户身份、浏览器登录或可独立吊销的凭据，请使用 [OAuth 2.1](../oauth/)。
