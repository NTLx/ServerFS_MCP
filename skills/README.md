# ServerFS Agent Skills

This directory is the canonical repository source for ServerFS operational Agent Skills.

The skills teach an AI Agent how to choose and execute the **existing** ServerFS installation, configuration, upgrade and diagnostic workflows safely. They are not a second configuration schema, a replacement deployment framework, or a copy of the product documentation.

## Choose the smallest matching skill

| User objective | Skill |
| --- | --- |
| Install ServerFS for the first time | [`serverfs-install`](serverfs-install/SKILL.md) |
| Change workdirs or ordinary ServerFS policy, including v0.23 Linux Repo Ops policy | [`serverfs-configure`](serverfs-configure/SKILL.md) |
| Upgrade an existing deployment | [`serverfs-upgrade`](serverfs-upgrade/SKILL.md) |
| Investigate a failure or verify current health | [`serverfs-diagnose`](serverfs-diagnose/SKILL.md) |
| Configure or troubleshoot the OpenAI Secure MCP Tunnel | [`serverfs-tunnel`](serverfs-tunnel/SKILL.md) |
| Configure remote HTTP API Key or OAuth authentication | [`serverfs-auth`](serverfs-auth/SKILL.md) |
| Configure or troubleshoot the Host Bridge lifecycle, Agent runtimes or Jev | [`serverfs-agent-bridge`](serverfs-agent-bridge/SKILL.md) |

Do not load every skill by default. Start from the user's objective and read the smallest relevant skill. A skill may route to another skill when the task crosses an optional subsystem boundary.

Repo Ops intentionally does **not** have an eighth standalone skill. Its per-workdir/action policy belongs to `serverfs-configure`; the Linux Host Bridge transport/lifecycle it reuses belongs to `serverfs-agent-bridge`; actual repository inspection/action/commit/push uses the deterministic MCP tools directly rather than Agent delegation.

## Source-of-truth rule

The current ServerFS checkout or installed release is authoritative. After locating a ServerFS checkout, resolve these paths from its root and inspect the relevant current sources before changing anything:

- `.env.example` for Linux/Docker environment configuration;
- `serverfs.toml.example` for native Windows/macOS non-secret policy;
- `compose.yml` and optional Compose overlays;
- `deployment/` platform/deployment implementation and guides;
- `site/src/content/docs/docs/` current product documentation;
- the `serverfs` CLI and `serverfs doctor` on the target installation.

Skills intentionally avoid duplicating complete default tables, version pins and configuration schemas that can be read from those sources.

## Execution model

ServerFS skills support two contexts:

### Repository-aware

The Agent is operating inside a ServerFS checkout. Read the checkout before acting and prefer the product's own CLI, parser, Compose and deployment helpers.

### Imported / standalone

The skill has been imported into an Agent without the ServerFS repository. First locate the target ServerFS checkout or deployed installation and establish its version/configuration source. If no authoritative target state is accessible, provide bounded guidance or diagnosis only; do not invent current paths, settings, supported platform claims or commands.

## Shared safety contract

All ServerFS operational skills follow these rules:

1. Detect the target platform and deployment family before selecting commands.
2. Read existing configuration before editing it.
3. Use merge/surgical changes; never replace a populated `.env` or `serverfs.toml` with an example file.
4. Change only settings required by the explicit objective.
5. Keep credentials, provider secrets and private state out of tracked files, exposed workdirs and normal logs.
6. Preserve current-user/non-elevated ownership. Do not introduce root, Windows Administrator/LocalSystem, or root LaunchDaemon shortcuts.
7. Prefer official `serverfs` CLI, Compose and `deployment/*` helpers over ad-hoc shell implementations.
8. Verify every mutation through a supported parser, `doctor`, Compose render or platform service/status surface.
9. Stop on failed validation or ambiguous ownership/state rather than layering speculative changes.
10. Never silently enable optional Tunnel, HTTP Auth, Repo Ops, or Agent delegation during an unrelated install/upgrade.
11. On v0.23+, treat Repo Ops as deterministic non-Agent authority: use its fixed public surface when authorized and available; never substitute Agent delegation merely to perform mechanical Git/action/commit/push work.

## Plugin distribution

The repository root contains the portable Agent Plugin manifest, `plugin.json`. Stable ServerFS releases package the seven runtime skill directories and their per-skill `references/` files into one release asset named:

```text
serverfs-plugin-vX.Y.Z.zip
```

The ZIP is a distribution wrapper only. The repository `skills/` tree remains the source of truth, and skill/plugin content follows the ServerFS release version rather than maintaining independent versions.

The Plugin intentionally does not bundle an MCP endpoint, instance credentials or provider-private configuration. Operators connect or expose their actual ServerFS deployment separately.
