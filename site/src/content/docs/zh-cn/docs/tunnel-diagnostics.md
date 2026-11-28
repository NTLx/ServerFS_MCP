---
title: Connector 诊断
description: 分别诊断 Core 可达、Connector 注册、公网 MCP 与响应投递。
---

本指南用于入口故障，不要直接重启整个部署。先记录精确 Core/Connector、平台、版本、端点、故障时间及近期变更。直接 HTTP 不存在待诊断的 Connector。

## 1. 定位故障边界

| 检查 | 成功表示 |
| --- | --- |
| Core 监听与认证后的 MCP initialize/tools/list | 本地 Core 和调用方准入正常 |
| 所选 Connector 进程与就绪状态 | 目标本地入口进程运行 |
| Provider 注册、DNS/TLS 和路由 | 外部服务可到达目标路由 |
| 认证后的远程 MCP 与安全读取 | 指定客户端端到端可用 |
| 长连接 SSE / 已完成响应 | 对应会话及投递路径正常 |

按顺序检查，定位首个失败边界。核对实际 Host 与凭据，不泄露密钥。API Key 401、OAuth challenge 或 resource mismatch 见 [API Key](../api-key/)与 [OAuth](../oauth/)，不要扩大白名单或关闭安全机制。

## 2. 检查所选组件

Linux 使用目标 project 已有的完整 env、overlay 和 profile。在 Bash 中保留 [Linux 运维手册](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/deployment/linux/README.md)所述完整 Compose 命令数组，下方以 `deployment` 表示。不要为了诊断另建不完整的部署组合。

```bash
# 根据实际部署选择 openai-tunnel 或 cloudflared。
service=cloudflared
"${deployment[@]}" ps serverfs-mcp "$service"
"${deployment[@]}" logs --tail=100 "$service"
```

macOS 使用命名实例 `status` 或 `owned-status`，确认精确 staged Connector。Windows 使用组件 CLI 的 `status` 并指定 Core 与 Connector ID。原生状态与日志路径见 [macOS](../macos-native/) 或 [Windows](../windows-native/)。Job 已注册或进程正在运行不能证明公网可达。

## 3. Cloudflare

确认 Tunnel 身份、Provider 路由未涉及无关 hostname，以及 origin 从 cloudflared 的网络空间实际可达（Linux 服务 DNS，原生 loopback）。检查凭据文件权限、Provider 注册和出站网络、公网 DNS/TLS、实际转发 Host，最后验证认证后的公网 MCP。Provider 已连接不等于 Core 已授权。见 [Cloudflare Connector](../cloudflare-tunnel/)。

## 4. OpenAI

Linux 命名 HTTP 的健康监听固定为内部 loopback 8080；兼容 Compose 路径根据 `HEALTH_LISTEN_ADDR` 派生端口。原生 job 可能使用动态分配端口，不应假定为 8080。

```bash
"${deployment[@]}" exec openai-tunnel \
  /usr/bin/tunnel-client health --port 8080 --require-control-plane-poll --json
```

该探针要求就绪且 daemon 启动以来至少完成一次成功的 control-plane poll，不能证明持续轮询或结果投递。对于支持详细 health route 的客户端，只查看相关诊断快照（使用实际健康端口及可信本地 HTTP 客户端，已测试的 Alpine 镜像含 wget）。

```bash
"${deployment[@]}" exec openai-tunnel \
  wget -qO- 'http://127.0.0.1:8080/health?details=true'
```

| 证据 | 含义 |
| --- | --- |
| `/healthz` / `/readyz` | 存活 / 启动就绪 |
| `/health/mcp` | 上游 MCP 观测；无观测可能只是没有流量 |
| `/health/control-plane` | 已完成轮询、失败及退避；正在 long poll 属正常 |
| `/health/response-delivery` | 完成结果是否被上游接收，与轮询独立 |
| `/health/queue`、`/health/dispatcher` | 积压、背压与活动操作 |

Component route 可能返回 HTTP 200 但 JSON 显示 degraded。健康路由与参数取决于实际客户端构建；核对其 help 与版本要求。仓库历史 v0.0.17 测试基线并非兼容性保证。 `tunnel-client doctor --explain` 是 preflight，不能替代运行中进程的证据。

MCP 访问失败时检查命名 HTTP upstream URL、精确 Host 与匹配的静态 Core bearer 文件。控制面凭据另行配置。轮询失败时检查 Provider 网络与 Connector 独立代理；该代理不配置镜像拉取。轮询成功但收不到结果时先检查响应投递。命名 OpenAI HTTP 不支持 OAuth-only Core；历史 stdio Tunnel OAuth 限制见 [OAuth 指南](../oauth/)。

## 5. 恢复与验证

在已授权的局部修复前保留证据。按平台流程仅重启出错组件，确认无关 Core、Bridge 与 Connector 身份不变。验证新的远程 MCP 会话，按需另行检查 SSE，不假定旧会话重放。

Agent 排障应只读取相关小型状态快照与近期脱敏日志，不读取完整 env、无限制日志或原始响应载荷。禁止公开健康/管理端口与凭据。报告实测状态、时间、失败边界和剩余不确定性。
