---
title: Binary Transfer
description: Bounded whole-file binary download and revision-guarded upload.
---

Binary transfer is optional and disabled by default.

Enable it globally with `SERVERFS_BINARY_TRANSFER_ENABLED=true` or for a specific workdir with `WORKDIR_XX_BINARY_TRANSFER_ENABLED=true`.

## Download

`download_binary_file` returns:

- raw binary content through the MCP binary resource channel
- exact byte size
- best-effort MIME type
- SHA-256
- an opaque revision

The structured metadata is exposed through the tool output schema.

## Upload

`upload_binary_file` accepts exactly one whole-file source:

1. `data_base64`: strict RFC 4648 base64, retained for generic MCP clients
2. `file`: a ChatGPT/OpenAI file parameter when optional file ingress is enabled

Publication semantics are unchanged:

- `overwrite=false` creates only
- `overwrite=true` performs a revision-guarded atomic replacement and requires the current `expected_revision`

The explicit ServerFS `path` always selects the destination. Client metadata such as `file_name` and `file_id` never becomes a filesystem path.

### ChatGPT file ingress

File ingress is independently disabled by default. Set `SERVERFS_FILE_INGRESS_ENABLED=true` and start Compose with `--profile file-ingress`. Exact hosts can be configured in `SERVERFS_FILE_INGRESS_ALLOWED_HOSTS`. Real ChatGPT fileParams use region-varying OpenAI Azure Blob accounts, so `SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS=true` enables only the measured `oaisdmntpr<account-suffix>.blob.core.windows.net` family; generic host wildcards remain unsupported.

When enabled, `upload_binary_file` advertises `_meta["openai/fileParams"] = ["file"]`. The MCP process calls only the fixed internal `serverfs-file-ingress:8081/fetch` endpoint and does not follow internal redirects. The isolated ingress sidecar fetches the temporary HTTPS URL; the main ServerFS MCP container keeps no Internet egress. The sidecar has no workdir mounts, no OpenAI credentials or published port. It accepts exact hosts or the opt-in constrained OpenAI Blob family, then independently rejects non-global DNS answers, pins the connection to a validated IP while verifying TLS for the original hostname, and revalidates every upstream redirect.

Binary transfer never bypasses workdir authorization. Upload still requires a read-write workdir.

## Limits

`SERVERFS_MAX_BINARY_TRANSFER_BYTES` and the per-workdir override bound both directions. The default is 8 MiB. The MCP HTTP request-body ceiling is derived from the largest enabled raw binary limit so the base64 compatibility path is not accidentally constrained by the SDK's smaller default body limit.
