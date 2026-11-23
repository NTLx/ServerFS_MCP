---
title: 静态 API Key
description: 通过独立共享 bearer 凭据保护 HTTP Core。
---

单个操作者或少量可信客户端可选择 API Key。它准入共享密钥持有者，不区分个人身份；Workdir 授权不变。需要身份联合或可独立撤销凭据时，选择 [OAuth](../oauth/)。

## 配置

每个 Core 使用独立高熵随机密钥。Linux 在私有 env 中设置 `SERVERFS_HTTP_API_KEY`；macOS 命名 HTTP 使用私有 staging 输入；Windows 初始配置生成受保护的密钥。按所选平台手册操作。API Key 与 OAuth 互斥，原生 stdio 均不需要。

公网访问要求 HTTPS 与精确 Host/Origin 准入。Connector 凭据与调用方凭据分离；命名 OpenAI HTTP Connector 还需要 Core 的 bearer header。

## 连接

优先使用 Header：

```http
Authorization: Bearer <key>
```

仅接受一行 URL 的客户端可使用兼容方式：

```text
https://mcp.example.com/mcp?key=<key>
```

每个请求只发送一种凭据。缺失、错误、空值、重复或混用凭据返回 HTTP 401，不触发 OAuth 登录。优先使用 Header：URL 密钥可能泄漏到反向代理日志、监控或浏览器历史。不得公开真实密钥。

## 验证与维护

验证无密钥和错误密钥的拒绝，再检查认证后的 MCP initialize、预期工具、安全文件访问及相关拒绝操作。公网入口需要另行远程验证。

只有一个有效密钥，不提供新旧并行缓冲。Linux 轮换需重建所选 Core，更新客户端和受影响的 OpenAI bearer 文件/Connector。命名原生 staging 仅用于新建；采用明确的迁移或重新配置计划，而非直接修改私有 manifest。

平台选择、凭据处理与轮换细节见 [API Key 运维手册](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/api-key.md)。
