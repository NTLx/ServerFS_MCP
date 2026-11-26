---
title: ServerFS MCP
description: 从纯文件 Core 开始，按需选择入口和宿主机执行能力。
---

ServerFS MCP 为 AI 客户端提供对显式配置的本地目录（Workdir）的受控访问。默认只读；写入需要明确策略和 revision 检查。可选能力包括有界二进制传输、确定性 Repo Ops 和宿主机 Agent 委托，不提供通用 Shell。

**当前稳定版本：[v0.26.0](https://github.com/NTLx/ServerFS_MCP/releases/tag/v0.26.0)。** 请使用版本匹配的安装产物与手册。本版本恢复多授权 Workdir，扩展签名 v2 的任务发现与审批审计，并支持 Windows 命名 Runtime 代理策略。

## 从这里开始

1. 阅读[架构](./architecture/)，理解独立 Core、可选 Connector 和可选 Agent Bridge，以及各平台支持边界。
2. 按[快速开始](./getting-started/)选择宿主机上的最小部署。Linux 使用 Docker Compose；[macOS](./macos-native/)和 [Windows](./windows-native/)遵循原生部署流程。
3. 依据[配置](./configuration/)与[安全](./security/)设定精确 Workdir、访问策略和 HTTP 准入。先验证 Core，再增加依赖组件。

## 按需增加组件

| 需求 | 指南 |
| --- | --- |
| 远程或本地直接 HTTP，无 Tunnel | [快速开始](./getting-started/) |
| Cloudflare 公网入口 | [Cloudflare Connector](./cloudflare-tunnel/) |
| OpenAI 入口 | [快速开始](./getting-started/)与 [Tunnel 诊断](./tunnel-diagnostics/) |
| HTTP 调用方认证 | [API Key](./api-key/)或 [OAuth](./oauth/) |
| 有界整文件传输 | [二进制传输](./binary-transfer/) |
| 确定性 Git 与命名仓库动作 | [Repo Ops](./repo-ops/) |
| 原生或 ACP Agent 任务 | [Agent Bridge](./agent-bridge/)与 [ACP](./acp/) |
| 可选建议能力 | [Jev 建议器](./jev-advisors/) |

Connector 不拥有 Core 或 Bridge 的生命周期。每个签名 Bridge 接入需要显式 Core grant；共享 Core 不管理 Bridge owner。Agent 和 Repo Ops 按 Workdir 分别启用。

## 能力组合

工具集合由已启用的能力决定，入口方式不会增加文件或 Agent 权限。

| 能力组合 | 工具数 |
| --- | ---: |
| 文件系统 | 11 |
| 文件系统 + 二进制 | 13 |
| 文件系统 + Repo Ops | 15 |
| 文件系统 + 二进制 + Repo Ops | 17 |
| 文件系统 + Agent | 22 |
| 文件系统 + 二进制 + Agent | 24 |
| 文件系统 + Agent + Repo Ops | 26 |
| 完整可选能力 | 28 |

Agent 工具包含 `list_agent_tasks`；有效任务发现需要支持该方法的签名 v2 接入和已授权别名。工具已注册不代表每个平台和协议均支持所有操作。

## 运维与维护

使用 [Agent Skills](./agent-skills/)指导版本匹配的安装、配置、诊断、升级与组件操作。特定部署场景可参阅[宿主机可观测性](./host-observability/)与[受限网络 ARM64](./restricted-network-arm64/)。不要将历史兼容命令当作当前命名实例流程。精确发布版本及实机验收另见[发布清单](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.26.0-release-checklist.md)。
