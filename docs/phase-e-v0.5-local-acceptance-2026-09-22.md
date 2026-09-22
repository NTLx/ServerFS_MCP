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

## Remaining Phase E gate

This acceptance proves the current ServerFS/container path using the OpenAI file-object
shape and independently proves the repaired >4 MiB Base64 request path. It does **not**
yet prove the hostname and redirect chain used by a real ChatGPT-generated or
ChatGPT-held file.

The remaining v0.5 Phase E gate is therefore:

1. deploy a v0.5 development image behind the OpenAI Secure MCP Tunnel;
2. refresh/reconnect the ChatGPT plugin so the new tool descriptor is discovered;
3. pass a real ChatGPT-generated or ChatGPT-held file parameter;
4. measure the actual temporary-download hostname and every redirect hostname;
5. configure only those exact hosts;
6. upload a generated PNG and verify size, SHA-256 and PNG signature through
   `download_binary_file`;
7. re-confirm the production MCP container has no Internet egress.

Do not infer or hard-code OpenAI temporary file hostnames before that measurement.
