---
title: Configuration
description: Configure a Core, its workdirs and optional capabilities.
---

Each Core has its own configuration. Workdir policy is resolved at startup. Choose the platform's configuration format; the security outcomes are shared, not the syntax.

## 1. Choose the configuration format

| Platform | Core policy | Persistent HTTP and components |
| --- | --- | --- |
| Linux | Private env + Compose; up to 16 slots | Named project, selected exposure and Bridge overlays; optional Connector profiles |
| macOS / Windows | Native TOML; up to 64 workdirs | Private HTTP admission inputs, named service manifests and separate Bridge/Connector configuration |

Use the version-matched [.env.example](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/.env.example) for Linux and [serverfs.toml.example](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/serverfs.toml.example) for native policy. These are reference templates: replace all active example paths and keep private inputs outside every exposed workdir.

## 2. Define the smallest necessary workdir

Linux env:

```dotenv
WORKDIR_01_ALIAS=docs
WORKDIR_01_PATH=/srv/serverfs-blue/docs
WORKDIR_01_READ_ONLY=true
```

Native TOML (macOS example; use a real native Windows path on Windows):

```toml
[[workdirs]]
alias = "docs"
path = "/Users/operator/ServerFS-data/docs"
read_only = true
```

Aliases are unique, begin with a letter and contain up to 32 letters, digits, underscores or hyphens. Paths must be existing authorized directories. Do not expose the private service root, credential directory or an entire home directory by convenience. A writable workdir requires both explicit policy and OS write permission. Linux `READ_ONLY` also controls the bind mount.

## 3. Understand policy inheritance

| Setting | Linux env | Native TOML |
| --- | --- | --- |
| Hidden files, read/write limits, binary transfer | Global defaults, slot overrides | Shared `[defaults]` for all workdirs |
| Extra deny globs / built-in deny override | Supported; extra globs union global and slot rules | Built-in deny retained; these override keys are not accepted |
| Read-only, Agent mode/runtimes, Repo Ops | Per slot | Per `[[workdirs]]` |

Linux empty scalar overrides inherit; explicit values take precedence. Repo Ops defaults off per slot. An explicitly disabled Agent mode with an empty runtime list resets that list. Native TOML rejects unknown keys; do not copy Linux slot fields into it. Keep global Agent defaults disabled and opt in only on intended workdirs.

Default limits include 512 KiB / 500 lines per text-read page, 1 MiB per text mutation and 8 MiB per whole-file binary transfer. The Linux binary limit is a default, not an unchangeable ceiling: an explicit slot override replaces it for direct transfer. File ingress additionally enforces its global ceiling. See [Binary Transfer](../binary-transfer/).

## 4. Configure HTTP admission separately

For Linux, choose internal-only HTTP or direct publication using the [Linux runbook](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/deployment/linux/README.md). The Core listener is independent of the published host port. Empty Linux Host policy derives `serverfs-mcp:<HTTP_PORT>`; admit only the actual authorities needed. Empty Origin policy rejects nonempty Origin. Preserve DNS-rebinding protection and use HTTPS for public access.

Named native HTTP admission requires an exact configured hostname and loopback authority; follow the [macOS](../macos-native/) or [Windows](../windows-native/) runbook rather than the Linux env template.

Choose one caller authentication mode:

- [Static API Key](../api-key/): prefer the Authorization header. Query credentials can leak through URL logs.
- [OAuth](../oauth/): use an independently configured authorization service and private introspection.

Linux defaults both off, but do not publish an unauthenticated Core. Native HTTP requires one mode. Local stdio does not use HTTP authentication. Connector credentials are separate; the named OpenAI HTTP Connector requires static bearer admission.

## 5. Enable capabilities only when needed

| Capability | Policy and prerequisite |
| --- | --- |
| [Binary transfer](../binary-transfer/) | Explicit enablement; writable workdir for upload; independent file-ingress configuration if needed |
| [Repo Ops](../repo-ops/) | Per-workdir opt-in, authorized host Bridge; no Agent runtime needed |
| [Agent delegation](../agent-bridge/) / [ACP](../acp/) | Authorized Bridge and configured runtime allowlist; runtime execution requires workspace-write and a writable workdir |
| [Jev](../jev-advisors/) | Optional Bridge-side advice, never authorization |

v0.26.0 supports 1–16 explicitly granted Linux Agent and/or Repo Ops slots and 1–16 owner-bound macOS Shared Agent workdirs. macOS Shared Core does not support Repo Ops; new Owned Cores may explicitly select signed v2 for per-alias Agent/Repo Ops grants. Windows initial shared provisioning requires exact policy subsets and individual grants. Use the platform runbooks; a capability switch is not a substitute for a valid binding.

Keep provider executables and credentials on the host. Configure each component's outbound proxy independently. The Linux compatibility renderer shares endpoint fields for Tunnel, Agent and Jev but has separate switches; Agent proxying requires a credentialless endpoint. Named Bridge owners have their own explicit policy. Windows named provisioning accepts per-runtime/ACP `use_proxy=true` with a protected credentialless `--agent-proxy-env-file`. Do not assume changing a Core env changes the owner.

## 6. Verify effective policy

Read the actual instance configuration and confirm its platform, identity and workdir mapping before changing it. Validate the selected deployment, restart only the authorized component when required, then check authenticated MCP, expected tools, safe reads and denied operations. Never infer access from a configuration file or a running task alone. See [Security](../security/).
