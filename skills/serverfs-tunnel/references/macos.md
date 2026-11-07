# macOS native

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

Use the current ServerFS bootstrap and ServiceManagement lifecycle:

```bash
serverfs bootstrap tunnel-client
serverfs service status
serverfs service restart
```

The persistent Tunnel is an app-associated per-user LaunchAgent owned through `SMAppService`. Do not replace it with a root LaunchDaemon or unmanaged hand-installed plist.

If status requires Login Items approval, surface the user action and use `serverfs service open-settings` when requested; do not bypass it.

## Native Cloudflare (v0.24.1)

The existing OpenAI Tunnel remains the normal persistent native Tunnel path. Cloudflare is a distinct opt-in HTTP-origin/Connector pair, available only in a matching v0.24.1 release on supported native Apple Silicon/macOS 27. The published v0.24.1 stable release does not include it; Windows Cloudflare remains planned for v0.24.2. Read `deployment/macos/README.md` for its complete setup and acceptance procedure.

The verified CLI syntax is `serverfs bootstrap cloudflare-client [--artifact PATH]`; install requires `--config`, `--cloudflared`, `--token-file`, `--env-file`, `--http-port`, and `--hostname` under `serverfs service cloudflare install`. `status`, `start`, `stop`, `restart`, and `uninstall` take no additional arguments. Never place secret values in argv. The connector Token and origin env file must each be current-user regular files mode `0600`, outside the checkout and all workdirs. The service requires only read-only staging workdirs, exact equality between the install hostname and `SERVERFS_HTTP_ALLOWED_HOSTS`, and exactly one caller-auth mode (ServerFS API Key or OAuth); the Connector Token is not caller authentication. After a Connector-only restart, establish a fresh MCP session and verify tools; an old session may return 404, and SSE replay is not guaranteed. Keep Cloudflare service health and credentials distinct from OpenAI Tunnel health and credentials.
