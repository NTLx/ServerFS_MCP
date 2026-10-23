# macOS native

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

Update the ServerFS/Bridge Python environments through the current project/release path, then validate:

```bash
serverfs doctor --config serverfs.toml
serverfs service status
```

Use the v0.13.2+ ServiceManagement lifecycle for install/restart/hand-over. Do not recreate legacy hand-installed LaunchAgents as an upgrade shortcut. Preserve Login Items approval state and report `requires-approval` if macOS requires human action.

When Agent Bridge is installed, current `serverfs service restart` uses a product-owned one-shot hand-over because the invoking Agent session may be terminated while the Bridge is replaced. A scheduling message is not final success. Reconnect and require native `serverfs service status` to report `handover.complete=true`, `handover.ok=true`, `handover.job_loaded=false`, and `handover.plist_exists=false`; also verify the expected managed processes and absence of unmanaged Tunnel ownership. Tunnel-only restart remains synchronous.

For Agent Bridge policy/private config refresh use the current `serverfs agent-bridge configure/restart` surfaces when required by the release/config change. `agent-bridge restart` uses the same bounded hand-over after synchronization and restarts only the Bridge.
