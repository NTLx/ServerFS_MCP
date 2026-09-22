# v0.5.0 Phase F release-candidate acceptance — 2026-09-22

## Disposition

ServerFS MCP v0.5.0 is **release-ready**.

Implementation, regression, container/security validation, real Streamable HTTP validation,
refreshed ChatGPT plugin discovery, live ChatGPT file-parameter transfer and repository
closeout have completed. The release tag has intentionally **not** been created; tagging is
a maintainer-controlled final publication action.

The development branch was fast-forwarded into `main`; local and remote `v0.5-dev`
were removed. Before this final release-candidate record, `main` and `origin/main`
were exactly:

```text
b7f881f3f8fb01330b9602343612426aa3b520be
```

This record itself is the final documentation-only release-candidate change after that
merge.

## Product change

v0.5.0 closes the ChatGPT binary-upload gap left by v0.4.0.

`upload_binary_file` now accepts exactly one whole-file source:

- strict RFC 4648 `data_base64`, retained for generic MCP clients; or
- an OpenAI/ChatGPT `file` parameter advertised through
  `_meta["openai/fileParams"] = ["file"]` when optional file ingress is enabled.

The explicit ServerFS `path` remains authoritative. Client `file_name`, `file_id` and
the temporary URL never select a filesystem destination.

## Security architecture

The main `serverfs-mcp` container still has no Internet egress and no published ports.

Optional ChatGPT file ingress is isolated in `serverfs-file-ingress`:

- no workdir mounts;
- no OpenAI/tunnel/control-plane credentials;
- no published ports;
- one dedicated Docker-internal network shared with MCP;
- one sidecar-only egress network;
- exact administrator-configured host allowlist;
- HTTPS port 443 only;
- DNS results must all be globally routable;
- outbound connection pinned to a validated IP while TLS verifies the original hostname;
- every upstream redirect is independently revalidated;
- independent byte and timeout ceilings;
- no URL/query/file-ID logging.

The MCP-to-sidecar client is narrower still: it is fixed to
`serverfs-file-ingress:8081/fetch` through `HTTPConnection`, is not administrator
configurable, and does not follow redirects.

Network retrieval happens before the existing mutation lock/writer lease. Publication
continues through the frozen binary create/revision-guarded replace primitives, so Agent
writer leases, path policy, overwrite/revision rules and atomicity remain unchanged.

## OpenAI file-input contract

The generated public tool schema was verified through the real MCP surface.

`OpenAIFileInput` declares exactly:

- `download_url: string` — required;
- `file_id: string` — required;
- `mime_type: string` — optional field;
- `file_name: string` — optional field;
- `additionalProperties=false`.

With ingress enabled, tool metadata is exactly:

```json
{"openai/fileParams":["file"]}
```

With ingress disabled, the OpenAI-specific metadata is absent and the Base64 path remains
available.

## Regression and build gates

The exact post-hardening source tree passed the complete local release gate:

- `uv sync --frozen`: pass;
- root Ruff lint: pass;
- root Ruff format check: pass;
- root tests: **790 passed**;
- Agent Bridge frozen-contract gate: Ruff lint/format pass and **83 passed**;
- site: build pass, **17 static pages**;
- `git diff --check`: pass;
- base Compose render: pass;
- `file-ingress` profile render: pass;
- Agent overlay render: pass;
- Agent overlay + `file-ingress` profile render: pass;
- scratch Docker image build: pass;
- scratch image package version: **0.5.0**.

The development image later served a real tunnel session reporting
`server_version=0.5.0`.

## Transport-capacity repair

The pinned MCP SDK's historical default request-body ceiling was smaller than the public
8 MiB raw whole-file Base64 contract.

v0.5 derives the Streamable HTTP request-body ceiling from the largest enabled workdir
binary-transfer limit.

A real Streamable HTTP MCP round trip uploaded and downloaded a deterministic **5 MiB**
raw payload through `data_base64`:

- raw bytes: **5,242,880**;
- SHA-256:
  `f9bbbc9cb6b8568b3611b0f3e138c92ca5d75eca70255452ab9100eaa59f0526`;
- upload/download byte counts and SHA-256 matched exactly.

## Isolated sidecar container E2E

Before live ChatGPT testing, a disposable container stack verified:

- the main MCP container had only internal networks;
- the sidecar had no workdir mounts;
- the sidecar held no provider/tunnel credentials;
- exact-host HTTPS download worked;
- a non-allowlisted hostname failed closed;
- binary byte limits were enforced;
- MCP direct Internet access failed.

A real MCP file-parameter round trip through the isolated sidecar also succeeded against a
controlled public HTTPS target, with exact upload/download size and SHA-256 equality.

## Live ChatGPT file-parameter E2E

The refreshed ChatGPT plugin exposed the new `file` parameter.

A temporary no-egress measurement probe was inserted on the internal sidecar endpoint. It
logged only the normalized hostname from the platform-supplied temporary `download_url`
and always returned `FILE_INGRESS_HOST_NOT_ALLOWED`.

A real ChatGPT-held file parameter reached that probe. The observed hostname was:

```text
oaisdmntprwestcentralus.blob.core.windows.net
```

This is evidence from this specific ChatGPT session, **not a product default**. It is not
hard-coded into source or configuration examples.

The real sidecar was restored with exactly that one hostname allowlisted. No additional
redirect hostname was required.

A deterministic PNG then completed the real ChatGPT -> Plugin -> MCP -> isolated sidecar
-> ServerFS workdir -> MCP download path:

- source size: **124 bytes**;
- source PNG signature: `89504e470d0a1a0a`;
- source SHA-256:
  `30efccdbf3648650a242e8ba64be465b574c075def9218a2185ad25ac9c3b1d3`;
- upload bytes written: **124**;
- upload SHA-256: identical;
- download size: **124**;
- download MIME: `image/png`;
- download SHA-256: identical.

The E2E workdir files were removed afterwards through revision-guarded ServerFS deletion.

## Live deployment boundary re-verification

During the v0.5 development deployment:

- `serverfs-mcp` ran version **0.5.0**;
- the Agent Bridge socket and writer-lock mounts remained present;
- `serverfs-mcp` was attached only to Docker `internal=true` networks;
- no host port was published;
- a direct HTTPS request from the MCP container failed with
  `socket.gaierror: Temporary failure in name resolution`;
- the OpenAI tunnel established a fresh MCP session reporting
  `server_version=0.5.0`;
- the real sidecar remained credential-free and workdir-free.

The test also exposed and documented an operational trap: an Agent-enabled recreate must
retain `compose.agent.yml`; using only the base Compose file silently removes Agent mounts
and collapses the running surface to Agent-disabled. The upgrade documentation now
preserves the overlay explicitly.

## Repository closeout

The v0.5 development history was merged by pure fast-forward. No merge commit or rewritten
history was introduced.

At merge verification:

- local `main` == `origin/main`;
- worktree was clean;
- the local development branch was removed;
- the remote development branch was already absent and its stale tracking reference was
  pruned;
- only `main` remained.

## Release point

After this release-candidate documentation is committed and pushed, the resulting `main`
HEAD is the intended source commit for the future `v0.5.0` tag.

Do **not** create, move or recreate the `v0.5.0` tag until the maintainer explicitly
requests publication.
