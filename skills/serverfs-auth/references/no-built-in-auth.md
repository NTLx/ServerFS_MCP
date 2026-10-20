# Linux / generic HTTP: no built-in auth

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

Keep both ServerFS-owned auth mechanisms disabled. This is appropriate only when the operator intentionally relies on a private network, OpenAI Tunnel, or an external authenticated gateway.

Do not remove an existing API Key/OAuth configuration merely because another ingress exists unless the user requests the change.
