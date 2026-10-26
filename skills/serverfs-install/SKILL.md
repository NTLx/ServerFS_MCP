---
name: serverfs-install
description: Install ServerFS on a new host; select the supported deployment and verify the base product before requested optional capabilities.
---

# Install ServerFS

Install the smallest supported ServerFS deployment for the target platform, verify the base product first, then add optional subsystems only when the user explicitly asks for them.

## When to use

Use this skill when ServerFS is not yet installed/configured on the target host, or when the user explicitly wants a clean first deployment on a new host.

## When NOT to use

Do not use this workflow to overwrite or rebuild an existing installation. If ServerFS, `.env`, `serverfs.toml`, managed service state or a deployment checkout already exists, determine whether the task is an upgrade (`serverfs-upgrade`), configuration change (`serverfs-configure`) or diagnosis (`serverfs-diagnose`).

Use dedicated skills for optional subsystems after the base install is healthy:

- `serverfs-tunnel`;
- `serverfs-auth`;
- `serverfs-configure` for per-workdir Repo Ops policy;
- `serverfs-agent-bridge` for the Linux Host Bridge transport/lifecycle used by Agent delegation and/or Repo Ops.

## Preflight

First locate the target ServerFS checkout or deployed installation and establish its version, active configuration and lifecycle owner. In imported/standalone mode, if no authoritative checkout/deployment source is accessible, stop at bounded guidance; do not mutate or infer target commands/settings.

Paths below are relative to the located **ServerFS checkout root**, never to this skill or the Plugin ZIP. Resolve repository paths only after locating that checkout. For an installed deployment without a checkout, use its version-matched deployment documentation, installed CLI help and active configuration; obtain matching sources before a step that needs missing repository material.

1. Identify the operating system, architecture and filesystem/storage boundary.
2. Read the current supported platform shape from the target ServerFS release.
3. Confirm the current user can satisfy external prerequisites without changing ServerFS into a privileged deployment.
4. Check for pre-existing ServerFS configuration/state before creating anything.
5. Determine the minimum requested ingress:
   - Linux generic Streamable HTTP is the normal base deployment;
   - Windows/macOS native use stdio plus optional Tunnel for ChatGPT connectivity.
6. Keep Tunnel, Auth, binary/file ingress, Repo Ops and Agent delegation disabled unless they are explicitly part of the requested installation. Repo Ops and Agent delegation are independent opt-ins even though Linux uses the same Host Bridge transport for both.

If an existing operator config is found, stop the new-install path. Never treat it as disposable merely because it differs from the example.

## Authoritative sources

- `site/src/content/docs/docs/getting-started.md`
- `site/src/content/docs/docs/windows-native.md`
- `site/src/content/docs/docs/macos-native.md`
- `site/src/content/docs/docs/repo-ops.md` when the requested Linux installation includes Repo Ops;
- `deployment/windows/README.md`
- `deployment/macos/README.md`
- `.env.example`
- `serverfs.toml.example`
- the release assets and current `serverfs` CLI for the target release.

Do not use an old development plan as the install guide when current deployment documentation exists.

## Procedure

1. **Select the supported platform family.** Do not invent a native Linux shape or Dockerize the supported native Windows/macOS shape to avoid following platform contracts. Proceed only after OS, architecture and storage match the target release support evidence.
2. **Acquire the intended release/source.** Prefer the user's requested release; otherwise use the current stable release documented by the checkout/release source. Do not silently track `edge`/unreleased code for a production install. Confirm acquired artifact version/integrity against the intended release metadata.
3. **Create initial configuration only when none exists.** Examples may seed a new config, but immediately reduce it to the operator's actual workdirs/settings and keep secrets untracked/private. Confirm the new config is private and parses before starting services.
4. **Configure one minimal workdir.** Read-only is the default; enable writes only when the user's use case requires them. Check the configured root exists and its effective read-only/write policy matches the request.
5. **Start/validate the base product before optional features.** This separates filesystem/configuration failures from Tunnel/Auth/Repo Ops/Agent failures. Proceed only after base parser/doctor and transport checks pass.
6. **Use the platform's supported current-user lifecycle.** Do not replace it with root/system services. Confirm status identifies the intended current-user owner.
7. **Add optional capabilities one at a time when requested**, using the dedicated skill and re-verifying after each capability. For Linux Repo Ops, configure the per-workdir policy through `serverfs-configure` and the required Host Bridge transport/lifecycle through `serverfs-agent-bridge`; do not enable an Agent runtime merely because Repo Ops needs the Bridge transport. Complete each requested subsystem’s verification before adding another.

## Platform branches

### Linux / Docker Compose

The normal base is the generic Streamable HTTP deployment described by the current Getting Started guide:

