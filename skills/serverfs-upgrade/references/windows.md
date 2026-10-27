# Windows native

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

Use the stable current-user connector and Bridge environments described by the current Windows guide. Upgrade the release wheels in their correct isolated environments; do not merge product and Agent Bridge dependencies. v0.23.2 adds validated native Windows 11 x64 Deterministic Repo Ops using the same independently gated Bridge transport and no-Agent-fallback contract as Linux/macOS. Preserve existing per-workdir Repo Ops enablement exactly; do not infer Repo Ops from Agent mode or vice versa.

Before/after the managed lifecycle update, use:

```powershell
& $serverfs doctor --config serverfs.toml
& $serverfs service status
```

Use `serverfs service install/restart/start` only according to the documented target-release lifecycle. Preserve current-user Task Scheduler ownership and operator enabled/disabled intent; do not replace it with an administrator service.

If Tunnel bootstrap/version must change as part of the target release, use the supported `serverfs bootstrap tunnel-client` path and its integrity checks rather than dropping an executable onto PATH.
