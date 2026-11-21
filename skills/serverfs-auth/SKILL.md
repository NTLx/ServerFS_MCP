---
name: serverfs-auth
description: Inspect or change a selected Core's caller admission, API Key, OAuth or Host/Origin policy; verify denial and avoid unintended exposure.
---

# Configure ServerFS caller admission

## When to use

Select or diagnose HTTP caller admission, or plan an authorized credential change.

## When NOT to use

Provider Tunnel credentials, workdir permissions, Bridge grants or unrelated components. Local stdio has no HTTP admission to configure.

## Preflight

Identify exact Core, installed version, transport, public/private resource, Host/Origin, current admission mode, dependent clients/Connectors, active work and auth state ownership. Confirm intended exposure and authorization. Stop on unclear identity or migration/rotation support.

## Platform branches

Select only the relevant reference:

- [API Key](references/api-key.md): one static secret per Core.
- [OAuth](references/oauth.md): separate authorization service and exact resource-server contract.
- [No built-in auth](references/no-built-in-auth.md): only an explicitly isolated Linux or validated external admission topology.
- [Native stdio](references/native-stdio.md): local transport, no HTTP auth.

Named native HTTP requires API Key or OAuth. Linux Compose supplies the auth service; native Core can use an independently managed one. Named OpenAI HTTP requires static bearer, not OAuth-only admission.

## Authoritative sources

Paths in references point to the matching checkout; links above are packaged. Use the selected auth guide, applicable platform runbook and actual CLI/config parser when necessary. Do not guess missing settings or copy template secrets into a live configuration.

## Procedure

1. Preserve original settings and state privately; do not reveal secrets or expand workdirs.
2. Choose one supported admission mode and exact Host/Origin policy; distinguish provider credentials from caller credentials. API Key and OAuth cannot both be active.
3. Validate new private inputs and required coordinated changes (OAuth introspection secret, OpenAI Core bearer header, client credentials). Native initial staging/install is create-only, not general online rotation.
4. Apply only supported scoped changes with authorization. Retain exact Compose selections; env changes require recreation, not restart. For unsupported native rotation, stop at a migration/reprovision plan.
5. Verify authenticated MCP and denials using fresh sessions. For OAuth verify metadata, real provider admission, resource/token checks and revocation behavior. For OpenAI test upstream Core key denial locally, provider admission separately.

## Safety / stop conditions

Never expose an unauthenticated public Core, private introspection endpoint, credential/state directory, expanded env or raw tokens. Do not widen principal/Host/Origin policy or modify provider routes to make a test pass. Shared Core cannot alter owner grants. Stop on unsafe active work, authorization ambiguity, validation failure or uncertain state compatibility.

## Verification

Check exact effective mode, authenticated initialize/tools/list and safe read, missing/wrong credentials or OAuth challenge, denied requests and unchanged workdir/Bridge policy. Verify current token revocation separately from removal of an allowlisted principal. Record actual external/client evidence and missing gates.

## Success criteria

Intended admission works, expected denials hold, no credential disclosure or unrelated policy change.

## Failure handling

Preserve state, isolate failing admission boundary, use only authorized compatible rollback. OAuth backup restoration may revive tokens; never restore or discard state silently. Report exact failure, changes and next bounded check.
