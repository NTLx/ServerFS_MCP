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
