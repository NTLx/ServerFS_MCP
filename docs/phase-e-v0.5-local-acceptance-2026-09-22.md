# v0.5 local file-ingress acceptance — 2026-09-22

## Scope

This record captures the current pre-ChatGPT acceptance evidence for ServerFS v0.5 file
ingress and the repaired whole-file Base64 transport ceiling.

The final disposable run used the current `v0.5-dev` source, scratch image
`serverfs-mcp:v05-gate2`, a unique temporary Compose project, and a temporary read-write
workdir below `/tmp`. It did not use the repository production `.env`, did not start
`openai-tunnel`, and did not inspect, stop, restart or recreate the deployed ServerFS
project.

All disposable containers, networks, workdirs, env files and probe scripts were removed
after the run. The scratch image was retained only for subsequent validation.

## Root regression and build gate

Executed against the current v0.5 development tree:

- `uv sync --frozen`: pass;
- `uv run ruff check .`: pass;
- `uv run ruff format --check .`: pass, **115 files already formatted**;
- `uv run pytest`: **790 passed in 50.23s**;
- `git diff --check`: pass;
- base Compose render: pass;
- `file-ingress` profile Compose render: pass;
- `compose.yml + compose.agent.yml` render: pass;
- scratch Docker build: pass as `serverfs-mcp:v05-gate2`;
- scratch image package version: **0.5.0**;
- scratch image ID:
  `sha256:fc5ac233e8bd4353b0dde20641fc9d8e2b112d8f0bd2a959879a209438555a93`.

Additional release gates also passed:

- site: `npm ci` succeeded with **0 vulnerabilities** and `npm run build` generated
  **17 static pages**; `git diff --check` passed and tracked Git status was unchanged;
- frozen Agent Bridge: `uv sync --frozen`, Ruff lint, Ruff format and **22 tests** all
  passed; root `git diff --check` passed and tracked Git status was unchanged.

The public `upload_binary_file` descriptor was inspected through the real MCP surface
with file ingress enabled:

- `file` is present and resolves through `$defs/OpenAIFileInput`;
- the file object declares exactly `download_url`, `file_id`, `mime_type`,
  `file_name` as string properties;
- only `download_url` and `file_id` are required;
- `additionalProperties=false`;
- tool metadata is exactly `{"openai/fileParams":["file"]}`.

## Disposable runtime topology

Only the two v0.5 services were started:

```text
serverfs-mcp
  -> serverfs_internal          (internal=true)
  -> serverfs_file_ingress      (internal=true)

serverfs-file-ingress
  -> serverfs_file_ingress      (internal=true)
  -> file_ingress_egress        (egress)
```

Both containers reached `healthy`.

Runtime inspection additionally verified:

- `serverfs-mcp` had no egress/non-internal network;
- `serverfs-file-ingress` had no workdir mounts;
- the sidecar environment contained no `CONTROL_PLANE`, `OPENAI` or `TUNNEL`
  credential variables;
- `openai-tunnel` was not started.

From inside `serverfs-mcp`, a direct request to `https://example.com/` failed with:

```text
URLError: [Errno -3] Temporary failure in name resolution
```

This is runtime evidence that adding file ingress did not grant the MCP container direct
Internet egress.

## Real Streamable HTTP file-parameter round trip

A client connected over the real Streamable HTTP MCP endpoint:

```text
http://serverfs-mcp:8000/mcp
```

The request therefore used the same `serverfs-mcp:8000` authority accepted by the
production transport-security policy.

The client called:

```text
upload_binary_file
  workdir = e2e
  path = ingress-example.html
  file.download_url = https://example.com/
  file.file_id = file_e2e
  file.mime_type = text/html
  file.file_name = ../../ignored.html
```

Result:

- bytes written: **559**;
- SHA-256:
  `ff67a9d764d6a2367a187734e697f6a53217db9a21c101d410a113ca871a299d`.

A subsequent `download_binary_file` returned:

- 559 decoded raw bytes;
- the same SHA-256;
- content containing `Example Domain`;
- exact upload/download size and SHA-256 equality.

No path derived from `file_name` was created. The explicit ServerFS `path` remained
authoritative.

