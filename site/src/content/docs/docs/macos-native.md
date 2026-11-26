---
title: macOS Native
description: Choose a native stdio or named HTTP deployment on Apple Silicon.
---

ServerFS runs on Apple Silicon, native arm64, macOS 27.x and local APFS, with the current user's filesystem permissions and macOS privacy controls. Persistent HTTP services use signed SMAppService / LaunchAgents. No Docker, Rosetta or root LaunchDaemon is required.

## Choose the right mode

| Mode | Use when |
| --- | --- |
| Local stdio | The MCP client is local and needs no persistent service |
| File-only Core | You need authenticated HTTP file access, without a Bridge |
| Owned Bridge | A Core needs its own Agent or Repo Ops execution boundary |
| Shared Bridge | Multiple Cores need an existing owner-managed v2 Bridge; each Core supports 1–16 owner-bound Agent workdirs, with Repo Ops disabled |

File-only and owned Cores support optional OpenAI and Cloudflare Connectors. Shared Cores currently have no Connector staging interface. An Owned pair defaults to v1 private native socket and policy checks. New Owned instances may explicitly use `owned-stage --agent-protocol v2` for signed per-alias Agent/Repo Ops grants; do not switch an existing instance in place. Shared attachment uses individual signed v2 grants.

## Deploy

Follow the [complete macOS runbook](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/macos/README.md) for the exact commands and copyable configuration examples.

1. Prepare version-matched Core (and, if needed, Bridge) environments and build a signed template App.
2. Create a minimal native TOML defining the intended workdirs. For persistent HTTP, prepare a distinct port and a private API Key or OAuth configuration. The Host allowlist must exactly match the configured hostname followed by the loopback authority.
3. Use the named CLI (`python -m serverfs_mcp.darwin_instance_cli --instance <id>`) to stage the selected mode, then start it explicitly. A Shared Core also requires a verified independent runtime and an owner-issued binding; run its preflight before activation.
4. Approve the Login Item in System Settings if required. Verify the Core before starting optional Connectors.

For same-provider additional Connector IDs, predeclare them at file-only/owned staging. OpenAI HTTP uses separate control-plane and static Core bearer files; Tunnel credentials never replace caller authentication.

## Verify and operate

Check anonymous denial, authenticated MCP initialization, safe workdir access and any explicitly enabled Agent/Repo Ops tools. A registered job does not prove public reachability. Operate each Connector by its exact identity; check that unrelated components remain running and reinitialize the session after restart.

Stop Connectors before stopping their Core or owned pair. Shared Core operations never manage the owner Bridge. The named staging commands create new instances; use an explicit migration or rollback plan for existing deployments. Release-level acceptance and current limitations are tracked in the [release checklist](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.26.0-release-checklist.md).