```bash
git clone https://github.com/NTLx/ServerFS_MCP.git
cd ServerFS_MCP
cp .env.example .env
chmod 600 .env
```

Only perform the copy when `.env` does not already exist. Configure at least one workdir. Before starting the default loopback endpoint, set this explicit Host allowlist in `.env`:

```text
SERVERFS_HTTP_ALLOWED_HOSTS=serverfs-mcp:8000,127.0.0.1:8000,localhost:8000
```

An empty allowlist permits only the internal authority. Preserve `serverfs-mcp:8000` for the Tunnel/internal path and add the actual loopback Host authorities for local clients. With customized ports, use the published port for loopback and the container HTTP port for `serverfs-mcp`. For external clients or a reverse proxy, add each exact Host authority received by ServerFS to the allowlist rather than disabling Host validation. Then use the generic HTTP shape:

```bash
docker compose pull
docker compose -f compose.yml -f compose.http.yml up -d serverfs-mcp
docker compose -f compose.yml -f compose.http.yml ps
```

Before public/non-loopback exposure, route to `serverfs-auth` if ServerFS-owned authentication is requested and ensure the separate TLS/Host/Origin boundary from current documentation is satisfied.

Do not add `compose.agent.yml`, `compose.oauth.yml`, file-ingress profile or the historical base Tunnel shape unless requested. On v0.23 Linux, `compose.agent.yml` is the Host Bridge transport overlay for Agent delegation and/or Repo Ops; adding it does not itself authorize either capability.

### Windows native

Supported installation uses the release wheels in **two isolated per-user Python environments**: product/native kernel in the connector environment and Agent Bridge in its separate environment when Agent support is needed. Do not install the product and Bridge into one dependency graph. The v0.23 Deterministic Repo Ops implementation is Linux-only; a Windows install must not invent or enable native Repo Ops policy.

For the base filesystem/native product, follow the current Windows guide and install product + native wheel into the stable current-user connector environment. Create `serverfs.toml` from the example only if no config exists, then run:

```powershell
& $serverfs doctor --config serverfs.toml
```

After doctor passes, the supported persistent lifecycle is:

```powershell
& $serverfs service install --config serverfs.toml --env-file .env
& $serverfs service start
& $serverfs service status
```

A fresh install being disabled until explicit `service start` is intentional. Do not replace the current-user Task Scheduler lifecycle with SCM/LocalSystem or Administrator-owned services.

### macOS native

Only proceed within the current documented macOS support boundary. Use the normal Python environments and current native deployment guide. The v0.23 Deterministic Repo Ops implementation is Linux-only; a macOS install must not invent or enable native Repo Ops policy. Create `serverfs.toml` only when no existing configuration exists, then run:

```bash
serverfs doctor --config serverfs.toml
serverfs service install --config serverfs.toml --env-file .env
serverfs service status
```

Use `serverfs service start` when needed by the current lifecycle state. If macOS requires Login Items approval, surface that user-controlled requirement; do not bypass it with root LaunchDaemons or raw replacement plists.

## Safety / stop conditions

Stop when:

- a pre-existing installation/configuration is discovered and ownership/history is unclear;
- the host is outside the currently supported platform/storage boundary;
- installation would require changing ServerFS to root/Administrator ownership;
- a requested workdir or key path would place secrets inside an exposed workdir;
- the base config/doctor/Compose gate fails;
- optional features are being implicitly added without user intent;
- release assets/versions cannot be verified from the intended release source.

Do not delete an existing environment/state to manufacture a "clean install" unless the user explicitly requested destructive replacement and the consequences are understood.

## Verification

Minimum base verification:

- installed `serverfs --version` corresponds to the intended release;
- configuration parses/renders;
- target workdir is visible through the supported ServerFS surface with expected read-only/write policy;
- intended current-user service/container is running/healthy;
- no unrequested Tunnel/Auth/Repo Ops/Agent capability is active.

Windows/macOS: `serverfs doctor --config serverfs.toml` must pass before claiming the native installation healthy.

Linux: verify the exact Compose shape used and the MCP service state; if the user requested remote HTTP access, test the configured endpoint through the intended ingress boundary rather than only checking the container process.

## Success criteria

A first installation is complete when the smallest requested ServerFS deployment is healthy, current-user/non-elevated, configured only for the intended workdirs/capabilities, and proven through supported status/doctor/transport checks.

## Failure handling

Do not respond to an install failure by adding privilege or optional overlays. Route unclear failures to `serverfs-diagnose`; route a confirmed subsystem failure to its dedicated skill.

Report which installation stage failed, the exact non-secret validation evidence, and what was left unchanged. Never claim a platform is installed successfully solely because files were copied or a service definition was created.
