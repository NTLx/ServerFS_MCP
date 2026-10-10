# macOS Native Deployment (v0.13)

ServerFS on macOS needs no package artifacts beyond the standard Python
distributions: the Darwin backend is pure Python over POSIX/Darwin
primitives (`fcopyfile`, `getpeereid`, `confstr` via minimal ctypes
bindings) — there is no `serverfs-macos-native` wheel, DMG, .pkg,
Homebrew formula, root helper or Xcode/Swift component, by design
(dev_plan_v0.13.md §18/§19).

## Supported platform

```text
Apple M-series Mac, native arm64 (no Rosetta), macOS 27 Golden Gate, local APFS
```

## Install

```bash
uv sync                                   # ServerFS product environment
uv sync --project agent_bridge            # Agent Bridge environment (separate venv)
serverfs bootstrap tunnel-client          # pinned official darwin-arm64 asset
serverfs doctor --config serverfs.toml    # platform/APFS/gate diagnostics
```

## Run

```bash
serverfs serve --config serverfs.toml     # plain stdio MCP service
# or, for ChatGPT:
serverfs tunnel --config serverfs.toml --tunnel-id tunnel_... --api-key-file KEY
```

When `SERVERFS_FILE_INGRESS_ENABLED=true`, `serve` spawns the native
file-ingress helper as an owned child; the helper serves a private
AF_UNIX HTTP socket in the per-user runtime directory (0700) and exits
if its supervisor disappears.

## Agent Bridge (launchd)

```bash
serverfs agent-bridge configure --config serverfs.toml --env-file .env
serverfs agent-bridge install --bridge-config "$HOME/Library/Application Support/ServerFS/agent-bridge/bridge.json"
serverfs agent-bridge start | stop | status | uninstall
```

The private `bridge.json` is derived/private state outside every exposed workdir, not a second
operator configuration. Workdir/runtime/lifecycle policy is owned by `serverfs.toml`; `configure`
creates or refreshes the private 0600 document from that policy plus the narrow Agent/Jev/proxy
values in `.env`. For a TOML-only change, `serverfs agent-bridge restart --config serverfs.toml`
synchronizes policy while preserving private material. If private Jev/proxy values also changed,
use `serverfs agent-bridge restart --config serverfs.toml --env-file .env`.
`serverfs doctor --config serverfs.toml` fails on policy drift, and a newly started native
`serve`/tunnel refuses to run until the two policy views agree.

The install command generates
`~/Library/LaunchAgents/com.ntlx.serverfs.agent-bridge.plist` from the
live environment (paths only — provider credentials and proxy secrets
stay in the Bridge's private config/runtime material), bootstraps it
into the user's gui domain with `launchctl bootstrap`, and enables
`RunAtLoad` + `KeepAlive`. `status` is a read-only `launchctl print`.

No root, no LaunchDaemon, no shell-profile dependency. launchd is a
service manager, not a containment kernel: no Job-Object equivalence is
claimed, and the Bridge's recovery guard keeps workspace-write failed
closed whenever provider state is unknown.
