---
title: macOS Native
description: ServerFS v0.19.0 native macOS deployment — Apple M-series, macOS 27, Darwin FD safety and Apple SMAppService persistence for Tunnel and Agent Bridge.
---

The current stable project release is **v0.19.0**; the macOS native support shape introduced in **v0.13.2** remains unchanged. ServerFS runs natively on **Apple M-series Macs running macOS 27 Golden Gate**. The supported boundary remains exact: native **arm64** only, no Rosetta/x86_64, macOS 27.x only, and **local APFS** workdirs. Intel Macs, Hackintosh, macOS 26 or older and macOS 28 or newer fail closed with `NATIVE_PLATFORM_UNSUPPORTED`.

v0.13.2 keeps the Darwin filesystem and Agent protocol implementation in Python, but replaces the old hand-installed LaunchAgent lifecycle with Apple's current **ServiceManagement / `SMAppService`** model. A small headless `ServerFSServiceManager.app` owns registration and status for two per-user LaunchAgents:

```text
SMAppService / launchd
  +-- com.ntlx.serverfs.tunnel
  |    -> serverfs tunnel
  |         -> official tunnel-client (tested release by default, darwin-arm64)
  |              -> sanitizer supervisor
  |                   -> serverfs serve
  |                        -> Darwin FD backend
  |                        -> optional file-ingress helper
  |
  +-- com.ntlx.serverfs.bridge
       -> Agent Bridge
            -> private AF_UNIX + getpeereid
            -> codex / claude / qoder
```

Both services survive terminal/Agent-session exit. `SMAppService` owns user-login persistence; live reboot/re-login recovery validation is deferred beyond v0.13.2. ServerFS does **not** install a LaunchDaemon or run before login.

There is no Docker, VM, Rosetta requirement or compiled macOS filesystem kernel package. The Darwin backend remains pure Python over descriptor-relative POSIX primitives plus narrow libc bindings (`fcopyfile`, `getpeereid`, `confstr`). There is no `serverfs-macos-native` wheel.

## Filesystem semantics

- Descriptor-relative traversal with `O_NOFOLLOW`; symlinks fail closed.
- Atomic publication (same-directory temp → fsync → `renameat`) with revision/CAS and hard-link protection.
- Replacement writes preserve mode, user xattrs and extended ACLs with `fcopyfile(COPYFILE_METADATA)` or fail before publication.
- `search_text` uses an FD-secure walk, bounded reads and literal UTF-8 scanning rather than `/proc` or an ambient shell tool.

## Install persistent services

Install the normal Python environments and official tunnel client first:

```bash
uv sync --frozen
uv sync --project agent_bridge --frozen
serverfs bootstrap tunnel-client   # defaults to the ServerFS-tested v0.0.16 release
serverfs doctor --config serverfs.toml

# To use another official release, provide both its tag and the SHA-256 of
# that release's official SHA256SUMS.txt:
# serverfs bootstrap tunnel-client --version vX.Y.Z --manifest-sha256 <sha256>
```

Then install the persistent user services:

```bash
serverfs service install --config serverfs.toml --env-file .env
serverfs service status
```

The installer builds the small Swift companion locally with the Apple command-line toolchain, validates its code signature and embedded LaunchAgent plists, installs it under ServerFS's private Application Support tree, materializes 0600 lifecycle state, and registers the services through `SMAppService`.

The normal lifecycle surface is:

```bash
serverfs service status
serverfs service start
serverfs service restart
serverfs service stop
serverfs service uninstall
```

If macOS reports `requires-approval`, ServerFS respects that user decision. Open the exact settings pane with:

```bash
serverfs service open-settings
```

Approve the ServerFS background item under **System Settings > General > Login Items**. ServerFS does not bypass or simulate approval.

## Agent Bridge policy

The v0.19.0 ACP profile uses the native POSIX process-group containment seam. OpenCode 2.1.2 with `opencode/glm-5.3-flash-free` passed macOS end-to-end acceptance, including prompt, persisted-session continuation, and active-tool cancellation/cleanup.

`serverfs.toml` remains the operator source of Agent workdir/runtime policy. The private `bridge.json` outside every exposed workdir remains derived state and may contain the narrow Jev/proxy material needed by the Bridge.

```bash
serverfs agent-bridge configure --config serverfs.toml --env-file .env
serverfs agent-bridge restart --config serverfs.toml
# refresh private Jev/proxy values as well:
serverfs agent-bridge restart --config serverfs.toml --env-file .env
```

`serverfs agent-bridge install` is retired in v0.13.2. Upgrades from v0.13.0/v0.13.1 detect the legacy `~/Library/LaunchAgents/com.ntlx.serverfs.agent-bridge.plist`, stop the old job only at hand-over, register the app-associated replacement, health-check it, and remove the old plist only after success. Registration failures retain a rollback path. An unmanaged legacy tunnel is detected before installation so ServerFS never silently starts a second control-plane client.

The managed tunnel also holds a per-user instance lock and forwards launchd termination to tunnel-client, preventing orphaned supervisor/serve/file-ingress descendants.

## Data and security boundaries

Data lives in `~/Library/Application Support/ServerFS` by default; sockets live in the OS-provided per-user runtime directory (`_CS_DARWIN_USER_TEMP_DIR`). Service manifests, private environment maps and logs are outside exposed workdirs with private permissions. LaunchAgent plists contain no provider or tunnel credentials.

ServerFS never bypasses macOS TCC privacy controls. Protected paths fail cleanly, and `serverfs doctor` reports platform, policy, Service Manager validity, registration/approval state, process health, legacy artifacts and unmanaged tunnel detection.

## Acceptance evidence

See `docs/phase-0-macos27-arm64-capability-probe-2026-10.md`, `docs/phase-v0.13.2-servicemanagement-probe-2026-10.md`, the v0.13 macOS acceptance records, and the v0.13.2 development/acceptance record.
