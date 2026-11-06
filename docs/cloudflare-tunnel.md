# Cloudflare Tunnel — Linux remote MCP ingress (v0.24.0)

> **Release status:** Linux Docker Cloudflare Tunnel ingress was **published in v0.24.0** after exact-SHA, dispatch-first cross-platform CI. Isolated staging passed public MCP protocol, direct ChatGPT tool calls, long-lived SSE and session reuse after Connector restart. This does not imply production workdir/Agent acceptance or native macOS/Windows Cloudflare support; those are planned separately for v0.24.1/v0.24.2.

Cloudflare Tunnel provides an outbound-only network ingress for ServerFS Streamable HTTP. It does not replace MCP authentication or the existing OpenAI Secure MCP Tunnel. The Linux Compose connector has no host-published port and shares only the private ServerFS MCP network with the application.

## Requirements and boundary

- Linux Docker Engine + Compose; outbound connectivity from the cloudflared container to Cloudflare (typically TCP/UDP 7844; confirm current Cloudflare network requirements).
- An active Cloudflare-managed domain and a **remotely managed** named Tunnel. Its hostname-to-service route must point to `http://serverfs-mcp:8000` (or the actual `SERVERFS_HTTP_PORT`), NOT `127.0.0.1:8000`: loopback in the cloudflared container points to itself.
- The original Tunnel Token is a connector credential. Its possession **does not** authenticate remote MCP callers.
- Choose exactly one ServerFS remote ingress admission boundary: set a strong `SERVERFS_HTTP_API_KEY` with OAuth disabled, or enable the existing `SERVERFS_OAUTH_ENABLED=true` flow with its documented isolated auth broker. Do **not** publish anonymously unless an equivalent upstream admission boundary has been independently validated.
- Allowlist the exact externally received hostname in `SERVERFS_HTTP_ALLOWED_HOSTS`, along with any needed internal authority. Do not turn off DNS-rebinding protection.
- Put token material **outside the checkout and every configured workdir**, including writable and read-only workdirs. Never paste it into a chat, documentation, repository, agent event or public CLI invocation.

## Deployment

1. Create or reuse a named Tunnel via Cloudflare Dashboard or its authenticated API. For current VPS staging, reuse `serverfs-v024-dev` (`b8724123-72ed-4b97-9be3-0abf6700a77d`) and its configured hostname `vps-serverfs-mcp.ntlx.top`, with HTTP origin `http://serverfs-mcp:8000`. Do not create a duplicate Tunnel. Other deployments must select their own unique operator-owned hostname.
2. On the Linux **Docker host**, store the Tunnel Token in a separate operator-controlled secrets directory, outside Git and ServerFS workdirs. Make the token file mode 0600 and owned by the account configured as `CLOUDFLARED_UID:CLOUDFLARED_GID` (defaults 10001:10001); keep its parent directory private. The connector UID/GID can be set to the **operator's own** `id -u`/`id -g` to avoid privileged ownership changes. It is independent of `SERVERFS_UID:SERVERFS_GID` and does not change the MCP process identity. Confirm the bind mount can be read by that UID inside the container. Provide the absolute path only as `CLOUDFLARE_TUNNEL_TOKEN_FILE` in ignored `.env`; never put the token value itself in `.env`.
3. Example `.env` additions (placeholders, **not live credentials**):

   ```dotenv
   SERVERFS_HTTP_ALLOWED_HOSTS=serverfs-mcp:8000,vps-serverfs-mcp.ntlx.top
   SERVERFS_HTTP_API_KEY=<unique-long-random-key>
   SERVERFS_OAUTH_ENABLED=false
   CLOUDFLARED_IMAGE=cloudflare/cloudflared:2026.10.7
   CLOUDFLARED_UID=1000
   CLOUDFLARED_GID=1000
   CLOUDFLARE_TUNNEL_TOKEN_FILE=/home/operator/.config/serverfs/cloudflare-tunnel-token
   ```

   The example UID/GID `1000:1000` is specific to the verified VPS non-root operator; for another installation, set `CLOUDFLARED_UID` and `CLOUDFLARED_GID` to the token-file owner's actual numeric IDs without changing the ServerFS MCP identity. `2026.10.7` is the recommended version tag verified on Linux amd64 during v0.24.0 development, with amd64/arm64 manifest availability checked. It is an example, not an immutable digest lock or an automatic upgrade policy. Operators can override `CLOUDFLARED_IMAGE` and verify later versions independently. If the client's public HTTPS hostname is used, its HTTP Host value is ordinarily `vps-serverfs-mcp.ntlx.top` without `:443` — verify the actual request headers if a reverse-proxy configuration changes them.

