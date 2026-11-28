# ServerFS documentation

Start with the [English README](../README_EN.md) or [简体中文 README](../README.md). The current product baseline is v0.26.0; see the [release notes](releases/v0.26.0.md). For the actual acceptance and publication status, consult the [release checklist](v0.26.0-release-checklist.md) and [evidence ledger](releases/v0.26.0-evidence.md).

## Active development (v0.27.0)

- [Accepted platform independence ADR](architecture/v0.27.0-platform-independence.md) — binding target architecture and three independent implementation boundaries.
- [Approved v0.27.0 development plan](plans/dev_plan_v0.27.0.md) — split-first sequence, mandatory reliability fixes, MCP integration scope and release gates.

These documents govern v0.27.0 development, not current v0.26.0 operations or acceptance. Start at [AGENTS.md](../AGENTS.md). The owning development trees are [Linux](../linux/), [macOS](../macos/), and [Windows](../windows/); run their own build/test commands from those directories.

## Operations

- [Linux Docker deployment](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/deployment/linux/README.md), [macOS native deployment](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/deployment/macos/README.md), [Windows native deployment](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/docs/deployment/windows/v0250-native-operations.md).
- [Windows named Agent proxy](deployment/windows/v0260-named-agent-proxy.md), [Windows Cloudflare](windows-cloudflare-tunnel.md), [Cloudflare Connector](cloudflare-tunnel.md).
- [Static API Key](api-key.md), [OAuth](oauth.md), [host Agent Bridge](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/agent_bridge/README.md), [operational Skills](../skills/README.md).
- [English website docs](https://ntlx.github.io/ServerFS_MCP/docs/) · [中文网站文档](https://ntlx.github.io/ServerFS_MCP/zh-cn/docs/).

## Engineering and release history

| Directory | Purpose |
| --- | --- |
| [`plans/`](plans/) | Version-specific development plans and historical design/discovery records; not current operational authority. |
| [`architecture/`](architecture/) | Architecture decisions, platform and protocol contracts. |
| [`deployment/`](deployment/) | Standalone platform-specific operational documents; component README entry points stay beside their implementation. |
| [`maintainers/`](maintainers/) | Maintainer invariants, workflow and validation requirements. |
| [`releases/`](releases/) | Versioned release notes and evidence ledgers. |
| [`diagnostics/`](diagnostics/) | Diagnostic reports, operator taskbooks and acceptance evidence. |

Existing top-level acceptance/checklist records under `docs/` retain their established paths and historical scope. Historical documents retain their original technical claims and release status. Resolve current behavior using the implementation, tests and version-matched operational guides.

Repository root retains `AGENTS.md`, `CLAUDE.md`, bilingual README and license. Component README entry points, executable/configuration inputs, tool-specific state, the canonical `skills/` package and Astro's `site/src/content/docs/` source tree remain at their required locations. Put new standalone project documentation in the appropriate `docs/` category; do not scatter version plans or acceptance reports at repository root.
