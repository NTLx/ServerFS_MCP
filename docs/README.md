# ServerFS documentation

Start with the [English README](../README_EN.md) or [简体中文 README](../README.md). The current product baseline is v0.26.0; see the [release notes](releases/v0.26.0.md). For the actual acceptance and publication status, consult the [release checklist](v0.26.0-release-checklist.md) and [evidence ledger](releases/v0.26.0-evidence.md).

## Operations

- [Linux Docker deployment](../deployment/linux/README.md), [macOS native deployment](../deployment/macos/README.md), [Windows native deployment](deployment/windows/v0250-native-operations.md).
- [Windows named Agent proxy](deployment/windows/v0260-named-agent-proxy.md), [Windows Cloudflare](windows-cloudflare-tunnel.md), [Cloudflare Connector](cloudflare-tunnel.md).
- [Static API Key](api-key.md), [OAuth](oauth.md), [host Agent Bridge](../agent_bridge/README.md), [operational Skills](../skills/README.md).
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