4. Verify the overlay *before* touching running containers:

   ```sh
   docker compose -f compose.yml -f compose.cloudflare.yml config --quiet
   docker compose -f compose.yml -f compose.cloudflare.yml config --services
   ```

   The first command must succeed. The second should show `serverfs-mcp` and `cloudflared`, not activate `openai-tunnel` unless its explicit profile is selected. **Never use `docker compose config` without `--quiet` while real credentials or sensitive workdir paths are configured.**

5. For a **new, dedicated** deployment only, select a unique Compose project and a private, deployment-specific env file. Verify its workdir mounts and independent HTTP authentication, then use the full overlay set. For example, after setting up the intended image, workdirs and secrets in a private file (adapt these placeholders to your own installation):

   ```sh
   docker compose -p serverfs_cf_new --env-file /secure/path/new.env \
     -f compose.yml -f compose.cloudflare.yml config --quiet
   docker compose -p serverfs_cf_new --env-file /secure/path/new.env \
     -f compose.yml -f compose.cloudflare.yml pull serverfs-mcp cloudflared
   docker compose -p serverfs_cf_new --env-file /secure/path/new.env \
     -f compose.yml -f compose.cloudflare.yml up -d --no-build serverfs-mcp cloudflared
   ```

   **Never use this new-deployment command sequence against an existing production project.** Preserve `compose.agent.yml` and all other existing overlays whenever that project uses Agent Bridge / Repo Ops / OAuth. No service needs a public host-bound port in this Cloudflare-only containerized path.

6. If using **host-managed cloudflared** instead of the Compose connector, the origin may correctly be `http://127.0.0.1:8000` through `compose.http.yml`. This is a separate, user-managed deployment branch; do not start both owners for the same Tunnel without deliberate multi-replica design.

## Current staging hostname: Universal SSL coverage

The approved test-and-future-production hostname **`vps-serverfs-mcp.ntlx.top`** is a first-level subdomain of the operator's Cloudflare full-setup Free zone `ntlx.top`. The currently active Universal SSL certificate includes `*.ntlx.top`, so **no additional edge certificate purchase is needed**. The public HTTPS TLS handshake was subsequently verified from the VPS against Cloudflare (`ssl_verify_result=0`, with certificate validation enabled). This verifies the staging hostname's current certificate and reachability, not every future client environment.

The named Tunnel `serverfs-v024-dev` now reports `healthy` (one connected Linux amd64 Connector, cloudflared `2026.10.7`). The proxied CNAME for `vps-serverfs-mcp.ntlx.top` is active. It routes only to the isolated authenticated `serverfs_cf_staging` project. The production OpenAI Tunnel is unaffected.

