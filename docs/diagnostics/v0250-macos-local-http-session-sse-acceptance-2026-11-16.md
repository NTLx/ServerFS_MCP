# ServerFS MCP v0.25.0 — macOS local HTTP session/SSE acceptance (2026-11-16)

## Scope and outcome

Added an opt-in, real-loopback Streamable HTTP acceptance test for the remaining
local authenticated session/SSE lifecycle gate. The test uses an ephemeral
loopback port, temporary in-process servers, an isolated empty workdir registry,
and a static synthetic bearer. It does not contact the public hostname or read
production credentials.

**Local gate: PASS.** An MCP SDK client completed authenticated stateful
`initialize` using protocol `2025-11-25`, received a valid session, maintained
one GET `text/event-stream` connection for more than 65 seconds with periodic
keepalive bodies, and closed it when the client context exited. Anonymous and
wrong bearer requests returned 401. After stopping and restarting the isolated
server on the same ephemeral port, the old session returned 404 and a fresh SDK
client established a distinct working session. No replay behavior was tested or
claimed. The server fixture verified its uvicorn thread stopped and closed each
listener.

## Evidence

- `SERVERFS_RUN_LOCAL_HTTP_SESSION_SSE_ACCEPTANCE=1 uv run pytest -q tests/test_streamable_http_session_sse_acceptance.py` — **1 passed**.
- `uv run pytest -q tests/test_api_key_e2e.py tests/test_mcp_http_wire_compat.py tests/test_http_auth.py tests/test_oauth_security.py` — **25 passed**.
- Ruff check, Ruff format check, and `git diff --check` passed for the touched test file.
- Final test records each GET independently and asserts exactly one GET during the hold. The SDK HTTP client uses a 3-second timeout for ordinary operations and an unbounded response read timeout for its long-lived SSE stream; this avoids an SDK read timeout before the server's periodic keepalive.

## Diagnostic attempts

The initial focused run failed with an actual pytest assertion, not a command
execution timeout: `time.monotonic() - started_at > 65` observed **62 seconds**.
The next run with a 75-second hold passed that elapsed-time assertion but failed
`body_messages >= 4` with **0** body messages. Inspection identified that the
test had set the SDK client's entire timeout to 3 seconds, shorter than the
server's first SSE ping; the SDK could close and reopen GET streams while the
observer overwrote a single global start time. The test now records per-GET
start/status/body/end data and uses an explicit long-stream read timeout. The
final passing run observed one GET, more than 65 seconds of continuous lifetime,
and at least four keepalive body messages.

## Production and public ingress boundary

No launchd lifecycle command, production configuration/credential, Cloudflare
API, DNS, Tunnel route, or Git state was modified. Read-only process inspection
on 2026-11-16 found the four existing launchd services loaded and their PIDs
alive: Tunnel `1779`, Bridge `1790`, HTTP Origin `40516`, Cloudflare Connector
`40543`. No Cloudflare control-plane health query was made during this local
test; the latest repository diagnostic records the existing Tunnel healthy on
2026-11-15.

The separate Mac-to-public Cloudflare HTTPS request remains blocked by edge
HTTP 403/WAF behavior as reported for this task. It was not bypassed or
re-probed. Previously completed ChatGPT Cloudflare MCP CRUD remains separate
evidence and does not satisfy this Mac-origin public SSE/session gate. Public
ingress, public >60-second SSE, and public restart/fresh-session acceptance
remain **NOT VERIFIED**.
