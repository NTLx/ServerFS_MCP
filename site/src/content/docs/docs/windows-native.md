---
title: Windows Native
description: Deploy native stdio or independent named HTTP components on Windows 11.
---

ServerFS supports Windows 11 x64, local NTFS and the matching prebuilt Rust/ABI3 native wheel. Persistent components use current-user Task Scheduler and Job containment with windowless launchers. No WSL, VM, SCM service or elevation is required.

## Choose the right mode

| Mode | Use when |
| --- | --- |
| Local stdio | The MCP client is local and needs no persistent HTTP service |
| File-only Core | You need authenticated HTTP file access, without Agent prerequisites |
| Owned Bridge | A Core needs its own Agent or Repo Ops execution boundary |
| Shared Bridge | Multiple Cores explicitly share a Bridge; the initial helper accepts up to 16 Cores with separate grants |

All named HTTP Cores can use optional Cloudflare and OpenAI Connectors. The Core owns caller authentication. Agent and Repo Ops are separate workdir opt-ins; a Shared Core cannot manage its owner.

## Deploy

Use the [complete Windows runbook](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/deployment/windows/v0250-native-operations.md) for the exact PowerShell commands.

1. Prepare a trusted installed controller with matching Core/Native Wheels and a trusted uv offline cache. Include the separate Bridge Wheel only if needed.
2. Install fresh private component runtimes, then configure the intended file-only, owned or shared topology. The initial helper generates independent bearer credentials and signed grants without starting any task.
3. Register and start required tasks with the component CLI (`python -I -B -m serverfs_mcp.windows_v25.cli`). Start the Bridge before dependent Cores, then verify authenticated MCP before adding Connectors.
4. For public ingress, provision a separate Connector ID, trusted executables and private provider credentials. OpenAI requires static Core bearer admission and the matching bundled cloudflared.

The initial helpers support bearer authentication and explicit per-runtime/ACP `use_proxy=true` with an owner-private credentialless `--agent-proxy-env-file`; use the version-matched runbook. OAuth and other specialized configurations retain the advanced manual path. Do not mix the two configuration routes or bypass private ACL/reparse-point checks.

## Verify and operate

Check task registration and actual running state, anonymous denial, authorized MCP initialize, workdir access and any opted-in capabilities. Verify public reachability separately. Use exact component, instance and Connector IDs for start, stop, restart and uninstall.

Stop ingress before its Core; retire the Bridge only when all dependent Cores and active tasks are accounted for. Uninstall removes the verified task, not private data. Failed provisioning attempts retain evidence; do not rerun over partial state. Existing installations require an explicit migration/rollback plan.

Current live-provider and OpenAI public acceptance limits are recorded in the [release checklist](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.26.0-release-checklist.md).
