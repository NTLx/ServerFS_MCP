# macOS Native Deployment (v0.24.1 stable)

ServerFS **v0.24.1 is the current stable release**. Native macOS Cloudflare Tunnel is an **v0.24.1 release**; it is optional and does not replace the existing OpenAI Secure MCP Tunnel, stdio, Agent Bridge or their lifecycle. Native Windows Cloudflare Tunnel remains planned for v0.24.2. The macOS deployment shape introduced
in v0.13.2 remains pure Python over POSIX/Darwin primitives (`fcopyfile`, `getpeereid`, `confstr`
via narrow ctypes bindings); v0.13.2 adds a small headless Swift ServiceManagement companion solely for persistent
per-user service registration. There is still no `serverfs-macos-native` wheel, Docker/VM,
LaunchDaemon, root helper or Rosetta requirement. Deterministic Repo Ops shipped on Linux Host Bridge in v0.23.0, validated native Apple Silicon macOS in v0.23.1, and validated native Windows 11 x64 in v0.23.2. The existing native persistent Tunnel remains the OpenAI adapter unless the operator separately opts into the Cloudflare pair below.

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

When the installed manifest includes Agent Bridge, `service restart` uses a product-owned, one-shot launchd hand-over so unregistering the Bridge cannot terminate the process that must finish the restart. The parent command reports that the hand-over was scheduled; reconnect and run `serverfs service status` to verify the native `handover` evidence reaches `complete=true`, `ok=true`, `job_loaded=false` and `plist_exists=false`. Tunnel-only restart remains synchronous. `serverfs agent-bridge restart` uses the same bounded hand-over after any requested policy/private-state synchronization and restarts only the Bridge.

The default native `service status` JSON includes this bounded hand-over evidence. For a sanitized common projection, explicitly select `--format common-json` or `--format common-human`; it omits paths, PIDs, task names, private IDs and the native hand-over detail, and reports `unknown`/`not-checked` rather than inferring health. Use `serverfs doctor` for deeper platform/configuration diagnostics. Phase F acceptance limits: [v0.21.0 native acceptance](../../docs/v0.21.0-phase-f-native-acceptance-2026-10-22.md).

`uninstall` removes only v0.13.2-owned registration/runtime artifacts; operator configuration and
normal ServerFS data are preserved.

## Optional Cloudflare Tunnel (v0.24.1 release)

v0.24.1 adds a separate native Cloudflare Connector and private loopback Streamable HTTP origin. It does not change the existing OpenAI Tunnel or Agent Bridge. Use it only with a v0.24.1 build on the supported native Apple Silicon/macOS 27 platform. It does not create or edit Cloudflare Tunnels, DNS routes, or public hostname configuration; configure the named remotely managed Tunnel and route through Cloudflare's operator controls first.

The bootstrap command installs the pinned official Cloudflare `cloudflared` macOS arm64 release, verifying its archive and binary digests, code signature and version. With no flag it downloads the pinned artifact; `--artifact` accepts a local archive for offline installation:

```bash
serverfs bootstrap cloudflare-client
serverfs bootstrap cloudflare-client --artifact /absolute/path/to/cloudflared-darwin-arm64.tgz
```

Prepare two current-user regular files with mode `0600`, stored outside the checkout and every configured workdir:

- a connector Token file, containing only the remotely managed Tunnel token;
- an HTTP-origin environment file containing only supported settings. Use either a non-empty `SERVERFS_HTTP_API_KEY` with OAuth disabled, or OAuth enabled with its required public resource/issuer/introspection settings and secret. Do not combine API Key and OAuth. Set `SERVERFS_HTTP_ALLOWED_HOSTS` to exactly the public hostname passed to install. The Token authenticates only the Connector; it does not authenticate MCP callers.

Use absolute paths for all install paths. `--http-port` has no default and must be available for the loopback origin. Configure `serverfs.toml` with at least one staging workdir and ensure every workdir is read-only before installing. Replace the path and hostname placeholders below with the operator's own values; never put secret values in command arguments:

```bash
serverfs service cloudflare install \
  --config /absolute/path/to/serverfs.toml \
  --cloudflared /absolute/path/to/cloudflared \
  --token-file /absolute/path/outside-workdirs/cloudflare-token \
  --env-file /absolute/path/outside-workdirs/cloudflare-origin.env \
  --http-port 18765 \
  --hostname mcp.example.invalid
serverfs service cloudflare status
```

The installer registers an HTTP-origin/Connector pair through the same current-user, app-associated `SMAppService` LaunchAgent mechanism as the existing native services. The origin binds to `127.0.0.1`; public TLS terminates at Cloudflare. The hostname must match exactly in ServerFS Host admission. Verify both service statuses, then verify public HTTPS rejects missing/wrong caller credentials with 401 and accepts the intended API Key/OAuth identity with 200. Continue with MCP tools and a persistent SSE connection longer than 60 seconds. After a controlled Connector-only restart, establish a **fresh MCP session** and verify tools; the previously established session may return 404 (the observed staging result was 8/8 successful fresh-session checks). Do not claim old-session continuity or SSE event replay. Keep this acceptance on an isolated read-only staging workdir and a separate test hostname; do not claim production workdir/Agent acceptance from it.

If registration fails, the installer attempts to unregister the services it registered and restore its manifest state. If rollback is reported incomplete, stop and preserve the recovery state; do not layer another install over it. The explicit lifecycle commands are:

```bash
serverfs service cloudflare start
serverfs service cloudflare stop
serverfs service cloudflare restart
serverfs service cloudflare uninstall
```

`uninstall` unregisters this Cloudflare service pair and removes its private origin-environment snapshot; it preserves the operator's source Token/env files, `serverfs.toml`, workdirs, existing OpenAI Tunnel and Agent Bridge. Cloudflare service registration can require user approval in System Settings > General > Login Items; respect that decision and do not bypass it.

For v0.24.1 lifecycle acceptance, architectural equivalence to the previously verified app-associated `SMAppService`/LaunchAgent mechanism is accepted. An additional real logout/login or whole-Mac reboot is not required for this release and must not be claimed as performed. This exception applies only while the lifecycle architecture remains the same.

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


ACP profile configuration and operation: [ACP Guide](https://ntlx.github.io/ServerFS_MCP/docs/acp/).
