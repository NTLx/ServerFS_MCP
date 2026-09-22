# v0.5.0 Phase F release-candidate acceptance — 2026-09-22

## Disposition

ServerFS MCP v0.5.0 has **completed the technical release gates, Git closeout, live
ChatGPT acceptance, and release-facing documentation alignment**. Creating and pushing
the `v0.5.0` tag remains the only maintainer-controlled publication action.

The earlier release-candidate draft was written before repeated real ChatGPT file probes
demonstrated region-varying Azure Blob account hostnames. Because no `v0.5.0` tag had
been created, development was safely reopened on branch `v0.5-host-family-fix` from
`main@ea939fee75a1f5f119a761e110b92d08bdf25533`.

The corrected file-ingress policy, fresh live byte-integrity E2E, full regression,
site/Compose gates, final scratch image build, main-only Git closeout, and the final
README/site/config/deployment documentation sweep are complete. Tagging remains a
maintainer-controlled publication action and is not part of this development pass.

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
- exact administrator-configured hosts and an optional constrained OpenAI Azure Blob account-name family; generic wildcards remain rejected;
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

The pre-host-family post-hardening tree passed a complete local release gate:

- `uv sync --frozen`: pass;
- root Ruff lint: pass;
- root Ruff format check: pass;
- root tests: **790 passed**;
- Agent Bridge frozen-contract gate: Ruff lint/format pass and **83 passed**;
- site: build pass, **17 static pages**;
- `git diff --check`: pass;
- all four required Compose render variants: pass;
- scratch Docker image build: pass;
- scratch image package version: **0.5.0**.

The host-family correction then passed its targeted gate: **90 passed**, Ruff lint/format
green, `git diff --check` green, and scratch image
`serverfs-mcp:v05-host-family` built successfully as package version **0.5.0**.

The final full release gate on the exact corrected tree then passed:

- root tests: **800 passed**;
- root Ruff lint/format: pass;
- Agent Bridge frozen-contract tests: **83 passed** with Ruff lint/format pass;
- site build: **17 pages**;
- `git diff --check`: pass;
- all four required Compose render variants: pass;
- final scratch image build: pass;
- package version: **0.5.0**;
- final scratch image ID:
  `sha256:a19604d43b38b998ef3626f5157127403f922fa3f232d4a609b6770e7f93c691`.

A later main-only Agent-delegation guidance change added two public-tool description/schema
regression tests and passed another complete gate before the release-documentation sweep:

- root tests: **802 passed**;
- root Ruff lint/format: pass;
- Agent Bridge: **83 passed** with Ruff lint/format pass;
- site build: **17 pages**;
- all four Compose render variants: pass;
- `git diff --check`: pass;
- scratch image build: pass;
- image package version: **0.5.0**.

The final release-documentation closeout then passed:

- `git diff --check`: pass;
- site build: **17 pages** with no Markdown code-language warnings;
- generated English/Chinese static output contains `v0.5.0`, the generic GitHub Releases
  link, and the documented file-ingress + Agent-overlay Compose path;
- current user-facing stale-version/release-string scan: zero findings;
- browser-rendered visual inspection: **Not verified** because no browser runtime was
  available without installing additional software.

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

## Live ChatGPT file-parameter E2E — corrected evidence

The refreshed ChatGPT plugin exposed the new `file` parameter. Repeated real-file probes
then found that the temporary Azure Blob account is not stable across files/storage
regions. Two observed hosts were:

```text
oaisdmntprindiasocentral.blob.core.windows.net
oaisdmntprwestcentralus.blob.core.windows.net
```

The earlier release-candidate draft incorrectly treated one measured exact host as a
stable production policy. That conclusion is superseded.

A later diagnostic access to an older temporary URL returned HTTP 403 from its original
host with no redirect. A newly generated 1,745-byte PNG tested immediately returned HTTP
200, no redirect, `Content-Length: 1745`, and a readable first byte. This distinguishes
temporary-URL lifetime from the host-policy problem and proves that a fresh ChatGPT file
parameter is directly fetchable.

The corrected policy retains exact host allowlists and adds a separate default-off
`SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS` switch. When enabled, it accepts only the
measured OpenAI Azure Blob account-name family: exact `.blob.core.windows.net` suffix,
storage-account label beginning `oaisdmntpr`, lowercase ASCII alphanumeric account name,
non-empty suffix after the prefix, and Azure's 24-character account-name maximum. Generic
host wildcards remain rejected.

The real sidecar was then run from scratch image `serverfs-mcp:v05-host-family` with the
exact-host list empty and the constrained family enabled. A brand-new conversation-held
PNG completed the real ChatGPT -> Plugin -> MCP -> isolated sidecar -> ServerFS workdir ->
MCP download path:

- source size: **2,066 bytes**;
- source PNG signature: `89504e470d0a1a0a`;
- source SHA-256:
  `3dbdf9788a85abd4528439973842b1ea90c9466ee124fc5f2051fee5486c5eed`;
- upload bytes written: **2,066**;
- upload SHA-256: identical;
- `stat_file` size: **2,066**;
- `stat_file` MIME: `image/png`;
- download size: **2,066**;
- download MIME: `image/png`;
- download SHA-256: identical;
- downloaded PNG signature: `89504e470d0a1a0a`.

The final test file was removed by revision-guarded `delete_file`, and all measurement
and diagnostic target paths were confirmed absent.

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

The original `v0.5-dev` history was fast-forwarded into `main`, then development was
reopened before tagging when region-varying host behavior was measured. The focused
`v0.5-host-family-fix` branch was completed, passed its corrected-tree gate, and was
pure-fast-forwarded into `main`. The temporary development branches were then removed.
Subsequent release-preparation work, including Agent-delegation guidance and the final
release-facing documentation sweep, continued directly on `main` as requested. No history
rewrite is required.

## Release point

No `v0.5.0` tag existed when this release-candidate record was finalized. The tag target
must be the final clean `main` commit after the release-documentation closeout, and the tag
must remain uncreated until the maintainer explicitly requests publication.
