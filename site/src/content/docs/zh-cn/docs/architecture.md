---
title: 架构
description: ServerFS 的数据路径与信任边界。
---

## 基础路径

```text
Linux 文件系统
   │
   ▼
Docker bind mounts
   │  默认只读
   ▼
ServerFS MCP
   │  仅内部网络
   ▼
OpenAI Secure MCP Tunnel
   │  仅出站
   ▼
ChatGPT
```

MCP 容器不发布端口，也没有互联网出口。OpenAI Tunnel 使用独立的出站网络访问控制面。

## 可选 ChatGPT 文件入口路径

```text
ChatGPT 文件参数
   │
   │ 临时 HTTPS URL
   ▼
ServerFS MCP
   │ 仅专用内部网络
   ▼
serverfs-file-ingress
   │ 仅允许经策略校验的 HTTPS/443 出站
   ▼
临时文件主机
```

该 sidecar 为显式可选能力。它不挂载 workdir、不持有 OpenAI/Tunnel 凭据，也不发布端口。MCP 主容器始终不获得互联网出口，只把临时 URL 和字节上限发送给 sidecar。主机授权可以来自管理员配置的精确主机名，也可以来自单独开启、根据真实 ChatGPT 文件参数实测得到的受限 OpenAI Azure Blob 账户家族；通用通配符会被拒绝。随后 Sidecar 继续校验 DNS 结果必须全部可公网路由，将连接固定到已验证 IP 并按原始主机名校验 TLS，对每次重定向重新校验，并执行超时和字节上限后才返回原始字节。

## 可选 Agent 路径

```text
ServerFS MCP 容器
   │
   │ 只读 Unix socket 挂载
   ▼
宿主机 Agent Bridge
   │
   ├── Codex
   └── Claude Code
```

Bridge 以用户级宿主机服务运行，并在仓库 checkout 之外维护自己的 runtime socket、锁、已安装 release、配置与状态。

### 可选 Jev advisory 路径

```text
Agent task / provider approval
   │
   ▼
宿主机 Agent Bridge
   │  最小化结构化 state
   ▼
TypeSafe Jev API
   │  probabilities / confidence / Noul
   ▼
仅作为 advisory result
```

Jev 从宿主机 Bridge 发起调用，不运行在 MCP 容器中。Task Preflight 与 Runtime Router 共用一次任务提交请求；Approval Advisor 只在 provider 真正创建 approval request 后运行。这些输出不会获得授权权力，也不会自动选择 runtime。

## 能力边界

ServerFS 提供窄能力操作，而不是通用执行原语：

- 文件系统读取工具
- 受保护的文件写入
- 有界整文件二进制传输
- 可选且隔离的 ChatGPT 文件入口
- 结构化 Agent task RPC
- 可选的宿主机 Jev advisory decision

当前 v0.7.2 稳定版支持 11 / 13 / 20 / 22 个工具的四种 MCP 能力面。每种可选能力都有独立门控；Jev 是 Agent Bridge 内部 advisor，不是新的 MCP capability surface，因此不会改变这些工具数量。
