# Windows native

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

Bootstrap the tested official client through ServerFS:

```powershell
& $serverfs bootstrap tunnel-client
```

A non-default official release must follow the CLI's integrity-verification contract. Do not manually place an arbitrary binary on PATH as the managed solution.



## Optional Cloudflare branch (v0.24.2 release)

The existing commands above are **OpenAI Tunnel** commands. The intended v0.24.2 release supports a separate same-user Task Scheduler owner named `ServerFS Cloudflare WorkPC`, managed with `service cloudflare-parallel`, alongside the OpenAI default owner. Each has its own venv/state/watchdog/runner and selects one provider; both may share intended writable workdirs and one Agent Bridge/Repo Ops service. The old `service cloudflare` provider guard remains scoped to one owner and does not provide an automatic handover.  The operator waived full-generation restart/recovery testing on 2026-11-10; it was not tested and did not pass. The real public SSE gate passed with 75.0/70.0/65.0-second holds, approximately 15-second keepalives, same-session 27-tool `tools/list` 200, and new-session 200. Connector-only restart and lossless SSE replay are not claimed. Read [Windows Cloudflare](cloudflare-windows.md) and `docs/windows-cloudflare-tunnel.md` before operating either owner. Preserve the shared Bridge while Cloudflare depends on it if OpenAI is retired.

The v0.14+ current-user managed runner owns normal persistent Tunnel operation. Check:

```powershell
& $serverfs service status
```

Use `serverfs service restart/start/stop` for lifecycle changes. Direct `serverfs tunnel` is for bounded diagnostics/compatibility, not a second persistent owner. Keep the control-plane key file outside every exposed workdir.
