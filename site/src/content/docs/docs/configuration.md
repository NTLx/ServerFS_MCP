---
title: Configuration
description: Define scoped workdirs and inherit secure defaults.
---

ServerFS resolves an immutable effective policy for each enabled workdir at startup.

## Workdir slots

Up to 16 workdirs can be configured:

```text
WORKDIR_01_ALIAS=projects
WORKDIR_01_PATH=/srv/projects
WORKDIR_01_DESCRIPTION="Projects"
WORKDIR_01_READ_ONLY=true
```

A second workdir can opt into writes:

```text
WORKDIR_02_ALIAS=scratch
WORKDIR_02_PATH=/srv/scratch
WORKDIR_02_DESCRIPTION="Agent scratch space"
WORKDIR_02_READ_ONLY=false
```

## Policy inheritance

Global `SERVERFS_*` settings act as defaults. A workdir-specific `WORKDIR_XX_*` scalar overrides the global value for that slot; an empty workdir value inherits the global default.

This model applies to:

- hidden-file policy
- read/write limits
- binary transfer
- Agent policy

Extra deny globs are stricter: global and workdir rules are **unioned**, so a workdir can add restrictions but cannot remove the global deny floor.

## Important defaults

- Workdirs are read-only by default.
- Binary transfer is disabled by default.
- ChatGPT file ingress is disabled by default and requires both `SERVERFS_FILE_INGRESS_ENABLED=true` and the `file-ingress` Compose profile.
- The constrained OpenAI Blob host-family policy is separately disabled by default; enable it with `SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS=true` only when ChatGPT file parameters are required. Exact additional hosts belong in `SERVERFS_FILE_INGRESS_ALLOWED_HOSTS`; generic wildcards are rejected.
- Agent policy is disabled unless explicitly configured.
- Jev advisors are disabled unless `SERVERFS_JEV_API_KEY` is non-empty. The key is consumed only by the host Agent Bridge deployment path, not the MCP container.
- Host paths are not exposed through MCP tool results.
- A typo in a workdir path fails loudly instead of silently creating a directory.

## Optional Jev advisors

To enable the host-side advisory suite, set the TypeSafe API key in the existing untracked `.env`:

```text
SERVERFS_JEV_API_KEY=<your key>
```

Leave it empty to disable Agent Task Preflight, Runtime Router, and Approval Advisor completely. The normal Agent Bridge installer renders a configured key only into its user-owned `0600` config. See [Jev Advisors](./jev-advisors/) for the runtime behavior and security boundary.

See the repository [README](https://github.com/NTLx/ServerFS_MCP#workdir-configuration) for the complete environment-variable reference.
