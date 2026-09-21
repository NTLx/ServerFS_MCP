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

`upload_binary_file` supports:

1. create-only publication when `overwrite=false`
2. revision-guarded atomic replacement when `overwrite=true`

Replacement requires the caller to provide the current `expected_revision`.

Binary transfer never bypasses workdir authorization. Upload still requires a read-write workdir.

## Limits

`SERVERFS_MAX_BINARY_TRANSFER_BYTES` and the per-workdir override bound both directions. The default is 8 MiB.
