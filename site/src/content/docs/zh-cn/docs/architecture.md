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
   │ 仅允许精确主机的 HTTPS 出站
   ▼
临时文件主机
```

该 sidecar 为显式可选能力。它不挂载 workdir、不持有 OpenAI/Tunnel 凭据，也不发布端口。MCP 主容器始终不获得互联网出口，只把临时 URL 和字节上限发送给 sidecar。Sidecar 会在返回原始字节前验证精确配置的主机名、解析得到的 IP、TLS 主机名、每次重定向、超时和文件大小上限。

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

## 能力边界

ServerFS 提供窄能力操作，而不是通用执行原语：

- 文件系统读取工具
- 受保护的文件写入
- 有界整文件二进制传输
- 可选且隔离的 ChatGPT 文件入口
- 结构化 Agent task RPC

每种可选能力都有独立门控。
