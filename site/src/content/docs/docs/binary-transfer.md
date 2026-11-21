---
title: Binary Transfer
description: Enable bounded whole-file downloads and guarded uploads.
---

Binary transfer is optional. Enable it only if the client supports MCP binary resources or needs to upload raw bytes. Text-only workdirs do not need it.

## Enable transfer

| Platform | Configuration |
| --- | --- |
| Linux | `SERVERFS_BINARY_TRANSFER_ENABLED=true` or `WORKDIR_XX_BINARY_TRANSFER_ENABLED=true` |
| macOS / Windows | `binary_transfer_enabled = true` in native TOML `[defaults]` (all workdirs in that Core) |

No Bridge is needed for binary transfer itself. Ordinary workdir authorization still applies: downloads may use read-only roots, uploads require writable roots. If no workdir enables transfer, neither binary tool is exposed.

## Download

Call `download_binary_file` with a workdir alias and relative path. The result contains the file bytes as an MCP EmbeddedResource/BlobResourceContents (base64 on the wire) plus structured metadata: size, MIME type, SHA-256 and opaque revision. Use the binary resource, not the metadata, to reconstruct the file. Check the hash when integrity matters.

## Upload

Call `upload_binary_file` with alias, destination path and exactly one source:

- `data_base64`: complete file encoded as strict RFC 4648 base64.
- `file`: ChatGPT/OpenAI file parameter, only when file ingress is available.

`overwrite=false` is create-only and rejects an existing path. For replacement use `overwrite=true` and the current `expected_revision` from stat/download. Replacement is atomic and refuses multiply linked regular files. Do not supply a revision for create-only. The explicit destination path is authoritative; file name or ID in client metadata never selects a filesystem path.

## Optional ChatGPT file ingress

For Linux, add the `file-ingress` Compose profile to the selected deployment and set `SERVERFS_FILE_INGRESS_ENABLED=true`. Configure exact hosts in `SERVERFS_FILE_INGRESS_ALLOWED_HOSTS` or explicitly enable `SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS=true` for the constrained `oaisdmntpr<suffix>.blob.core.windows.net` family. Generic wildcards and non-global DNS addresses are rejected. The isolated sidecar fetches temporary HTTPS files; Core does not browse arbitrary URLs. See [Security](../security/) for the trust boundary.

The upload tool advertises `_meta["openai/fileParams"] = ["file"]` on the enabled ingress path. Do not assume a named native Core has this sidecar: named macOS HTTP instances disable shared file ingress, Windows native uses direct binary transfer. A separate macOS compatibility helper exists for its documented deployment path. Check the actual tool schema and readiness, not just an env switch.

## Size and context limits

Default raw size is 8 MiB. Linux global `SERVERFS_MAX_BINARY_TRANSFER_BYTES` provides the default, and a slot's `WORKDIR_XX_MAX_BINARY_TRANSFER_BYTES` can replace it for direct upload/download. File ingress enforces the smaller of the slot limit and sidecar global ceiling. Native TOML uses `max_binary_transfer_bytes` in `[defaults]`. The HTTP request-body limit accommodates the largest enabled raw transfer after base64 expansion.

Whole-file transfer is not chunked or resumable. Base64 adds roughly one third to byte size and may consume substantial client context. Agents should use the binary resource or file parameter when supported; avoid copying large base64 strings into conversational prompts. For text inspection, use bounded text reads instead. Verify size, SHA-256 and destination revision after transfer.
