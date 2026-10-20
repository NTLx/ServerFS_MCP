# macOS native

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

Update the ServerFS/Bridge Python environments through the current project/release path, then validate:

```bash
serverfs doctor --config serverfs.toml
serverfs service status
```

Use the v0.13.2+ ServiceManagement lifecycle for install/restart/hand-over. Do not recreate legacy hand-installed LaunchAgents as an upgrade shortcut. Preserve Login Items approval state and report `requires-approval` if macOS requires human action.

For Agent Bridge policy/private config refresh use the current `serverfs agent-bridge configure/restart` surfaces when required by the release/config change.