With only `example.com` allowlisted, a file parameter using
`https://www.example.com/` failed with:

```text
FILE_INGRESS_HOST_NOT_ALLOWED
```

No target file was published.

The successful test file was later removed through revision-guarded `delete_file`.

## Real >4 MiB Base64 transport round trip

The same real Streamable HTTP MCP path uploaded a deterministic **5 MiB** raw payload
through `data_base64`. This request is materially larger than the pinned MCP SDK's
historical 4 MiB default HTTP request-body limit and therefore exercises the v0.5
transport-capacity fix rather than an in-process helper.

Evidence:

- raw bytes: **5,242,880**;
- SHA-256:
  `f9bbbc9cb6b8568b3611b0f3e138c92ca5d75eca70255452ab9100eaa59f0526`;
- `upload_binary_file` succeeded with exactly 5,242,880 bytes;
- `download_binary_file` returned exactly 5,242,880 decoded bytes;
- source, upload metadata, download metadata and decoded download SHA-256 matched exactly.

The 5 MiB test file was also removed through revision-guarded `delete_file`.

A first high-level Python MCP SDK probe reached the server successfully but encountered a
client-side SSE stream-teardown problem after the large transfer. The authoritative
byte-integrity check was therefore repeated using a minimal standards-level
JSON-RPC/SSE client over the same Streamable HTTP endpoint; that run completed all calls
and the exact-byte checks above. No ServerFS source or container configuration was
changed between the two probes.

## Security negative coverage

In addition to the disposable runtime checks, automated tests cover:

- HTTPS-only and port-443-only temporary URLs;
- exact hostname allowlists with wildcard rejection;
- userinfo/fragment rejection;
- DNS failure handling;
- rejection if any resolved address is loopback, private, link-local or otherwise
  non-global;
- public-address acceptance;
- redirect target revalidation and redirect limits;
- Content-Length oversize rejection before body reading;
- streaming oversize rejection without Content-Length;
- independent MCP-side and sidecar-side byte ceilings;
- fixed MCP-to-sidecar host/port/path with no internal redirect following;
- agent-safe sidecar error mapping without reflecting signed URLs;
- Compose isolation: no sidecar workdir mounts/credentials/published ports and no MCP
  egress network.

## Cleanup

After the final run:

- revision-guarded test files: absent;
- disposable containers: absent;
- disposable networks: absent;
- temporary env/workdir/probe files: absent;
- scratch image `serverfs-mcp:v05-gate2`: retained for later validation only.

No production container was started, stopped, recreated or modified.

## Post-acceptance hardening addendum

After the container acceptance above, the MCP-to-sidecar client was narrowed further:
it now uses `http.client.HTTPConnection` to the fixed internal
`serverfs-file-ingress:8081/fetch` endpoint and therefore cannot follow an internal HTTP
redirect to another destination. The OpenAI-facing sidecar redirect logic is unchanged and
still revalidates every upstream HTTPS redirect.

The targeted gate on this post-hardening tree passed with **80 tests**, Ruff lint green and
Ruff format green, covering binary upload, ingress URL/DNS/redirect/size policy, the fixed
internal endpoint, Compose isolation, configuration and MCP transport-body sizing.

The full release gate was subsequently rerun on the exact post-hardening tree at
`f1d6005d0f74d167c17dcba077ea0ed83eb7eb36`: root **790 passed**, Agent Bridge
**83 passed**, Ruff lint/format passed, the site built **17 pages**, all four required
Compose render variants passed, the scratch image built successfully, and the installed
`serverfs-mcp` package reported version **0.5.0**. The worktree was clean.

## Live ChatGPT file-parameter E2E — corrected final evidence

The refreshed ChatGPT plugin exposed the new `file` parameter and real conversation-held
files reached the MCP file-ingress path.

The first acceptance attempt treated one measured Azure Blob hostname as if it were a
stable deployment property. Later repeated probes disproved that assumption, so that
single-host conclusion is superseded by the evidence below.

Two different ChatGPT-held files produced different normalized storage hosts:

```text
oaisdmntprindiasocentral.blob.core.windows.net
oaisdmntprwestcentralus.blob.core.windows.net
```

