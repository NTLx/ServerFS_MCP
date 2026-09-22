# ServerFS MCP v0.5.0 development plan

Status: **COMPLETE / RELEASE-READY**

Final branch: `main`

Development baseline: `main@fcb5cc97455bab795dfe0f07e461531114edc7d3`

## 1. Goal

v0.5.0 closes the real ChatGPT binary-upload gap discovered after v0.4.0:

```text
ChatGPT file/image
  -> OpenAI file parameter
  -> bounded file ingress
  -> existing ServerFS binary mutation pipeline
  -> workdir file
```

The version MUST make generated/uploaded ChatGPT files usable as ServerFS binary-upload
inputs without asking the model to materialize multi-megabyte base64 in a tool argument.

The existing provider-neutral base64 upload remains supported as a compatibility path.

## 2. Non-goals

v0.5.0 does not add:

- generic URL download;
- generic HTTP proxying;
- shell or command execution;
- recursive filesystem mutation;
- background transfer workers;
- resumable/chunked uploads;
- arbitrary client-controlled destination names derived from `file_name`;
- Internet egress to the `serverfs-mcp` container.

## 3. Verified platform contract

OpenAI's current Plugin file-input contract requires a top-level tool input listed in
`_meta["openai/fileParams"]`. Each file object declares exactly the supported fields:

- `download_url: string` — required;
- `file_id: string` — required;
- `mime_type: string` — optional value, required schema property;
- `file_name: string` — optional value, required schema property.

ServerFS uses `download_url` only as an opaque, short-lived ingress locator. `file_id`,
`file_name`, and the URL itself are never used to choose a workdir path and are never
written to audit logs.

## 4. Public upload contract

`upload_binary_file` remains the only binary-upload mutation tool.

Inputs:

```text
workdir
path
data_base64?       # compatibility source
file?              # ChatGPT/OpenAI file source
overwrite=false
expected_revision?
```

Exactly one payload source MUST be supplied:

- `data_base64 is not None` XOR `file is not None`.

Important: the empty string is a valid base64 source representing a zero-byte file.

Errors:

- neither source -> `BINARY_SOURCE_REQUIRED`;
- both sources -> `BINARY_SOURCE_CONFLICT`;
- file source while ingress disabled/unavailable -> coded file-ingress error;
- fetched file larger than the effective workdir binary limit -> `BINARY_PAYLOAD_TOO_LARGE`.

All existing path, read-write, binary-capability, overwrite/revision, writer-lease,
hard-link, metadata-preservation and atomic-publication rules remain unchanged.

## 5. Capability exposure

The `file` input exists in the MCP input schema so the contract is explicit and
testable.

When file ingress is enabled, `upload_binary_file` advertises:

```json
{"openai/fileParams": ["file"]}
```

When ingress is disabled, that OpenAI-specific metadata is omitted. Base64 upload remains
usable by generic MCP clients.

## 6. Network architecture

The `serverfs-mcp` container MUST retain no Internet egress.

A narrowly scoped optional sidecar performs file ingress:

```text
                         Internet
                            ^
                            |
                      file_egress
                            |
                  serverfs-file-ingress
                            |
                serverfs_file_ingress
                       (internal)
                            |
                       serverfs-mcp
                            |
                  serverfs_internal
                            |
                      openai-tunnel
```

Properties:

- no workdir mounts on the ingress sidecar;
- no OpenAI API key or tunnel credential in the ingress sidecar;
- no published ingress port;
- ingress sidecar is opt-in through a Compose profile;
- `serverfs-mcp` remains connected only to Docker-internal networks;
- OpenAI tunnel is not attached to the dedicated ingress network.

## 7. File-ingress security contract

The sidecar is not a generic URL fetcher.

It MUST:

1. accept HTTPS only;
2. accept exact administrator-configured hostnames only;
3. reject userinfo and fragments;
4. allow only port 443 (implicit or explicit);
5. resolve DNS itself and reject every non-global address;
6. connect to the validated resolved address while preserving TLS SNI/certificate
   verification for the original hostname;
