# Windows/macOS native

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

ServerFS built-in HTTP API Key/OAuth does not apply to the native stdio deployment. If the user's actual goal is ChatGPT connectivity, use `serverfs-tunnel`. If they need a Linux remote HTTP ServerFS endpoint, configure that Linux deployment rather than bolting HTTP auth onto native stdio.
