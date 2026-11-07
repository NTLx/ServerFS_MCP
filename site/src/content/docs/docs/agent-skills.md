---
title: Agent Skills
description: Use ServerFS repository skills to guide AI Agents through safe installation, configuration, upgrade, diagnosis and optional subsystem operations.
---

> **Current stable release: v0.24.0.** All seven operational Agent Skills now include v0.24.1 candidate guidance. The v0.24.1 Plugin packaging is unreleased; the published Plugin remains the v0.24.0 asset.

ServerFS keeps operational Agent Skills in the repository [`skills/`](https://github.com/NTLx/ServerFS_MCP/tree/main/skills) directory. They teach an Agent **how to use the existing ServerFS product interfaces safely**; they are not another configuration format or deployment implementation.

## Seven task-oriented skills

| Objective | Skill |
| --- | --- |
| First installation | `serverfs-install` |
| Workdir and ordinary policy changes, including v0.23 Repo Ops policy | `serverfs-configure` |
| Existing deployment upgrade | `serverfs-upgrade` |
| Read-first diagnosis and health verification | `serverfs-diagnose` |
| OpenAI Secure MCP Tunnel, Linux Cloudflare stable, or native macOS Cloudflare (v0.24.1 candidate) | `serverfs-tunnel` |
| Remote HTTP API Key / OAuth | `serverfs-auth` |
| Host Bridge lifecycle / Codex / Claude / Qoder / ACP / Jev | `serverfs-agent-bridge` |

The published v0.24.0 release retains exactly **seven** Skills. Linux Cloudflare support is a branch inside the existing `serverfs-tunnel` Skill; the unreleased v0.24.1 native macOS candidate guidance is also included in the existing Skills and their references, not as an eighth Skill. Candidate macOS evidence covers isolated read-only staging only; it does not establish production workdir/Agent acceptance. Native Windows Cloudflare remains planned for v0.24.2. Plugin validation passed for the unreleased v0.24.1 candidate; the published Plugin remains v0.24.0. Exact-SHA CI and stable release remain pending.

The repository-level `AGENTS.md` contains only a thin index. An Agent should load the smallest matching `SKILL.md` rather than placing all operational procedures in its base context.

Repo Ops intentionally does not add an eighth Skill. Its per-workdir/action policy is handled by `serverfs-configure`, its platform-native Bridge transport/lifecycle by `serverfs-agent-bridge`, and its actual repository operations by the deterministic public MCP tools rather than Agent delegation.

## Source of truth

Skills deliberately refer back to the current checkout or installed release:

- `.env.example` for Linux/Docker configuration;
- `serverfs.toml.example` for native non-secret policy;
- Compose files and `deployment/*` helpers;
- the current `serverfs` CLI and `serverfs doctor`;
- the platform/product documentation on this site.

Before mutation, the Agent must identify the platform, deployment owner and current configuration. Existing `.env` and `serverfs.toml` files are merged/surgically edited; example files are never used to overwrite operator configuration.

## Safety model

The skills preserve the same ServerFS invariants as manual operation:

- current-user/non-elevated lifecycle;
- read-only workdirs unless explicitly widened;
- separate opt-in boundaries for Tunnel, HTTP Auth, v0.23 Repo Ops and Agent delegation;
- deterministic Repo Ops remains non-Agent and is never replaced by Agent submission merely to perform mechanical Git/action/commit/push steps;
- secrets outside tracked files, exposed workdirs and normal logs;
- validation after every configuration mutation;
- stop rather than guess when platform, ownership or active state is ambiguous.

## Portable Agent Plugin

The repository root contains the portable Agent Plugins manifest `plugin.json`. The currently published v0.24.0 GitHub Release packages the seven runtime skills and their references as this asset:

```text
serverfs-plugin-v0.24.0.zip
```

The ZIP is a **skills-only** Plugin. It does not bundle `mcp.json`, a ServerFS instance URL, credentials, or provider-private settings. The v0.24.0 Plugin was published after the successful exact-SHA cross-platform release gate; it contains the same seven Skill directories and references, including the Linux Cloudflare Tunnel reference. Plugin and skill content follow the ServerFS release version; there is no independent skill version lifecycle.

The Git repository remains canonical. ServerFS does not automatically copy these skills into `~/.codex/skills`, Claude configuration, or other provider-private directories.