7. validate every redirect independently;
8. cap redirect count;
9. stream under a byte ceiling and reject oversized Content-Length early;
10. read at most `limit + 1` bytes when Content-Length is absent/untrusted;
11. use bounded connect/read timeouts;
12. never log URLs, query strings, file IDs, response bodies or workdir paths.

The exact allowed hostnames are configuration, not source-code guesses. A real ChatGPT
E2E probe MUST measure the current temporary-download hostname/redirect chain before
production enables this path.

## 8. Internal ingress protocol

`serverfs-mcp` calls the sidecar over the dedicated internal network:

```http
POST /fetch
Content-Type: application/json

{"download_url":"https://...","max_bytes":8388608}
```

Success:

```http
200
Content-Type: application/octet-stream

<raw bytes>
```

Failure responses are small JSON objects containing only a stable error code. The MCP
client maps them to agent-safe ServerFS errors without reflecting the URL.

Both sides enforce the byte limit. A sidecar bug must not allow the MCP process to read
more than the workdir's effective transfer ceiling.

## 9. MCP HTTP body-size consistency

v0.4.0 exposes an 8 MiB raw binary ceiling, while the pinned MCP SDK defaults to a 4 MiB
HTTP request-body ceiling. Whole-file base64 inflates by approximately 4/3, so the
transport default can reject a valid ServerFS payload before tool dispatch.

v0.5.0 MUST compute the Streamable HTTP request-body ceiling from the largest effective
enabled workdir binary limit:

```text
encoded = 4 * ceil(raw_bytes / 3)
request_body_limit = max(SDK_default, encoded + bounded_JSON_overhead)
```

This preserves the documented base64 compatibility contract. File-parameter ingress does
not carry raw file bytes in the MCP request and therefore does not consume that body
budget.

## 10. Concurrency and atomicity

Network retrieval occurs after authorization/path resolution but before the global
mutation lock and writer lease are acquired.

Reason:

- do not hold the process-wide mutation lock across network I/O;
- do not block all workdirs while a temporary URL is downloading;
- after retrieval, the existing lock + writer lease gate decides whether publication may
  proceed;
- if an Agent acquires the workdir writer lease while ingress is fetching, publication
  fails closed with `WORKDIR_BUSY`; downloaded bytes are discarded.

No file is partially published.

## 11. Configuration

New MCP-process settings:

```text
SERVERFS_FILE_INGRESS_ENABLED=false
SERVERFS_FILE_INGRESS_TIMEOUT_SECONDS=30
```

New sidecar settings:

```text
SERVERFS_FILE_INGRESS_ALLOWED_HOSTS=
SERVERFS_FILE_INGRESS_MAX_BYTES=8388608
SERVERFS_FILE_INGRESS_FETCH_TIMEOUT_SECONDS=30
SERVERFS_FILE_INGRESS_MAX_REDIRECTS=3
```

The MCP-to-sidecar endpoint is intentionally fixed to the Compose service
`http://serverfs-file-ingress:8081/fetch`; it is not an administrator-configurable URL.
File ingress is disabled by default. Enabling the MCP setting without a reachable sidecar
fails calls with a coded recoverable error, not silent fallback to model-generated base64.

## 12. Implementation phases

### Phase A — Contract and schema

> Status: **COMPLETE / LOCALLY VERIFIED**. The exact generated tool schema and
> `openai/fileParams` metadata match the documented OpenAI file-input shape.

- add `OpenAIFileInput`;
- make base64/file upload sources mutually exclusive;
- advertise `openai/fileParams` only when ingress is enabled;
- test the exact generated schema and metadata.

### Phase B — MCP ingress client

> Status: **COMPLETE / TARGETED POST-HARDENING VERIFIED**. The MCP process enforces its
> workdir byte ceiling again, maps sidecar failures to agent-safe codes without reflecting
> URLs, and now connects only to the fixed internal sidecar host/port/path through
> `HTTPConnection` (no internal redirect following). The current targeted v0.5 gate is
> 80 passed with Ruff lint/format green.

- add bounded internal HTTP client;
- map sidecar statuses/codes to ServerFS errors;
- keep URLs/file IDs out of logs;
- reuse existing binary mutation primitives unchanged.

### Phase C — Isolated ingress sidecar

