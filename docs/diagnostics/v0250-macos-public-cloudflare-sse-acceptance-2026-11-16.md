# macOS v0.25.0 — public Cloudflare MCP authentication and SSE acceptance

**Date:** 2026-11-16 (Mac local). **Current deployed service:** existing `macbook-serverfs.ntlx.top` Cloudflare Tunnel, the independently deployed candidate HTTP Core on local `127.0.0.1:8765`. No additional hostname, Tunnel, security policy or service instance was created.

## Positive live public verification

The existing ChatGPT-connected Cloudflare ServerFS connector previously completed authenticated file CRUD, revision conflict rejection and binary file transfer against this hostname, independently cross-read through the preserved OpenAI Tunnel connector (`v0250-macos-cloudflare-cutover-2026-11-15.md`). This report adds **direct public HTTPS and Streamable HTTP SSE** from the Mac with its configured HTTP proxy explicitly bypassed and normal TLS verification.

1. **Anonymous edge/origin-auth reachability:** a one-time `curl --noproxy '*'` HTTPS HEAD request with a descriptive application User-Agent returned **HTTP 401** with a Cloudflare Ray ID and a public non-loopback remote endpoint. The previous Python-library request's 403 / Cloudflare 1010 was not reproduced with this legitimate client signature. No WAF or DNS configuration was changed. This HEAD check alone does not prove MCP or streaming.
2. **Authenticated MCP initialization:** loaded the current HTTP Core's existing API key solely in process memory from its owner-controlled, private mode-0600 HTTP environment file (regular, non-symlink, same UID). Sent to the original `/mcp` HTTPS hostname, using direct no-proxy routing, normal TLS and a descriptive application User-Agent. Response: **HTTP 200** with a session ID. The key and session ID were not logged, copied into the repository or displayed.
3. **Authenticated continuous SSE:** on that authenticated session, a single GET with `Accept: text/event-stream` and the proper session header returned **HTTP 200** and stayed open for **75.640 s**, with **five** observed SSE keepalive/comment lines. The first stream line arrived after **15.636 s**. There was **no GET reconnect, retry, service restart, or policy bypass**. The test deliberately closed the one stream after exceeding the 65-second gate.

## Failed attempts preserved as diagnostic evidence

An initial authenticated public initialize returned 200, but its GET attempt ended without stream headers after a **13.5-second** timeout; that attempt is **NOT** counted as streaming success. The subsequent single bounded re-check allowed the normal server 15-second keepalive interval and observed the first actual stream line at ~15.6 s, then five keeps-alives over ~75.6 s. This strongly supports a client read-timeout mismatch as the cause of the earlier attempt; it does not prove that all clients/regions or arbitrary long-lived SSE sessions behave identically.

Cloudflare's official Error 1010 explanation attributes that error to browser-signature policy (https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/). The distinct historical 1010 and current 200/401 observations are client-specific, not evidence that WAF controls were disabled.

## Boundaries and remaining release checks

**PASS for current production-hostname authenticated initialize + >60-second continuous SSE.** Not tested here: active production Cloudflare Connector restart/recovery, old-session persistence across restart, simultaneous clients or global edge regions, browser-based OAuth, or final release-matched v0.25.0 Wheel. Existing OpenAI Tunnel/Agent Bridge and Cloudflare HTTP/Connector services were not stopped/restarted, no secret was written to any workdir, and no Cloudflare API/DNS/WAF configuration was mutated.

Independent macOS Shared Core `SMAppService` multi-Core lifecycle and grant-revocation/re-authorization evidence is documented in the 2026-11-16 native acceptance reports. Current installed-wheel three-process E2E also passed for dirty-source `0.24.2`-metadata candidates, not a release SHA.
