# macOS v0.25.0 — signed Shared Core preflight and managed runtime provenance

Date: 2026-11-15. Scope: **pre-registration/read-only management acceptance only**. No native ServiceManagement registration, production cutover or Git release occurred.

## Product implementation

- `src/serverfs_mcp/darwin_instance_shared_lifecycle.py` exposes `preflight_shared_core(identity, config_path, port)`. The existing `shared-start` reuses this exact gate before the first native register operation. The gate verifies the instance identity and codesign-verified App, signed Core Python pin, exact manifest/config/port, authenticated **live Bridge v2 owner grant**, Core registration state, absence of a conflicting launchd PID, and port inventory.
- `src/serverfs_mcp/darwin_instance_cli.py` exposes `--instance CORE shared-preflight --bridge-id BRIDGE --config core.toml --http-port PORT`. A success result is `state=ready-to-register` and `bridge_authenticated=true`; neither staging nor native registration follows from this status.
- `src/serverfs_mcp/darwin_shared_runtime.py` exposes `verify_managed_shared_core_python(python)`. An official installed interpreter named `bin/serverfs-core-python` must have an intact private `runtime-provenance.json`, matching Python and libpython SHA-256 values, and successful isolated imports. A different, non-managed development interpreter retains the original import probe and is **not** claimed as release-attested.
- `src/serverfs_mcp/darwin_instance_shared_staging.py` now fails before writing any Core instance files if a named managed interpreter has missing or invalid provenance.
- `darwin_instance_shared_lifecycle._signed_shared_core_python` checks official managed runtime provenance **after** signed App path/pin validation and **before** manifest, Bridge challenge, status or register. Failure messages do not copy credential or raw binary contents.
- The `shared-preflight` command does not modify ServiceManagement registration, tasks or grants. Authenticated Bridge v2 challenges can still claim normal nonce anti-replay state on the owner; “read-only” describes the service lifecycle and configuration boundary.

## Executed validation

| Acceptance | Measured outcome | Meaning |
| --- | --- | --- |
| New preflight, same-path shared-start, wrong identity, failed owner, occupied port and CLI | **103 passed, 0 failed** | Synthetic tests; Ruff and git diff check passed |
| Real signed TWO Core apps + independent live fake owner v2 Bridge, no Login Items registration | **1 passed in 43.70s** | Both Core preflights pass, owner offline refuses, restart recovers, owner revocation of B refuses B while A remains authorized |
| Official runtime provenance gating synthetic set | **96 passed, 1 skipped** | Corrupt managed runtime refused before stage/native manager; inactive opt-in install skipped |
| Real signed TWO Core apps + owner and **mode-tampered provenance** | **1 passed in 73.18s** | After successful stages, temporarily changing mode of disposable runtime-provenance.json 0600 → 0644 forces both preflights to refuse; restoring 0600 recovers; owner restart/revocation acceptance remains green |
| Final broad safe macOS regression (post-provenance changes) | **598 passed, 9 skipped, 0 failed** | Seven live/HTTP/runtime opt-ins explicitly disabled; Ruff, formatting and `git diff --check` passed; production PID/listener state unchanged |

The subsequent **real operator CLI subprocess** integration (`python -m serverfs_mcp.darwin_instance_cli ... shared-preflight`) also **passed once in approximately 88 seconds**: both signed cores returned `ready-to-register`/exit 0 while authorized, and revoked Core B returned exit 2 with a generic error that did not contain its private key. The pytest case itself checked conservative candidate cleanup and production PIDs. A separate post-test shell audit requested an ambiguously scoped/high-sensitivity permission; that permission was not granted and the delegated audit was cancelled. Consequently, the independent post-test shell audit must be recorded **not completed** rather than inferred from the green pytest result.

The real signed preflight tests created only random `shpf-*` Core candidates in the current user's instances directory because Foundation does not follow an overridden HOME. The completed tests un-staged these exact intact unregistered candidates conservatively. Production default Tunnel/Bridge/Cloudflare/HTTP launchd PIDs remained `1779, 1790, 99279, 99271` with unchanged start times. Temporary Bridge owner, synthetic grants, runtime, sockets, signed template build and scratch directories were cleaned. No native service was registered, started, stopped, or unregistered.

These are distinct overlapping test groups and must not be summed into one test count. The prior baseline before these changes was 587 passed / 8 skipped.

## Important limits

1. **Real two-Core SMAppService Login Items registration is still NOT accepted.** The earlier attempt failed before registration due to an interpreter mismatch, which has been corrected. A further live registration request was blocked by the execution safety gate; these non-registering tests do not circumvent or replace that acceptance.
2. SHA-256 provenance proves local metadata consistency, not cryptographic publisher authenticity. It does not hash every installed site-packages file; same-user rewrites and races remain a threat that release signing and immutable deployment must address.
3. The verification convention applies to the official installer filename `bin/serverfs-core-python`. Non-managed Python configurations are development/test paths, not production acceptance.
4. Public Cloudflare-to-ChatGPT end-to-end access, production runtime placement with exact immutable release Git SHA, and cross-platform Windows/macOS/Linux release parity remain outstanding.
5. This work did not commit, push, tag or modify the running user's default ServerFS service.