The older file was later diagnosed through a temporary egress probe and returned HTTP
403 from its original host with no redirect. That later observation is evidence only that
the old temporary URL was no longer usable at diagnosis time; no stronger cause is
claimed.

A newly generated 1,745-byte PNG was then tested immediately. Standard HTTPS access from
the diagnostic sidecar returned:

```text
HTTP_STATUS=200
FINAL_HOST=oaisdmntprwestcentralus.blob.core.windows.net
CONTENT_LENGTH=1745
READ_ONE=1
```

There was no redirect. This proved that current ChatGPT fileParams can be valid while the
Azure Blob account hostname varies across files/storage regions.

### Host-policy correction

v0.5 therefore retains exact administrator-configured hosts but no longer assumes that one
measured exact host is sufficient for ChatGPT. A separate, default-off
`SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS` switch admits only the measured OpenAI
Azure Blob account-name family:

- exact suffix `.blob.core.windows.net`;
- storage account label must begin with `oaisdmntpr`;
- at least one account-name character must follow the prefix;
- lowercase ASCII letters/digits only;
- total storage-account label length at most 24 characters.

This is deliberately narrower than `*.blob.core.windows.net`; generic wildcard host
configuration remains rejected. Every accepted hostname still goes through HTTPS/443
enforcement, all-global DNS validation, IP-pinned connection, original-host TLS
verification, redirect-by-redirect revalidation, and byte/time ceilings.

Targeted host-policy regression passed **90 tests** with Ruff lint/format and
`git diff --check` green. Scratch image
`serverfs-mcp:v05-host-family` built successfully as package version **0.5.0**.

### Fresh final byte-integrity round trip

The real sidecar was started from that host-family image with:

- exact-host allowlist empty;
- constrained OpenAI Blob family enabled;
- no workdir mounts;
- no published ports;
- one dedicated internal network plus its sidecar-only egress network.

A newly generated ChatGPT-held PNG then completed the real:

```text
ChatGPT file -> Plugin fileParams -> MCP -> isolated sidecar -> workdir
              -> download_binary_file
```

round trip.

Final evidence:

- source size: **2,066 bytes**;
- source SHA-256:
  `3dbdf9788a85abd4528439973842b1ea90c9466ee124fc5f2051fee5486c5eed`;
- source PNG signature: `89504e470d0a1a0a`;
- `upload_binary_file` bytes written: **2,066**;
- upload SHA-256: identical;
- `stat_file` size: **2,066**;
- `stat_file` MIME: `image/png`;
- `download_binary_file` size: **2,066**;
- download MIME: `image/png`;
- download SHA-256: identical;
- downloaded first eight bytes: `89504e470d0a1a0a`.

The final E2E file was removed through revision-guarded `delete_file`. The earlier
measurement/diagnostic target paths were also confirmed absent.

The MCP container boundary had already been re-verified on the v0.5 development
deployment: version **0.5.0**, Agent Bridge mounts present, only Docker
`internal=true` networks attached, no published host ports, direct HTTPS unavailable
from the MCP container, and a fresh tunnel session reporting `server_version=0.5.0`.
The host-family change is confined to the isolated sidecar and does not add MCP egress.

## Phase E disposition

**LIVE FILE-PARAMETER E2E COMPLETE.** The provider-neutral Base64 path, transport-size
repair, refreshed ChatGPT file discovery, region-varying temporary-host behavior,
constrained host-family policy, fresh byte-integrity round trip and main-container
no-egress boundary are all verified.

The final full repository gate was then rerun on the exact host-family-fix tree:
root **800 passed**, Agent Bridge **83 passed**, Ruff lint/format passed, the site built
**17 pages**, all four required Compose render variants passed, `git diff --check` passed,
and scratch image `serverfs-mcp:v05-final-gate` reported package version **0.5.0**
(image ID `sha256:a19604d43b38b998ef3626f5157127403f922fa3f232d4a609b6770e7f93c691`).

Phase E and all technical release gates were complete at this acceptance point. Git
closeout was completed afterward in Phase F; no `v0.5.0` tag had been created at the time
of this record.
