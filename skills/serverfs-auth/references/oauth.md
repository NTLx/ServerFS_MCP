# OAuth resource admission

Use `docs/oauth.md` and primary Skill gates. The packaged `serverfs-auth` runs in Linux Compose; native Core can use an independently managed authorization service, not a native installer-managed service. Named OpenAI HTTP requires static bearer, not this path.

For Linux retain exact Core project/env/exposure/Bridge selections and documented `compose.oauth.yml`. Set empty API Key, OAuth enabled, exact HTTPS resource/issuer/auth public URLs, scopes and shared private introspection secret; use distinct private auth-state directory with correct ownership. Configure enabled providers and exact stable-subject principal allowlist, not display names. Establish public TLS routes for resource/issuer but never route `/internal/introspect` publicly. Client preregistration requires explicit read-only mount per runbook.

For native use the matching private HTTP input (Windows manual path, not bearer-only helper). Set `SERVERFS_OAUTH_ENABLED`, resource URL, issuer URL, required scopes, introspection URL and secret using the full `SERVERFS_OAUTH_*` keys in the guide. Native auth state and provider credentials remain with the independent auth service. Validate exact Host, protected metadata and private introspection connectivity.

Verify health separately from real enabled-provider login, allowlisted acceptance/unallowlisted denial, authenticated MCP, wrong resource, expired/revoked token and failed introspection denial. Removing a principal blocks new admission but does not revoke existing access/refresh pairs; refresh/introspection do not recheck allowlist. Revoke current pair or explicitly authorize state invalidation. Provider secret rotation does not revoke tokens. Introspection secret rotation coordinates auth and every dependent Core. Scoped env recreation, not restart alone.

Back up whole private state only in appropriate maintenance; restoration may revive old token pairs. State replacement invalidates all pairs and loses registration/state; never discard or restore it silently. Report actual provider/client verification or missing evidence.
