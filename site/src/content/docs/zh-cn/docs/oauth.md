---
title: OAuth 2.1
description: 通过独立授权服务准入具有明确身份的 MCP 调用方。
---

OAuth 是[静态 API Key](../api-key/)的替代方式，解决谁可以进入 HTTP Core；操作权限仍由 Workdir 和 Bridge 策略决定。本地 stdio 两者均不需要。

## 选择部署方式

配套授权服务 `serverfs-auth` 使用 Linux Docker Compose 部署，负责身份 Provider 凭据、OAuth 状态及显式准入。Core 通过私有 introspection 校验 ServerFS 签发的 resource-bound Token。

原生 HTTP Core 可使用独立配置的授权服务，原生安装器不部署该服务。Windows 初始配置仅支持 bearer，因此 OAuth 使用手动路径。命名 OpenAI HTTP Connector 要求静态 Core bearer，不属于 OAuth 部署路径。

## 配置准入

精确 env、状态目录准备、Compose 组件级命令及原生配置见 [OAuth 运维手册](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/oauth.md)。

1. 选择精确 Core 与 canonical 公网 HTTPS MCP resource URL。
2. 准备独立 HTTPS 授权 origin 与私有 auth state 目录。Issuer 必须等于 auth public URL；introspection 仅通过可信私有路径访问。
3. 选择 Provider，登记回调地址，配置真实凭据和稳定身份白名单。
4. 先启用授权服务并确认就绪，再启动 Core。保留已授权的组件选择；OAuth 不要求 Tunnel 或 Bridge。

| Provider | 白名单 Principal |
| --- | --- |
| GitHub | `github:<numeric-id>` |
| 钉钉 | `dingtalk:<unionId>` |
| 飞书/Lark | `feishu:<open_id>` |
| 微信开放平台网站应用 | `wechat:<unionid>` |

准入默认拒绝，使用精确稳定 subject，而非显示名称或邮箱。公网使用前完成真实 Provider 登录。优先使用 CIMD 或显式公共客户端预注册，DCR 仅用于兼容。Authorization Code 使用 PKCE S256。

## 验证与维护

检查公网 TLS、OAuth discovery、未认证 challenge、允许和拒绝身份登录、认证后 MCP 及错误 resource/已撤销 Token 的拒绝。健康检查不等于验收，不得为适配客户端而放宽 resource 或绕过 introspection。

移除 principal 阻止新准入，但不会撤销已有 access/refresh Token pair。已认证客户端可通过 `/revoke` 撤销其凭据对；Provider 密钥变更不会撤销 ServerFS Token。Introspection 密钥轮换需两端协调；备份恢复保持整个私有 state 目录。恢复历史状态可能使原有 Token 重新有效。恢复、代理配置和旧 OpenAI 兼容路径限制详见运维手册。
