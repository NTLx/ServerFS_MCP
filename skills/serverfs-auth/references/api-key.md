# Linux / static API Key

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

Set one high-entropy `SERVERFS_HTTP_API_KEY` in the untracked `.env`; leave OAuth disabled. Keep the normal generic HTTP Compose shape:

```bash
docker compose --env-file .env -f compose.yml -f compose.http.yml config --quiet
docker compose --env-file .env -f compose.yml -f compose.http.yml up -d serverfs-mcp
```

Clients should prefer `Authorization: Bearer <key>`. The `?key=<key>` compatibility form can leak through upstream URL logs; do not paste or log that complete URL unnecessarily.

For rotation, replace the one key, recreate/restart ServerFS, then update clients. There is no built-in multi-key overlap database in the current design.
