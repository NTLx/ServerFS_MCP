---
title: macOS Native
description: The native macOS deployment — Apple M-series only, native arm64, macOS 27 Golden Gate; Darwin FD backend, getpeereid AF_UNIX Bridge, launchd lifecycle, no Docker or Rosetta.
---

Since v0.13.0 ServerFS runs natively on **Apple M-series Macs running macOS 27 Golden Gate** as an MCP **stdio** service. The platform boundary is exact: native **arm64** execution only (no Rosetta, no x86_64), macOS 27.x only, **local APFS** workdirs. Intel Macs, Hackintosh, macOS 26 or older and macOS 28 or newer are refused by the measured runtime gate with `NATIVE_PLATFORM_UNSUPPORTED` — never silently executed.

The runtime chain:

```text
serverfs tunnel
  -> official pinned tunnel-client            (darwin-arm64 asset only)
       -> sanitizer supervisor
            -> serverfs serve                 (MCP stdio child, measured darwin gate)
                 -> Darwin FD backend         (pure Python over descriptor-relative POSIX)
            -> file-ingress helper            (separate process, private AF_UNIX socket)
       -> Agent Bridge (launchd user agent)   (com.ntlx.serverfs.agent-bridge)
            -> AF_UNIX + getpeereid           (0700 runtime dir, no peer PID fabrication)
            -> real provider CLI              (codex / claude / qoder; native arm64 only)
```

There is no Docker, VM, Rosetta requirement or compiled macOS kernel package: the Darwin backend is pure Python over descriptor-relative POSIX primitives plus three narrow libc bindings (`fcopyfile`, `getpeereid`, `confstr`). There is no `serverfs-macos-native` wheel.

## Filesystem semantics

- Descriptor-relative traversal with `O_NOFOLLOW` everywhere; symlinks fail closed exactly as on Linux.
- Atomic publication (same-directory temp → fsync → `renameat`) with revision/CAS and hard-link protection identical to the Linux contract.
- Replacing a file carries the original mode, user xattrs and extended ACLs onto the new inode via `fcopyfile(COPYFILE_METADATA)`, failing before publication on any loss.
- `search_text` never uses ripgrep or `/proc`: FD-secure walk → bounded read → literal UTF-8 scan, contract-identical to the Linux and Windows searchers.

## Agent Bridge on launchd

The Bridge runs as a per-user LaunchAgent (`com.ntlx.serverfs.agent-bridge`) managed with modern `launchctl bootstrap/kickstart/bootout`:

```bash
serverfs agent-bridge configure --config serverfs.toml --env-file .env
serverfs agent-bridge install --bridge-config "$HOME/Library/Application Support/ServerFS/agent-bridge/bridge.json"
serverfs agent-bridge start | stop | status | uninstall
```

The private `bridge.json` lives outside every exposed workdir and may contain Jev/proxy material; it is **derived/private state, not a second operator config**. Workdir, runtime and lifecycle policy remains owned by `serverfs.toml`. `configure` creates or refreshes the private 0600 document from that policy plus the narrow Agent/Jev/proxy values in `.env`. For a TOML-only change, `serverfs agent-bridge restart --config serverfs.toml` preserves private material; if private Jev/proxy values also changed, add `--env-file .env`. `serverfs doctor --config serverfs.toml` reports a FAIL on drift, and a newly started native `serve`/tunnel refuses to run until the policy is synchronized.

The generated plist carries paths only — never secrets. launchd is a service manager, not a containment kernel: no Job-Object equivalence is claimed, the recovery guard is retained, and workspace-write fails closed when provider state is unknown.

## Install

```bash
uv sync
uv sync --project agent_bridge
serverfs bootstrap tunnel-client        # darwin-arm64 asset only
serverfs doctor --config serverfs.toml
serverfs serve --config serverfs.toml   # or through the tunnel chain
```

Data lives in `~/Library/Application Support/ServerFS` (override: `SERVERFS_DATA_HOME`); sockets live in the OS-provided per-user runtime directory (`_CS_DARWIN_USER_TEMP_DIR`, validated — an inherited `$TMPDIR` is never trusted). ServerFS never bypasses macOS TCC privacy controls: protected paths fail cleanly and `serverfs doctor` identifies the likely denial.

## Acceptance evidence

`docs/phase-0-macos27-arm64-capability-probe-2026-10.md`, `docs/phase-macos-native-acceptance-2026-10.md`, `docs/phase-macos-agent-acceptance-2026-10.md`, `docs/phase-macos-live-chatgpt-e2e-2026-10.md`.
