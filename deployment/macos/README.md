# macOS Native Deployment (v0.14.1)

ServerFS v0.14.1 is the current published stable release. The macOS deployment shape introduced
in v0.13.2 remains pure Python over POSIX/Darwin primitives (`fcopyfile`, `getpeereid`, `confstr`
via narrow ctypes bindings); v0.13.2 adds a small headless Swift ServiceManagement companion solely for persistent
per-user service registration. There is still no `serverfs-macos-native` wheel, Docker/VM,
LaunchDaemon, root helper or Rosetta requirement.

## Supported platform

```text
Apple M-series Mac, native arm64 (no Rosetta), macOS 27 Golden Gate, local APFS
```

## Install dependencies

```bash
uv sync --frozen                           # ServerFS product environment
uv sync --project agent_bridge --frozen    # Agent Bridge environment
serverfs bootstrap tunnel-client           # defaults to the ServerFS-tested official release
serverfs doctor --config serverfs.toml
```

The persistent-service installer builds `ServerFSServiceManager.app` locally with the Apple command
line toolchain (`xcrun swiftc`, `codesign`, `plutil`), validates it, and installs it under the
private per-user ServerFS Application Support tree. The app contains two app-associated
LaunchAgents and uses Apple's `SMAppService` API; normal operation does not copy a plist into
`~/Library/LaunchAgents` or ask the operator to run raw `launchctl` commands.

## Persistent Tunnel + Agent Bridge

With tunnel credentials and any Agent/Jev/proxy settings in the private `.env`:

```bash
serverfs service install --config serverfs.toml --env-file .env
serverfs service status
```

`install` materializes private 0600 lifecycle state, builds/validates the native companion, registers
`com.ntlx.serverfs.tunnel` and (when Agent delegation is enabled) `com.ntlx.serverfs.bridge`,
and verifies the managed processes. Both are current-user LaunchAgents with `RunAtLoad` and
`KeepAlive`: they survive terminal/Agent-session exit. `SMAppService` owns user-login persistence;
live reboot/re-login recovery validation is deferred beyond v0.13.2.

macOS may require explicit approval under **System Settings > General > Login Items**. ServerFS
reports that condition as `requires-approval` and never bypasses the user's setting:

```bash
serverfs service open-settings
```

Lifecycle commands are:

```bash
serverfs service status
serverfs service start
serverfs service restart
serverfs service stop
serverfs service uninstall
```

`uninstall` removes only v0.13.2-owned registration/runtime artifacts; operator configuration and
normal ServerFS data are preserved.

## Agent policy updates

The private `bridge.json` remains derived/private state outside every exposed workdir, not a second
operator configuration. Workdir/runtime/lifecycle policy is owned by `serverfs.toml`:

```bash
serverfs agent-bridge configure --config serverfs.toml --env-file .env
serverfs agent-bridge restart --config serverfs.toml
# if private Jev/proxy values also changed:
serverfs agent-bridge restart --config serverfs.toml --env-file .env
```

The legacy `serverfs agent-bridge install` path is retired in v0.13.2. Upgrading from v0.13.0 or
v0.13.1 detects the old `~/Library/LaunchAgents/com.ntlx.serverfs.agent-bridge.plist`, performs a
bounded hand-over to the app-associated LaunchAgent, health-checks the new Bridge, and removes the
legacy plist only after the replacement is healthy. An unmanaged v0.13.1 tunnel is detected and
must be stopped before installation, preventing duplicate control-plane clients.

## Manual/debug operation

Direct commands remain useful for diagnostics, but they are not the persistent production path:

```bash
serverfs serve --config serverfs.toml
serverfs tunnel --config serverfs.toml --tunnel-id tunnel_... --api-key-file KEY
```

When `SERVERFS_FILE_INGRESS_ENABLED=true`, `serve` owns a native file-ingress helper over a private
AF_UNIX socket. The v0.13.2 managed tunnel also forwards termination to tunnel-client so the
supervisor/serve/helper tree shuts down coherently rather than becoming orphaned.

No root, no LaunchDaemon and no shell-profile dependency are introduced. launchd remains a service
manager rather than a containment kernel; the Agent Bridge recovery guard still keeps
workspace-write failed closed whenever provider state is unknown.
