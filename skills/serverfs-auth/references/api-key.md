# Static API Key

Use `docs/api-key.md`, the matching platform runbook and primary Skill gates.

One unique high-entropy ASCII secret per HTTP Core; no per-user identity or multi-key grace period. Linux private env uses `SERVERFS_HTTP_API_KEY`; macOS named private HTTP env and Windows initial generated key/manual protected auth input follow native runbooks. OAuth disabled. Keep secrets outside exposed roots and logs.

Prefer private Authorization bearer configuration. Compatibility `?key=` query may leak upstream; missing/wrong/empty/duplicate/mixed header-query credentials fail with 401. Test exact Host/Origin and fresh MCP acceptance, relevant denial and unchanged workdir policy.

For Linux rotation, plan affected sessions/clients and OpenAI Core bearer input, update private env and scoped `up -d --no-deps --no-build --force-recreate serverfs-mcp` with full Compose selection; recreate affected OpenAI Connector separately after updating its private header file. Verify old key denied, new key works. OpenAI injects Core bearer; check local Core denial separately from provider admission. Native create-only provisioning provides no general online rotation; plan explicit migration/reprovision rather than modifying pinned snapshots or manifests. Preserve original inputs and recovery path.
