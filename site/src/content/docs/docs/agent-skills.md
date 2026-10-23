---
title: Agent Skills
description: Use ServerFS repository skills to guide AI Agents through safe installation, configuration, upgrade, diagnosis and optional subsystem operations.
---

> **Prepared next maintenance release: v0.22.1 (not published).** ServerFS includes seven operational Agent Skills and one skills-only portable Agent Plugin.

ServerFS keeps operational Agent Skills in the repository [`skills/`](https://github.com/NTLx/ServerFS_MCP/tree/main/skills) directory. They teach an Agent **how to use the existing ServerFS product interfaces safely**; they are not another configuration format or deployment implementation.

## Seven task-oriented skills

| Objective | Skill |
| --- | --- |
| First installation | `serverfs-install` |
| Workdir and ordinary policy changes | `serverfs-configure` |
| Existing deployment upgrade | `serverfs-upgrade` |
| Read-first diagnosis and health verification | `serverfs-diagnose` |
| OpenAI Secure MCP Tunnel | `serverfs-tunnel` |
| Remote HTTP API Key / OAuth | `serverfs-auth` |
| Agent Bridge / Codex / Claude / Qoder / ACP / Jev | `serverfs-agent-bridge` |

The repository-level `AGENTS.md` contains only a thin index. An Agent should load the smallest matching `SKILL.md` rather than placing all operational procedures in its base context.

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
- separate opt-in boundaries for Tunnel, HTTP Auth and Agent delegation;
- secrets outside tracked files, exposed workdirs and normal logs;
- validation after every configuration mutation;
- stop rather than guess when platform, ownership or active state is ambiguous.

## Portable Agent Plugin

The repository root contains the portable Agent Plugins manifest `plugin.json`. The planned v0.22.1 publication packages the seven runtime skills and their references into an expected GitHub Release asset:

```text
serverfs-plugin-v0.22.1.zip (expected asset; not yet published)
```

The ZIP is a **skills-only** Plugin. It does not bundle `mcp.json`, a ServerFS instance URL, credentials, or provider-private settings. Plugin and skill content follow the ServerFS release version; there is no independent skill version lifecycle.

The Git repository remains canonical. ServerFS does not automatically copy these skills into `~/.codex/skills`, Claude configuration, or other provider-private directories.
