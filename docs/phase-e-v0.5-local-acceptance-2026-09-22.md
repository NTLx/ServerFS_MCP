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

## Live ChatGPT file-parameter E2E

The final Phase E gate was completed through the refreshed ChatGPT plugin and the real
OpenAI Secure MCP Tunnel.

A deterministic 124-byte PNG held by the ChatGPT conversation was supplied to
`upload_binary_file` through the OpenAI `file` parameter. A temporary no-egress probe
first replaced the real sidecar solely to measure the normalized hostname carried in the
platform-supplied `download_url`; the probe logged only the hostname and always rejected
the request. The measured hostname was:

```text
oaisdmntprwestcentralus.blob.core.windows.net
```

This hostname is **observed E2E evidence, not a product default**. It is not hard-coded in
source, Compose or `.env.example`, because the actual temporary-file host can vary by
platform deployment/region and must remain administrator policy.

The real isolated sidecar was then restored with an exact one-host allowlist containing
only that measured hostname. No additional redirect hostname was required: the same real
ChatGPT-held PNG uploaded successfully through `upload_binary_file(file=...)`.

Byte-integrity evidence:

- source file size: **124 bytes**;
- source SHA-256:
  `30efccdbf3648650a242e8ba64be465b574c075def9218a2185ad25ac9c3b1d3`;
- source PNG signature: `89504e470d0a1a0a`;
- `upload_binary_file` bytes written: **124**;
- upload SHA-256: identical;
- `download_binary_file` size: **124**;
- download MIME: `image/png`;
- download SHA-256: identical;
- source/upload/download byte identity therefore matched end to end.

The MCP container was also re-verified after the v0.5 development deployment:

- package/server version: **0.5.0**;
- both Agent Bridge mounts remained present;
- only Docker `internal=true` networks were attached to `serverfs-mcp`;
- no host ports were published;
- a direct HTTPS request from `serverfs-mcp` failed with
  `socket.gaierror: Temporary failure in name resolution`, confirming no Internet egress;
- the tunnel established a fresh MCP session reporting `server_version=0.5.0`.

The real sidecar remained isolated: no workdir mounts, no published ports, no
`CONTROL_PLANE`/`OPENAI`/`TUNNEL` credential variables, one dedicated internal network
plus its egress network, and the exact measured hostname allowlist.

All E2E workdir files were removed afterwards through revision-guarded `delete_file`.

## Phase E disposition

**COMPLETE.** The provider-neutral Base64 path, the isolated file-parameter path, the
transport-size repair, real ChatGPT file discovery, live temporary-host measurement,
exact-host policy, byte-integrity round trip, and main-container no-egress boundary have
all been verified. No OpenAI temporary-file hostname is inferred or hard-coded into the
product.
