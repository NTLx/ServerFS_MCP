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

MCP 容器不发布端口，也没有互联网出口。OpenAI Tunnel 容器是唯一向外连接的组件。

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
- 结构化 Agent task RPC

每种可选能力都有独立门控。
