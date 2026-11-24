---
title: Agent Skills
description: 按任务选择 ServerFS 运维 Skill，并使用仅包含 Skills 的可移植 Agent Plugin。
---

ServerFS 维护七个运维 [Agent Skills](https://github.com/NTLx/ServerFS_MCP/tree/main/skills)，用于指导 Agent 安全使用已有产品接口。Skills 自身不安装 ServerFS 服务、不增加 MCP 工具、不授予权限。

## 选择一个 Skill

| 用户目标 | Skill |
| --- | --- |
| 新建本地或命名持久 Core | `serverfs-install` |
| 已有 Core 与 workdir 策略 | `serverfs-configure` |
| 限定范围的版本切换或显式迁移 | `serverfs-upgrade` |
| 只读优先的故障定位 | `serverfs-diagnose` |
| 指定 OpenAI 或 Cloudflare Connector | `serverfs-tunnel` |
| HTTP 调用方认证 | `serverfs-auth` |
| Host Bridge、grant、runtime 或任务 | `serverfs-agent-bridge` |

Repo Ops 不新增独立 Skill：策略由 `serverfs-configure` 管理，接入由 `serverfs-agent-bridge` 管理，实际已授权仓库操作直接使用 [Repo Ops MCP 工具](../repo-ops/)。不要仅为执行确定性的 Git 操作而委派 Agent。

## 按需加载上下文

1. 读取匹配的 `SKILL.md`，确认安装版本、平台、精确组件 ID、目标与允许变更范围。
2. 按需加载匹配的平台或模式 Reference（若有）。仓库相对路径指向版本匹配的源码 checkout，而非 ZIP 目录。
3. 阅读相关 Runbook 章节、实际配置及受支持的 CLI/tool schema。无法确认时停止在非修改性指导阶段。
4. 使用有界证据，保留无关工作，记录实际验证和未验收事项。

仓库 Skills 索引用于导航与维护；必要的停止条件保留在每个运行时 Skill 中。避免把无关平台流程、历史报告、大范围日志或原始二进制数据加载到 Agent 上下文。

## 操作边界

纯文件服务不需要 Bridge；Agent 和 Repo Ops 独立启用，Shared Core 不能管理 owner。Connector 凭据不等于调用方认证。遵循各平台实际生命周期，包括原生 create-only 安装限制，不擅自原地更新。不用示例覆盖现有策略，不泄露私有状态、不清除 recovery guard，也不把注册成功当作实时就绪。Skills 不替代用户授权或产品本身的权限约束。

## 可移植 Agent Plugin

根目录 `plugin.json` 是 skills-only 的可移植 Agent Plugins manifest。v0.25.0 构建产物为 `serverfs-plugin-v0.25.0.zip`。v0.26.0 候选 Skills 可在本地验证并构建为 `serverfs-plugin-v0.26.0.zip`，但尚未正式发布；发布须通过发布门禁。使用可信、版本匹配的产物，通过实际 Agent 客户端支持的 Plugin 安装方式导入。

ZIP 包含 `serverfs/plugin.json` 和七个 Skill 目录及 Markdown Reference，不含 Skills 索引、完整 checkout、`mcp.json`、实例端点 URL 或凭据。实际 ServerFS MCP 连接需另外配置；ZIP 不会自动复制 Skills 至 Provider 私有目录。

维护者可在版本匹配的 checkout 中使用仓库打包脚本验证和构建：

```bash
python deployment/plugin/package_plugin.py validate --tag v0.26.0
python deployment/plugin/package_plugin.py build --output-dir /absolute/private/output --tag v0.26.0
```

使用 Python 3.12 或更新版本。本地构建/验证不等同于发布或远程服务验收。