> Status: **COMPLETE / CONTAINER VERIFIED**. A disposable Compose E2E proved the
> dedicated network topology, public-HTTPS fetch, exact-host rejection, byte ceiling and
> continued lack of Internet egress from the main MCP container. See
> `docs/phase-e-v0.5-local-acceptance-2026-09-22.md`.

- implement exact-host HTTPS fetcher;
- DNS/IP validation;
- TLS hostname verification with connection pinned to validated IP;
- redirect validation;
- size/time limits;
- no access logging of URLs;
- Compose profile + dedicated networks.

### Phase D — Transport capacity alignment

> Status: **COMPLETE / REAL HTTP VERIFIED**. The runtime derives and passes a request-body
> ceiling from the largest enabled binary workdir. A real Streamable HTTP MCP round trip
> uploaded and downloaded a deterministic 5 MiB raw payload through `data_base64`, with
> exact byte-count and SHA-256 equality. See
> `docs/phase-e-v0.5-local-acceptance-2026-09-22.md`.

- derive MCP request-body limit from effective workdir binary policy;
- wire the value into `mcp.run("streamable-http", ...)`;
- add boundary tests for the calculation and real ASGI rejection/acceptance behavior.

### Phase E — Integration and real ChatGPT E2E

> Status: **COMPLETE / LIVE CHATGPT VERIFIED**. The exact post-hardening tree passed the
> full root/Agent/site/Compose/image release gate. A refreshed ChatGPT plugin then supplied
> a real conversation-held PNG through the OpenAI `file` parameter. The temporary download
> hostname was measured with a no-egress reject probe, configured as the sole exact-host
> allowlist entry, and the real sidecar completed an exact 124-byte PNG round trip with
> matching SHA-256 and PNG signature. The MCP container remained without Internet egress.
> See `docs/phase-e-v0.5-local-acceptance-2026-09-22.md`.

- run unit/full regression gates;
- build Compose image;
- exercise disposable sidecar with controlled HTTPS fixtures where practical;
- deploy development edge stack;
- refresh ChatGPT plugin schema;
- verify `upload_binary_file` exposes the file parameter;
- measure the actual ChatGPT temporary URL hostname/redirect chain;
- configure the narrow exact-host allowlist;
- upload a real generated PNG through ChatGPT;
- verify size, SHA-256 and PNG signature from the ServerFS download path;
- verify no Internet egress from `serverfs-mcp`.

### Phase F — Release closure

> Status: **COMPLETE / RELEASE-READY**. Runtime docs/config examples, root regression,
> Compose renders, scratch image build, container-isolation E2E, explicit >4-MiB HTTP
> compatibility, site build, independent Agent Bridge regression, refreshed-plugin
> discovery and live ChatGPT file-parameter E2E are all green. The development history was
> fast-forwarded into `main`, development branches were removed, and the release-candidate
> evidence is frozen in `docs/phase-f-v0.5-release-candidate-2026-09-22.md`. The
> `v0.5.0` tag remains intentionally uncreated until the maintainer explicitly requests it.

- update README/site docs/config examples;
- document migration and rollback;
- full root + Agent Bridge regression;
- Compose render/build/security checks;
- merge to main with linear history;
- prepare, but do not create, `v0.5.0` tag until explicitly requested.

## 13. Acceptance criteria

v0.5.0 is release-ready only when all of the following are true:

- generic base64 upload still round-trips exact bytes;
- valid empty base64 still creates a zero-byte file;
- ChatGPT file object schema passes the documented file-input shape;
- file and base64 source conflicts fail closed;
- file ingress is opt-in;
- main MCP container still has no Internet egress;
- ingress sidecar has no workdir mounts and no OpenAI credentials;
- arbitrary/private/loopback/link-local hosts cannot be fetched;
- every redirect is revalidated;
- byte ceilings are enforced on both sides;
- writer lease/overwrite/revision semantics are unchanged;
- MCP HTTP body size admits the advertised whole-file base64 limit;
- real ChatGPT generated-image -> ServerFS workdir E2E succeeds with byte-integrity evidence;
- full tests/lint/Compose/build/security gates pass.
