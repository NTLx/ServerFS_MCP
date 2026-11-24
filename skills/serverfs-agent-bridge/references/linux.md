# Linux host Bridge

Use `deployment/linux/README.md` and primary Skill gates. Core and Bridge IDs are distinct nondefault IDs even for dedicated owner. Owner is a host user systemd instance, not a container. Initial grants support 1–32 Cores; no live append/rotation via initial provisioning.

Provision private owner JSON (protocol_mode v2, workdir lease_key equal to alias), grants and bindings via `linux_v0250_provision`. Runtime/ACP/proxy/Jev owner settings live in private JSON, not Core env; legacy singleton renderer is a different path. Provider proxy endpoints must be credentialless. Use `linux_v0250_install` stage/select for the exact owner release; verify stopped state before selection. Start only the selected user unit.

Core uses `deployment/linux/compose.bridge-v2.yml` instead of bridge-disabled. v0.25.0 binds one Agent/Repo Ops workdir in slot 01; the v0.26.0 candidate allows 1–16 explicitly granted Agent and/or Repo Ops workdirs in slots 01–16. Use schema-version-matched bindings; disjoint file-only roots remain allowed. Keep global Agent defaults disabled; match owner grant and Core slot policy. Mount private socket, lock and per-Core binding directories read-only as documented.

Run exact full Compose JSON through `linux_v0250_preflight` privately; never print/tee the credential-bearing model. Static preflight is not live peer UID/GID, socket or other-project isolation proof. Verify actual mounts, IPC, runtime/Repo Ops and denials independently. File-only uses disabled overlay. Shared Core cannot stop/update owner; signed v2 rejects continuation/idempotency. Preserve leases and provider state.
