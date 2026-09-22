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
- MCP 仅连接内部网络，无公开端口、无互联网出口。
- 可选 ChatGPT 文件入口被隔离在独立 sidecar 中，不挂载 workdir，也不持有 OpenAI 凭据。

## 文件入口边界

v0.5.0 继续禁止 ServerFS MCP 主容器访问互联网。显式启用后，`serverfs-file-ingress` 使用独立出站网络，并通过只与 MCP 服务共享的专用内部网络接收请求；Tunnel 不连接该 ingress 网络。

MCP 到 sidecar 的端点固定为内部 Compose 服务，MCP 客户端本身不会跟随重定向。Sidecar 只接受 443 端口上的 HTTPS。主机授权可以是管理员配置的精确主机名，也可以显式开启根据真实 ChatGPT fileParams 实测得到的受限 OpenAI Azure Blob 家族：存储账户名以 `oaisdmntpr` 开头、仅含小写 ASCII 字母/数字、满足 Azure 账户长度上限，并使用精确的 `.blob.core.windows.net` 后缀；通用主机名通配符仍会被拒绝。主机授权之后仍会拒绝任何非公网 DNS 结果，将连接固定到已验证 IP，同时按原始主机名校验 TLS，对每次上游重定向重新校验，并独立限制字节数和超时。它不是通用 URL 代理。

## 传输层加固

v0.4.0 引入了 MCP Streamable HTTP DNS-rebinding protection；v0.5.0 保持这一传输边界不变。

传输层只接受固定内部 authority `serverfs-mcp:8000`；异常或缺失 Host 会被拒绝，非空且未允许的 Origin 也会在进入 MCP dispatch 前被拒绝。

## Agent 边界

Agent 支持是 opt-in，并位于宿主机 Unix socket 之后。MCP 容器获得的是结构化 Agent 能力，而不是宿主机 Shell。

完整 threat model 与实现细节请查看仓库 [Security Model](https://github.com/NTLx/ServerFS_MCP#security-model)。
