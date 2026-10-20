# Windows native

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

Bootstrap the tested official client through ServerFS:

```powershell
& $serverfs bootstrap tunnel-client
```

A non-default official release must follow the CLI's integrity-verification contract. Do not manually place an arbitrary binary on PATH as the managed solution.

The v0.14+ current-user managed runner owns normal persistent Tunnel operation. Check:

```powershell
& $serverfs service status
```

Use `serverfs service restart/start/stop` for lifecycle changes. Direct `serverfs tunnel` is for bounded diagnostics/compatibility, not a second persistent owner. Keep the control-plane key file outside every exposed workdir.
