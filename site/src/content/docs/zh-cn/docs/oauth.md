---
title: OAuth 2.1
description: 使用可选 OAuth 2.1 与身份联合保护远程 Streamable HTTP。
---

ServerFS v0.16.0 为**远程 Streamable HTTP** 新增可选 OAuth 2.1 认证。OAuth 默认关闭，也不适用于 Windows/macOS 原生 stdio。

OAuth 解决的是**谁可以进入这个 ServerFS 实例**；现有 workdir 策略仍决定**已准入调用方具体能做什么**。

```text
GitHub / 钉钉 / 飞书 / 微信
          │
          ▼
     serverfs-auth
   身份联合 + 精确准入
   ServerFS 自有不透明 Token
          │ 私有 introspection
          ▼
      serverfs-mcp
          │
          ▼
     现有 workdir 策略
```

`serverfs-auth` 独占上游身份提供商凭据与联网出口；`serverfs-mcp` 不接收 provider 凭据，也不获得通用 Internet egress。

## 部署边界

OAuth 当前是 **Linux Docker Compose** 能力：

```bash
docker compose --env-file .env \
  -f compose.yml \
  -f compose.http.yml \
  -f compose.oauth.yml \
  up -d
```

只有需要 Agent 委托时才额外加入 `compose.agent.yml`。

典型公网部署使用两个独立 HTTPS origin：

```text
https://mcp.example.com/mcp -> 127.0.0.1:8000/mcp
https://auth.example.com    -> 127.0.0.1:8082
```

Docker 端口继续只绑定 loopback，由反向代理终止公网 TLS。反向代理必须保留 OAuth metadata 与 callback 所依赖的外部 host/scheme 语义。

最小 `.env` 配置：

```text
SERVERFS_OAUTH_ENABLED=true
SERVERFS_OAUTH_RESOURCE_URL=https://mcp.example.com/mcp
SERVERFS_AUTH_PUBLIC_URL=https://auth.example.com
SERVERFS_OAUTH_ISSUER_URL=https://auth.example.com
SERVERFS_OAUTH_REQUIRED_SCOPES=serverfs:access
SERVERFS_OAUTH_INTROSPECTION_SECRET=<long-random-secret>
SERVERFS_AUTH_ALLOWED_PRINCIPALS=<provider:stable-subject,...>
```

`SERVERFS_OAUTH_ISSUER_URL` 必须与 `SERVERFS_AUTH_PUBLIC_URL` 完全一致。除明确的 loopback 测试外，公网 URL 必须使用 HTTPS。

启动 Compose 前必须预先创建 auth state 目录，并确保配置的非 root `SERVERFS_UID:GID` 可写：

```bash
mkdir -p .serverfs-auth-state
chmod 700 .serverfs-auth-state
```

## 默认拒绝的准入策略

`SERVERFS_AUTH_ALLOWED_PRINCIPALS` 只接受逗号分隔的精确 `provider:stable-subject`。不支持通配符。

| Provider | ServerFS Principal | 稳定身份来源 |
| --- | --- | --- |
| GitHub | `github:<numeric-id>` | 不可变的数字 user `id` |
| 钉钉 | `dingtalk:<unionId>` | Open Platform `unionId` |
| 飞书 | `feishu:<open_id>` | app-scoped `open_id` |
| 微信 | `wechat:<unionid>` | Open Platform `unionid` |

显示名称、邮箱、手机号、昵称等可变字段都不是授权键。

## Provider 回调配置

每个 provider 独立启用；启用后必须同时提供 client ID 与 client secret。

### GitHub OAuth App

回调地址：

```text
https://auth.example.com/oauth/provider/github/callback
```

```text
SERVERFS_OAUTH_GITHUB_ENABLED=true
SERVERFS_OAUTH_GITHUB_CLIENT_ID=<client-id>
SERVERFS_OAUTH_GITHUB_CLIENT_SECRET=<client-secret>
```

ServerFS 不申请 repository scope，只使用公开的已认证用户身份，并以不可变数字 GitHub user ID 做准入。

### 飞书 / Lark

回调地址：

```text
https://auth.example.com/oauth/provider/feishu/callback
```

```text
SERVERFS_OAUTH_FEISHU_ENABLED=true
SERVERFS_OAUTH_FEISHU_CLIENT_ID=<app-id>
SERVERFS_OAUTH_FEISHU_CLIENT_SECRET=<app-secret>
```

流程只请求 `contact:user.base:readonly`，不请求 `offline_access`，并以 `open_id` 做准入键。

### 钉钉

回调地址：

```text
https://auth.example.com/oauth/provider/dingtalk/callback
```

```text
SERVERFS_OAUTH_DINGTALK_ENABLED=true
SERVERFS_OAUTH_DINGTALK_CLIENT_ID=<client-id>
SERVERFS_OAUTH_DINGTALK_CLIENT_SECRET=<client-secret>
```

流程只请求 `openid`，回调读取钉钉的 `authCode` 字段，并以 `unionId` 做准入键。

### 微信开放平台

这里指**微信开放平台网站应用扫码登录**，不是企业微信，也不是公众号网页授权。

回调地址：

```text
https://auth.example.com/oauth/provider/wechat/callback
```

```text
SERVERFS_OAUTH_WECHAT_ENABLED=true
SERVERFS_OAUTH_WECHAT_CLIENT_ID=<app-id>
SERVERFS_OAUTH_WECHAT_CLIENT_SECRET=<app-secret>
```

流程使用 `snsapi_login`，会校验 user-info 的 `openid` 与 Token subject 一致，并以开放平台 `unionid` 做准入。

Provider access/refresh token 只作为短生命周期的上游身份材料存在；准入成功后由 ServerFS 签发自己的 resource-bound 不透明凭据。

