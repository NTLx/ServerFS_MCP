# Linux / OAuth 2.1

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

OAuth is an explicit Compose overlay and separate authorization service:

```bash
docker compose --env-file .env \
  -f compose.yml \
  -f compose.http.yml \
  -f compose.oauth.yml \
  config --quiet
```

Before starting, verify from the current OAuth guide/template that:

- OAuth is enabled;
- API Key is empty;
- resource/public/issuer URLs are exact and use HTTPS outside loopback tests;
- authorization public URL and issuer are exactly equal where required;
- introspection secret is configured privately;
- at least one provider is enabled with credentials;
- the exact stable-principal allowlist is present and contains no wildcards;
- the auth-state host directory exists with appropriate non-root ownership/permissions.

Then deploy the same validated shape. Preserve `compose.agent.yml` whenever the existing deployment uses the Host Bridge for Agent delegation and/or v0.23 Repo Ops; do not infer Agent enablement from the overlay itself, and do not change Repo Ops/Agent policy as part of OAuth work.

Do not authorize users by display name, email, nickname or phone when the provider contract requires a stable provider subject.
