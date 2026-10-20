# macOS native

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

Agent workdir/runtime policy lives in `serverfs.toml`. Private derived Bridge state stays outside exposed workdirs. Use supported commands:

```bash
serverfs agent-bridge configure --config serverfs.toml --env-file .env
serverfs agent-bridge restart --config serverfs.toml --env-file .env
serverfs service status
serverfs doctor --config serverfs.toml
```

Persistent Bridge ownership is through the app-associated current-user ServiceManagement lifecycle. Do not resurrect the retired `serverfs agent-bridge install` path or hand-install legacy LaunchAgent plists.

Respect Login Items/TCC user decisions; do not bypass them.