## Client 注册：Pre-registration、CIMD 与 DCR

ServerFS 支持三种客户端识别方式：

1. **预注册**：通过可选只读 `SERVERFS_AUTH_CLIENTS_FILE` 提供客户端 metadata。
2. **CIMD（Client ID Metadata Documents）**：面向现代标准客户端的首选方式；ServerFS 对 HTTPS、DNS 公网地址、TLS 等做严格验证。
3. **DCR（Dynamic Client Registration）**：仅用于兼容，默认关闭。只有客户端确实要求时才开启：

```text
SERVERFS_AUTH_DCR_ENABLED=true
```

公共客户端使用 Authorization Code + PKCE S256。能使用 CIMD 或显式预注册时，不要仅因为 DCR 可用就开启它。

## Token 与 Resource 模型

ServerFS 签发自己的不透明 access/refresh token；SQLite 中只存摘要，不保存原始凭据值。

- Token 绑定到 `SERVERFS_OAUTH_RESOURCE_URL`；
- Resource Server 通过私有 introspection 校验 issuer、resource、client、subject、scope 与过期时间；
- provider-native token 不能直接访问 MCP；
- authorization code 与 provider state 都是一次性的；
- refresh token 使用后轮换，重放失败关闭；
- 默认 OAuth scope 是 `serverfs:access`，workdir 读写/Agent 权限仍由既有 ServerFS 策略独立控制。

## OpenAI Secure MCP Tunnel

关闭 OAuth 时，现有 Tunnel 路径仍受支持，并已用仓库测试基线 `tunnel-client v0.0.16` 做回归。

如果 `serverfs-auth` 与 MCP resource 不是同一个 origin，需要显式信任精确的 auth origin：

```text
MCP_OAUTH_TRUSTED_ORIGINS=https://auth.example.com
```

### 已知上游限制

截至 2026-10-19，OpenAI tunnel-client issue #78 仍处于 open 状态：ChatGPT 在授权过程中可能把 RFC 9728 的 canonical `resource` 替换成 hosted Tunnel resource。

ServerFS **不会**为了绕过这一问题而放宽 audience/resource 校验、接受通配 resource，或写死未公开的 OpenAI gateway host。

因此：直接/通用 OAuth 与关闭 OAuth 的 Tunnel 是 ServerFS 已验证路径；OAuth-through-Tunnel 仍需要针对实际 OpenAI 行为做 live acceptance，并可能被上述上游 resource rewrite 阻塞。

## Proxy 路由

只有 `serverfs-auth` 需要 provider/CIMD egress：

```text
SERVERFS_AUTH_USE_PROXY=true
SERVERFS_PROXY_HOST=proxy.example.internal
SERVERFS_PROXY_PORT=7890
```

该开关与 Tunnel、Agent、Jev 的 proxy 开关相互独立。

## 备份与恢复

`SERVERFS_AUTH_STATE_HOST_DIR` 保存 SQLite 授权状态。虽然不会持久化原始 access/refresh token，备份仍应按安全敏感数据处理。

一致性备份流程：

1. 在维护窗口只停止 `serverfs-auth`；
2. 保留权限完整复制整个 auth-state 目录；
3. 启动 `serverfs-auth`；
4. 验证 `/healthz`。

恢复时也应先停止 `serverfs-auth`，整体恢复 state 目录，不要拼接不同快照中的 SQLite 文件。

## 撤销与密钥轮换

- 客户端可通过 `/revoke` 撤销 ServerFS 签发的凭据；
- 从 allowlist 移除 principal 会阻止后续准入，但不会自动撤销已经签发的 access token；
- provider client secret 轮换只需更新并 recreate/restart `serverfs-auth`；
- `SERVERFS_OAUTH_INTROSPECTION_SECRET` 只在 `serverfs-mcp` 与 `serverfs-auth` 之间共享，应在同一次维护变更中原子更新并 recreate 两个服务；不一致时会 fail closed；
- 紧急实例级失效可在停止 `serverfs-auth` 后更换整个 OAuth state 目录。这会让所有 ServerFS 凭据失效，同时丢失动态注册/state，只应用于明确的恢复操作。

## 故障排查

**登录前返回 401**：OAuth 开启后没有有效 ServerFS bearer 时属于正常行为。检查 Protected Resource Metadata 与 canonical `SERVERFS_OAUTH_RESOURCE_URL`。

**Auth 服务无法启动**：确认至少启用一个有完整凭据的 provider、public/issuer URL 完全一致、公网使用 HTTPS、introspection secret 已配置、state 目录可写。

**登录成功但 access denied**：稳定 provider subject 不在精确 allowlist 中。不要用邮箱/昵称替代。

**Resource mismatch**：修正 canonical 外部 URL 或客户端请求，不要放宽 audience。Tunnel 场景还应检查前述 resource rewrite 限制。

**Provider 请求超时**：需要时启用 `SERVERFS_AUTH_USE_PROXY`；不要给 `serverfs-mcp` 通用 Internet egress。

**Tunnel 无法发现独立 auth origin**：将 `MCP_OAUTH_TRUSTED_ORIGINS` 设置为精确授权服务器 origin，不要使用通配符。

## 原生平台

Windows 与 macOS 原生 stdio 继续**不需要任何 OAuth 配置**。v0.16 的共享 OAuth 模块已验证可在两平台安全导入，但 OAuth 部署本身仍属于 Linux remote-HTTP shape。

仓库中的权威运维契约是 [`docs/oauth.md`](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/oauth.md)，完整环境变量以版本化的 [`.env.example`](https://github.com/NTLx/ServerFS_MCP/blob/main/.env.example) 为准。
