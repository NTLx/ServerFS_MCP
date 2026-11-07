# Windows native

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

Bootstrap the tested official client through ServerFS:

```powershell
& $serverfs bootstrap tunnel-client
```

A non-default official release must follow the CLI's integrity-verification contract. Do not manually place an arbitrary binary on PATH as the managed solution.



## Optional Cloudflare branch (v0.24.2 candidate)

The existing commands above are **OpenAI Tunnel** commands. Windows Cloudflare is a distinct opt-in **provider for the same current-user Task Scheduler owner**, not an additional always-on Windows service. For a version-matched v0.24.2 implementation, read [Windows Cloudflare](cloudflare-windows.md) and `docs/windows-cloudflare-tunnel.md` before using `service cloudflare`; the production WorkPC OpenAI service must not be switched or stopped for development testing. v0.24.1 remains the stable Windows release until v0.24.2 is published.

The v0.14+ current-user managed runner owns normal persistent Tunnel operation. Check:

```powershell
& $serverfs service status
```

Use `serverfs service restart/start/stop` for lifecycle changes. Direct `serverfs tunnel` is for bounded diagnostics/compatibility, not a second persistent owner. Keep the control-plane key file outside every exposed workdir.