Cloudflare reference: [Universal SSL](https://developers.cloudflare.com/ssl/edge-certificates/universal-ssl/).

## VPS non-disruptive staging handoff (2026-11-06)

The existing production `serverfs_mcp` Compose project is **not** a safe direct Cloudflare ingress target: v0.23.3 is running without ServerFS HTTP API Key/OAuth, with the existing OpenAI Tunnel and Agent/file ingress services. Do not add the Cloudflare overlay to this production project or run `up` against its active Compose file set.

An isolated project has been prepared and its MCP container started. The operator-owned file `/home/lx/.config/serverfs-cloudflare/staging.env` (0600) holds the staging-only strong API Key and connector path settings. The only workdir is `/home/lx/.local/share/serverfs-cloudflare-staging/readonly`, a private test fixture with no access to production workdirs. No host port is published and the production Docker networks are not shared. The staging MCP container is healthy and its private HTTP authentication has been tested: missing/incorrect Bearer returns 401; correct Bearer returns 200 and a session header. Real public Streamable HTTP JSON-RPC tests passed `initialize`, `tools/list`, `list_workdirs` (one read-only fixture) and `read_text_file` (harmless fixture content). Public attempted mutation and relative path traversal were denied. An early ad hoc SDK-client probe used an incompatible context-invocation pattern, so its `TypeError` was not counted as a product defect; subsequent public protocol and direct ChatGPT tests passed.

The operator has installed the Cloudflare Tunnel Token privately at `/home/lx/.config/serverfs-cloudflare/tunnel-token`, outside all ServerFS workdirs; file existence, non-symlink status, mode 0600 and owner 1000:1000 were checked without reading its contents. Do not paste the token into ChatGPT, Agent prompts, argv, Git or workdirs. The environment file contains only the external token **path**, not its value. Reuse the existing Tunnel rather than creating a duplicate.

For reproducible **future** staging reactivation, verify token ownership/mode **without reading its contents**, verify the isolated Compose model and, only when authorized, start **only** the connector (the command below was already executed successfully; do not repeat merely to inspect status):

```sh
cd /home/lx/ServerFS_MCP
docker compose -p serverfs_cf_staging \
  --env-file /home/lx/.config/serverfs-cloudflare/staging.env \
  -f compose.yml -f compose.cloudflare.yml config --quiet
# After verifying independent caller authentication and staging network isolation:
docker compose -p serverfs_cf_staging \
  --env-file /home/lx/.config/serverfs-cloudflare/staging.env \
  -f compose.yml -f compose.cloudflare.yml up -d --no-deps cloudflared
```

The Connector requires no host binary or published port. **Verified staging acceptance** covers:

- Proxied DNS and HTTPS TLS, unauthorized HTTP 401 and authenticated MCP initialization.
- Direct ChatGPT `vps-serverfs` calls: `list_workdirs`, `list_directory`, `stat_file`, `read_text_file`, `find_files` and `search_text` on the benign read-only fixture; `.env` and path-traversal reads were refused.
- A real public SSE GET with `text/event-stream`, at least 65 seconds of stream lifetime, an observed event/heartbeat and three same-session `tools/list` calls.
- One controlled restart of only the staging Connector; the original MCP Session ID remained valid for another `tools/list` on bounded retry #2, without restarting staging MCP or changing any production container ID.

These tests do **not** establish production workdir/Agent access or lossless replay of in-flight SSE events. A separate `Python-urllib/3` probe returned HTTP 403, while the regular MCP client identifier succeeded; the exact enforcement layer is unresolved, so no broad Cloudflare security-policy relaxation is justified.

**Protocol note:** This deployment may return an SSE response (`text/event-stream`) even for `initialize` POST; a compliant test client must parse that response rather than assuming JSON only. For a separate long-lived GET stream, complete the `notifications/initialized` handshake first. The first public GET headers may take approximately 15 seconds to arrive, so overly aggressive client read timeouts produce false negatives.

**Runtime security evidence:** the staging Connector actually runs non-root with read-only root filesystem, capability drops, no-new-privileges, a readonly Token-only mount, no host port bindings and no ServerFS API Key environment variable. The MCP container has only internal Docker networks `serverfs_internal` and `serverfs_file_ingress` (the latter remains defined for optional file-ingress compatibility), all workdir bind mounts read-only, and no connection to the Connector's `tunnel_egress` network. No production container shares the staging private networks. The first topology probe assumed only one private network and was corrected after consulting the base Compose schema; the actual separation passed.

**Credential note:** the ChatGPT connector was configured with a URL query API Key for compatibility. URL query secrets can be recorded by intermediaries; prefer OAuth or Bearer support for production and rotate the staging key before any broader access. Deployment does not imply authority to commit, push or publish the v0.24.0 release.

## Remote MCP client URL and authentication

The currently live **isolated staging** URL is `https://vps-serverfs-mcp.ntlx.top/mcp`. Its DNS and HTTPS handshake have been verified, but it does not yet expose production ServerFS workdirs or Agent capabilities. Prefer the ServerFS OAuth 2.1 flow when supported and properly configured, or `Authorization: Bearer <ServerFS HTTP API Key>` when the MCP client allows it. For clients proven to accept only a URL, ServerFS supports `/mcp?key=<ServerFS HTTP API Key>`, but **Cloudflare, browser history, WAF and intermediate systems may record the entire URL**. Assess that risk before use and rotate the secret on any suspected exposure.

Cloudflare Access interactive login is not automatically compatible with headless MCP clients. Service Tokens also require a confirmed client-side authentication mechanism. Do not assume a Cloudflare Tunnel means Access is enabled or that Access can replace the ServerFS API Key.

## Verification ladder

1. **Configuration / policy:** Compose render succeeds; both transport paths are explicitly distinguished; no unexpected public host port; secrets stay off the workdir mounts and connector environment; ServerFS Host/Origin restrictions remain enabled.
2. **Connector:** after startup, `cloudflared` reports connected status. Review only recent, redacted log lines; a running container alone is insufficient. Confirm DNS maps the intended hostname and that the public URL responds over HTTPS.
3. **Auth before MCP:** send a valid `initialize` JSON-RPC POST without credentials and confirm 401 in API Key mode; wrong credential 401; correct credential is accepted. Do not interpret an empty `curl -X POST` response as MCP protocol validation.
4. **MCP protocol:** initialize with compatible `Accept: application/json, text/event-stream` and `Content-Type: application/json`, continue the negotiated MCP session, then `tools/list`, `list_workdirs` and `read_text_file`.
5. **Authorization:** verify readonly workdir denial of mutation and denial of reading credential files (e.g. `.env`), without revealing their content.
6. **Transport resilience:** verify streaming/SSE behavior, session continuity, connector restart/reconnect, and large transfers or Agent tasks only when these are explicitly enabled. Record specific results; do not assert they passed on source inspection alone.
7. **No regressions:** validate the legacy OpenAI Tunnel and generic HTTP Compose render, check that existing Agent/Repo Ops mounts survive when relevant, and confirm no production deployment changed during source development.

For external troubleshooting, distinguish (a) hostname/DNS, (b) Cloudflare Tunnel connectivity, (c) connector-to-origin Host/network mapping, (d) ServerFS authentication/Host/Origin, and (e) MCP session/response handling. Do not disable ServerFS security checks as a workaround.

## Rollback

For a dedicated Cloudflare-only deployment, disable the intended public hostname route in Cloudflare, then remove/stop only that `cloudflared` service; preserve the MCP instance and any prior ingress owners. A source rollback removes the optional `compose.cloudflare.yml` from the Compose file set; nothing in base `compose.yml` was changed. Rotate/revoke the Tunnel Token if compromised. Never delete unrelated Tunnels or DNS routes.

## External resources

- [Cloudflare Tunnel — setup](https://developers.cloudflare.com/tunnel/get-started/)
- [Cloudflare cloudflared run parameters, including token-file](https://developers.cloudflare.com/tunnel/reference/run-parameters/)
- [Cloudflare Tunnel routing](https://developers.cloudflare.com/tunnel/concepts/routing/)
- [Cloudflare Tunnel troubleshooting](https://developers.cloudflare.com/tunnel/troubleshooting/)
- [ServerFS static API Key](api-key.md)
- [ServerFS OAuth 2.1](oauth.md)
