# ServerFS v0.24.1 — Native macOS Cloudflare Tunnel

Status: **planning only**. No source implementation or macOS Cloudflare acceptance has been performed. Depends on the v0.24.0 Linux Cloudflare release being published and verified. Owner of existing production OpenAI Tunnel must remain unchanged unless separately approved.

## Objective and platform boundary

Add an optional **native user-level Cloudflare Tunnel ingress on supported Apple Silicon/macOS 27** with the same ServerFS Streamable HTTP and independent API Key/OAuth admission contract as v0.24.0, **without Docker**. Preserve existing OpenAI Secure MCP Tunnel, stdio, Agent Bridge, Repo Ops, macOS SMAppService lifecycle, workdir restrictions and safe defaults. Do not claim Intel, Rosetta or untested macOS versions.

## Decisions and non-goals

- Reuse the existing `native/macos/service_manager` app-associated `SMAppService`/LaunchAgent ownership; no root/LaunchDaemon, uncontrolled plist, second external daemon, or mandatory login-shell startup.
- Use an **official, integrity-verified macOS arm64 cloudflared** executable staged in a per-user ServerFS installation location. Record tested upstream version and authenticated checksum/release provenance. No unverified `curl | sh`, global PATH dependency, or automatic silent upgrades.
- Tunnel is a remotely managed named Cloudflare Tunnel with an operator-configured Host routing to a **private native loopback Streamable HTTP origin**. The Connector Token resides outside every configured workdir and source repository in user-private storage; file security and rotation must be tested. No Cloudflare API/DNS mutations as a side effect of bootstrap/service install.
- A Tunnel Token only authenticates the Connector; remote MCP callers still need effective ServerFS API Key/OAuth (or a separately verified upstream authentication boundary), narrow Host/Origin controls, TLS termination at Cloudflare and explicit public-endpoint admission.
- Define explicit Tunnel provider selection while preserving existing OpenAI Tunnel CLI/config defaults and its single-managed-owner contract. Reuse current service state/observability models, do not create a generic command executor or unnecessary provider abstraction.
- Product and public docs must not imply production workdir/Agent or automatic credentials sharing. Query-string API keys can be logged and are not a preferred production authentication pattern.

## Phase 0 — Evidence-led feasibility and design gate (no implementation yet)

1. Inspect `src/serverfs_mcp/native_tunnel.py`, `darwin_service.py`, `darwin_lifecycle.py`, `native_config.py`, `tunnel_bootstrap.py`, `src/serverfs_mcp/main.py`, and the native service templates. **Determine whether the current macOS persistent chain actually exposes a private HTTP /mcp listener independently of the OpenAI tunnel-client.** Do not assume the Linux Compose HTTP origin applies on macOS.
2. Confirm official cloudflared Darwin arm64 binary packaging, integrity verification and compatible `tunnel run --token-file` behavior on the supported OS. Select the least invasive versioned binary-management contract.
3. Document the provider-selection surface and service ownership model, coexistence/migration policy with OpenAI Tunnel, native HTTP listener lifecycle, how Agent Bridge continues to work, and rollback semantics. If the existing managed runner cannot safely switch providers, revise the architecture and obtain explicit approval before implementation.
4. Produce concrete configuration examples, safe token storage path/permissions and an isolated local acceptance topology. Preflight must fail closed on missing token, missing HTTP auth, unexpected Host/Origin, duplicate managed process or user Login Items denial.

## Phased implementation (after Phase 0 approval)

- **A. Native bootstrap and configuration:** official verified binary, private token-file contract, provider selection and diagnostics; no unauthorized Cloudflare control-plane creation.
- **B. Persistent service integration:** use the existing `SMAppService` app-associated LaunchAgent and user-owned service install/start/stop/restart/uninstall state; preserve existing Bridge lifecycle/handover, avoid accidentally replacing production Tunnel, and support reversible provider migration.
- **C. Protocol/security regression:** loopback-only native origin, API Key/OAuth/Host/Origin enforcement, workdir confinement, hidden-path policy, no secret disclosure; deterministic unit/platform tests and rollback.
- **D. Real macOS acceptance:** isolated test workdir and separate named Tunnel/hostname, TLS 401/200, real ChatGPT plugin file read/search, negative mutation/hidden/traversal, >60-second SSE, session use after controlled Connector restart, login/logout persistence where supported; record actual platform/arch and evidence.
- **E. Release closure:** update all **seven** existing Skills and reference branches, plugin manifest/ZIP, README/README_EN, native macOS runbook, bilingual website and release notes; align project, Bridge/native metadata and locks; full CI and dispatch-first exact-SHA release workflow.

## Acceptance criteria

- Correct official binary + authenticated version verification; no root access and no unmanaged background service.
- `serverfs service status/start/stop/restart` reports and controls the chosen mode without unexpected OpenAI Tunnel takeover, Agent Bridge death, or duplicate ownership.
- Actual Cloudflare Connector connected, public HTTPS verified, independent MCP admission gates deny unauthorized clients.
- Direct ChatGPT tool discovery/read with isolated fixture; private workdir enforcement, 60+ seconds SSE, existing session reuse after Connector restart; no claim of in-flight SSE lossless replay.
- Demonstrated rollback to the previously working OpenAI Tunnel behavior without losing private configuration or user authority.
- All applicable macOS native, Linux nonregression, Agent/Skill/Plugin and site build gates green before v0.24.1 release.

## Out of scope / handoff

No Windows Cloudflare native lifecycle in this release; it is planned for **v0.24.2**. No Linux Tunnel redesign, general macOS platform expansion, Cloudflare WAF/DNS automation, new MCP tools, or deployment against the operator's existing production instance without explicit approval.
