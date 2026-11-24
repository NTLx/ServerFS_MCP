---
title: Windows 原生部署
description: 在 Windows 11 上部署原生 stdio 或独立命名 HTTP 组件。
---

ServerFS 支持 Windows 11 x64、本地 NTFS 与匹配的预构建 Rust/ABI3 native wheel。持久组件由当前用户 Task Scheduler 管理，使用无终端窗口的 launcher 和 Job 进程约束。无需 WSL、VM、SCM 服务或管理员提权。

## 选择合适的模式

| 模式 | 适用场景 |
| --- | --- |
| 本地 stdio | MCP 客户端在本机，无需持久 HTTP 服务 |
| 纯文件 Core | 需要认证后的 HTTP 文件访问，无需 Agent 前置依赖 |
| Owned Bridge | Core 需要专属 Agent 或 Repo Ops 执行边界 |
| Shared Bridge | 多个 Core 显式共享 Bridge；初始配置助手支持最多 16 个具有独立 grant 的 Core |

所有命名 HTTP Core 均可按需接入 Cloudflare 和 OpenAI Connector。Core 管理调用方认证。Agent 与 Repo Ops 根据 Workdir 分别启用；Shared Core 不管理 owner。

## 部署步骤

完整 PowerShell 命令见 [Windows 运维手册](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/windows/v0250-native-operations.md)。

1. 准备可信的已安装 controller，使用匹配的 Core/Native Wheel 与可信 uv 离线缓存。仅在需要 Bridge 时额外准备 Bridge Wheel。
2. 安装新的私有组件运行时，再配置纯文件、owned 或 shared 拓扑。初始配置助手生成独立 bearer 凭据和签名 grant，不启动任何 task。
3. 使用组件 CLI（`python -I -B -m serverfs_mcp.windows_v25.cli`）注册并启动所需 task。先启动 Bridge，再启动依赖它的 Core；先验证认证后的 MCP，再增加 Connector。
4. 如需公网入口，为 Connector 准备独立 ID、可信可执行文件和私有 Provider 凭据。OpenAI 需要静态 Core bearer 认证及匹配的 bundled cloudflared。

v0.25.0 初始配置助手支持 bearer 认证和显式无代理 runtime 策略。v0.26.0 候选版支持通过 owner 私有、无凭据的 `--agent-proxy-env-file` 配置 runtime/ACP `use_proxy=true`；请使用版本匹配的手册。OAuth 和其他特殊配置仍使用高级手动路径。不要混用两条配置流程，也不要绕过私有 ACL 和 reparse-point 检查。

## 验证与运维

检查 task 注册与实际运行状态、匿名拒绝、认证后的 MCP initialize、Workdir 访问及显式启用的能力；另行验证公网可达。启动、停止、重启及卸载均使用精确组件、实例和 Connector ID。

先停入口，再停对应 Core；退役 Bridge 前确认所有依赖 Core 和活跃任务。uninstall 仅移除已验证的 task，不删除私有数据。配置失败保留诊断证据，不得在部分状态上盲目重试。已有部署应采用明确的迁移或回滚方案。

真实 Provider 和 OpenAI 公网验收的当前限制见[发布清单](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.25.0-release-checklist-2026-11-21.md)。
