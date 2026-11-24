---
title: Agent Skills
description: Choose a ServerFS operational Skill and install the portable skills-only Agent Plugin.
---

ServerFS maintains seven operational [Agent Skills](https://github.com/NTLx/ServerFS_MCP/tree/main/skills). They guide safe use of existing product interfaces; they do not install a ServerFS service by themselves, add MCP tools or grant permissions.

## Select one Skill

| Objective | Skill |
| --- | --- |
| New local or named persistent Core | `serverfs-install` |
| Existing Core/workdir policy | `serverfs-configure` |
| Scoped version switch or explicit migration | `serverfs-upgrade` |
| Read-first fault isolation | `serverfs-diagnose` |
| Selected OpenAI or Cloudflare Connector | `serverfs-tunnel` |
| HTTP caller admission | `serverfs-auth` |
| Host Bridge, grants, runtimes or tasks | `serverfs-agent-bridge` |

Repo Ops has no separate Skill: configure its policy with `serverfs-configure`, manage attachment with `serverfs-agent-bridge`, and perform authorized repository operations directly with the [Repo Ops MCP tools](../repo-ops/). Do not delegate deterministic operations merely to invoke Git.

## Load only what the task needs

1. Read the matching `SKILL.md`; identify installed version, platform, exact component IDs, objective and allowed changes.
2. Load the applicable packaged platform/mode reference, if provided. Resolve checkout-relative paths against matching repository source, not the ZIP directory.
3. Read the relevant runbook section, actual configuration and supported CLI/tool schema. If those cannot be established, stop at non-mutating guidance.
4. Use bounded evidence, preserve unrelated work and record actual verification and missing gates.

The repository Skills index is for navigation and maintenance; the essential stop conditions live within each runtime Skill. Avoid loading unrelated platform procedures, historical reports, broad logs or raw binary data into the Agent context.

## Operational boundary

File-only needs no Bridge. Agent and Repo Ops are independent; a Shared Core cannot operate its owner. Connector credentials are not caller authentication. Respect each platform's supported lifecycle, including native create-only provisioning rather than speculative in-place updates. Never overwrite existing policy with an example, disclose private state, clear recovery guards, or treat registration as proof of live readiness. Skills do not override user authorization or the actual product enforcement.

## Portable Agent Plugin

The root `plugin.json` is the skills-only portable Agent Plugins manifest. For release v0.25.0 the build produces `serverfs-plugin-v0.25.0.zip`. v0.26.0 candidate Skills are not a published artifact; package them only after the version metadata and release gates are reconciled. Use a trusted version-matched artifact and the importing Agent client's own supported Plugin installation procedure.

The archive contains `serverfs/plugin.json` and the seven Skill directories with Markdown references. It excludes the Skills index, full checkout, `mcp.json`, endpoint URLs and credentials. Configure the actual ServerFS MCP connection separately. The archive does not auto-copy Skills into provider-private directories.

Maintainers can validate and build from the matching checkout using the repository packaging script:

```bash
python deployment/plugin/package_plugin.py validate --tag v0.25.0
python deployment/plugin/package_plugin.py build --output-dir /absolute/private/output --tag v0.25.0
```

Use Python 3.12 or later. Local build/validation is not release publication or proof of remote service acceptance.
