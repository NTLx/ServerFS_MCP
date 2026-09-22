---
title: 安全模型
description: MCP、Docker、文件系统、传输与 Agent 边界上的纵深防御。
---

ServerFS 围绕**窄能力与独立执行层**设计。

## 核心属性

- 默认只读。
- 按 workdir 显式开放写入。
- 无 Shell 或通用命令执行。
- 无通用 `write_file`。
- 无递归删除或 force 模式。
- 破坏性操作受 revision 保护。
- 基于文件描述符的路径遍历，并拒绝 symlink。
- 内建凭据 deny 规则。
- 读 / 写 / 搜索 / 二进制大小限制。
- 结构化审计日志，不记录文件内容。
- 容器根文件系统只读。
- 非 root 容器用户、drop capabilities、no-new-privileges。
- MCP 仅在内部网络，无公开端口。

## 传输层加固

v0.4.0 为 MCP Streamable HTTP 启用了 DNS-rebinding protection。

传输层只接受固定内部 authority `serverfs-mcp:8000`；异常或缺失 Host 会被拒绝，非空且未允许的 Origin 也会在进入 MCP dispatch 前被拒绝。

## Agent 边界

Agent 支持是 opt-in，并位于宿主机 Unix socket 之后。MCP 容器获得的是结构化 Agent 能力，而不是宿主机 Shell。

完整 threat model 与实现细节请查看仓库 [Security Model](https://github.com/NTLx/ServerFS_MCP#security-model)。
