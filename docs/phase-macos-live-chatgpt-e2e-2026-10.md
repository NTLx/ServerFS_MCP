# Phase J — Live ChatGPT E2E over the official darwin-arm64 tunnel (record + run book)

**Date:** 2026-10-10
**Machine:** MacBook Air (Mac16,12), Apple M4 — macOS 27.0.1, native arm64, not Rosetta
**ServerFS commit:** `df94673e16927e4a27d22428ce693cac5f3ec665`
**Tunnel-client:** official v0.0.15, asset `tunnel-client-v0.0.15-darwin-arm64.zip`,
Mach-O 64-bit executable arm64

## What is already proven on this machine

1. **Bootstrap chain (Phase 0H, re-verified after Phase C gating):** the pinned
   `serverfs bootstrap tunnel-client` flow downloads the official release, verifies the
   pinned manifest SHA-256 and the asset digest from the trusted manifest, extracts the
   top-level runtime set (`tunnel-client`, `cloudflared`, manifest, LICENSE, NOTICE) under the
   user-owned data dir, and the client executes natively (`--version` →
   `0.0.15+a390c168ff1b…`). Architecture verified as arm64 Mach-O.
2. **Native MCP serving:** `serverfs serve` passes the measured darwin gate and runs the full
   filesystem surface over stdio (see the native filesystem acceptance record).
3. **stdio supervisor wiring:** `run_native_tunnel` spawns the tunnel-client as a stdio child
   of the ServerFS supervisor exactly as on Windows/Linux; the transport contract is unchanged
   from v0.12 (no new protocol surface).

## What requires the operator's live secrets

The remaining gate items need the operator's real OpenAI tunnel credentials and an interactive
ChatGPT session; they cannot be executed from a development context without them:

```text
ChatGPT  ->  official darwin-arm64 tunnel-client  ->  native ServerFS (darwin)
```

### Run book (operator, on the M-series Mac)

```bash
# 1. bootstrap the pinned client (already verified)
serverfs bootstrap tunnel-client

# 2. configure serverfs.toml with the real workdirs, then:
serverfs doctor --config serverfs.toml          # expect: darwin FD kernel, APFS, 0 FAIL

# 3. run the tunnel (stdio supervisor owns the MCP child)
serverfs tunnel --config serverfs.toml \
  --tunnel-id tunnel_... --api-key-file /path/to/key

# 4. in ChatGPT, connect the MCP integration and exercise:
#    - list_directory / read_text_file / search_text on a real workdir
#    - create_text_file + edit_text_file (revision CAS) when write-enabled
#    - upload_binary_file with a real ChatGPT fileParams attachment, verifying:
#        temporary OpenAI URL -> native ingress helper (AF_UNIX) -> upload -> SHA-256 exact
```

### Evidence to capture during the operator run

- doctor output before and after (platform line, APFS verdict, tunnel-client version)
- the ChatGPT-side tool transcripts for the exercised filesystem operations
- `upload_binary_file` SHA-256 equality against the source file
- confirmation that the temporary OpenAI URL never appears in any log line
  (the ingress client logs contain no URL by construction; grep the stderr log for `oaisdmntpr`
  / `blob.core.windows.net` and expect no hits)

## Security property preserved

ServerFS MCP itself never retrieves the ChatGPT temporary HTTPS files: on macOS the fetch
happens in the separate native ingress helper (`python -m serverfs_mcp.file_ingress`),
reachable only over a private AF_UNIX socket in the 0700 per-user runtime dir, with the full
v0.12 validation chain (HTTPS/443, host allowlist, OpenAI Blob family validation, DNS +
global-IP checks, IP pinning, hostname TLS, redirect revalidation, size limit) reused
unchanged.

**Documented macOS boundary:** the Linux helper runs inside a container network namespace;
the macOS helper is a separate user-owned process without that namespace isolation. The
process boundary, the private socket and the identical validation chain are the v0.13
guarantees; full kernel-network isolation is NOT claimed on macOS.
