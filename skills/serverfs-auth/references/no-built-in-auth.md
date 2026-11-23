# HTTP without built-in authentication

Use only for explicitly authorized isolated Linux local/private HTTP or a validated independent external caller admission boundary. Named macOS/Windows HTTP requires API Key or OAuth; named OpenAI HTTP requires a static Core bearer. Never remove required auth to accommodate a Connector.

Before changing Linux admission, inspect published bind address, network membership, reverse-proxy enforcement, alternate routes and exact Host policy. Loopback alone is not per-caller authorization; Tunnel registration, token or unguessable URL are not caller auth. Stop if any reachable path bypasses intended admission. Prefer explicit Core auth.

Use the exact Linux runbook/Compose selection; any env change requires scoped recreate, not restart. Verify from intended and disallowed networks/clients using safe requests, and confirm no unintended publication. Do not claim external admission works without testing that boundary.
