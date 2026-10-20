---
title: Agent Skills
description: 使用 ServerFS 仓库内置 Skills，指导 AI Agent 安全完成安装、配置、升级、诊断和可选子系统运维。
---

> **当前版本：v0.18.0。** ServerFS 包含 7 个运维 Agent Skills 和一个 skills-only portable Agent Plugin。

ServerFS 在仓库 [`skills/`](https://github.com/NTLx/ServerFS_MCP/tree/main/skills) 目录中维护运维 Agent Skills。它们告诉 Agent **如何安全使用 ServerFS 已有的正式产品接口**，而不是再建立一套配置格式或部署实现。

## 7 个目标型 Skill

| 用户目标 | Skill |
| --- | --- |
| 首次安装 | `serverfs-install` |
| Workdir 与普通策略配置 | `serverfs-configure` |
| 已有部署升级 | `serverfs-upgrade` |
| 只读优先的诊断与健康核验 | `serverfs-diagnose` |
| OpenAI Secure MCP Tunnel | `serverfs-tunnel` |
| 远程 HTTP API Key / OAuth | `serverfs-auth` |
| Agent Bridge / Codex / Claude / Qoder / Jev | `serverfs-agent-bridge` |

仓库根 `AGENTS.md` 只保留很薄的索引。Agent 应根据具体任务加载最小匹配的 `SKILL.md`，而不是把全部运维流程长期塞进基础上下文。

## 单一事实源

Skills 会回到当前 checkout 或当前安装版本读取事实：

- Linux/Docker 配置以 `.env.example` 为版本化参考；
- Windows/macOS 原生非敏感策略以 `serverfs.toml.example` 为参考；
- Compose 文件和 `deployment/*` 辅助工具；
- 当前版本的 `serverfs` CLI 与 `serverfs doctor`；
- 本站当前平台和产品文档。

任何修改前，Agent 都必须先识别平台、部署所有者和正在生效的配置。已有 `.env`、`serverfs.toml` 只能合并或进行最小化修改，不能用示例文件整体覆盖。

## 安全模型

Skills 继续保持 ServerFS 手工运维时的核心约束：

- 当前用户、非提权的生命周期；
- Workdir 默认只读，除非用户明确扩大权限；
- Tunnel、HTTP Auth、Agent delegation 分别独立显式启用；
- Secret 不进入受版本控制文件、暴露的 Workdir 或普通日志；
- 配置修改后必须经过正式解析器、doctor、Compose 或平台状态接口验证；
- 平台、所有权或运行状态不明确时停止，而不是猜测。

## Portable Agent Plugin

仓库根目录维护 portable Agent Plugins manifest：`plugin.json`。v0.18.0 将 7 个运行时 Skill 及其参考文件统一打包成一个 GitHub Release 资产：

```text
serverfs-plugin-v0.18.0.zip
```

该 ZIP 是 **skills-only Plugin**，不会内置 `mcp.json`、某个 ServerFS 实例 URL、凭据或 Provider 私有配置。Plugin 与 Skills 都直接跟随 ServerFS 项目版本，不建立独立版本生命周期。

Git 仓库始终是唯一源码。ServerFS 不会自动把这些 Skills 复制到 `~/.codex/skills`、Claude 配置目录或其他 Provider 私有目录。
