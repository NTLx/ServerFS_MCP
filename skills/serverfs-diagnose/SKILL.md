---
name: serverfs-diagnose
description: Investigate an unknown ServerFS failure or check deployment health; identify the failing boundary before authorized repair.
---

# Diagnose ServerFS

Use a read-first evidence ladder. Do not repair while the failing boundary is still unknown.

## When to use

Use this skill when the user reports that ServerFS is unavailable, unhealthy, misconfigured, disconnected, returning errors, exposing the wrong capability surface, or behaving differently after a restart/upgrade, and the cause is not already established.

Also use it for a bounded health check or to prove the current deployment state before another operational change.

## When NOT to use

Do not use this as the main workflow when the requested change is already explicit:

- first installation: use `serverfs-install`;
- ordinary configuration mutation: use `serverfs-configure`;
- planned upgrade: use `serverfs-upgrade`;
- known Tunnel work: use `serverfs-tunnel`;
- known HTTP API Key/OAuth work: use `serverfs-auth`;
- known Agent Bridge/runtime/Jev work: use `serverfs-agent-bridge`;
- known Repo Ops policy changes: use `serverfs-configure`. Repo Ops diagnosis may still begin here when the failing layer is not yet known.

Do not use diagnosis as permission to change configuration, restart services, delete state, reinstall packages or rotate credentials unless the user requested remediation and the cause is established.

## Preflight

First locate the target ServerFS checkout or deployed installation and establish its version, active configuration and lifecycle owner. In imported/standalone mode, if no authoritative checkout/deployment source is accessible, stop at bounded guidance; do not mutate or infer target commands/settings.

Paths below are relative to the located **ServerFS checkout root**, never to this skill or the Plugin ZIP. Resolve repository paths only after locating that checkout. For an installed deployment without a checkout, use its version-matched deployment documentation, installed CLI help and active configuration; obtain matching sources before a step that needs missing repository material.

1. Identify the target host and platform: Linux, supported Windows native, or supported macOS native.
2. Identify the deployment family and how ServerFS is currently owned:
   - Linux Docker Compose;
   - Windows current-user native lifecycle;
   - macOS current-user ServiceManagement lifecycle;
   - bounded manual/direct diagnostic invocation.
3. Locate the authoritative configuration without printing secrets:
   - Linux: existing `.env` plus the exact Compose files used;
   - native: `serverfs.toml` plus the narrow `.env` used by the managed lifecycle.
4. Record the observed failure and timestamp. Separate facts from hypotheses.
5. Do not infer a remote/live state from a different machine or platform.

## Authoritative sources

Read the smallest relevant current sources before drawing conclusions:

- `site/src/content/docs/docs/getting-started.md`
- `site/src/content/docs/docs/windows-native.md`
- `site/src/content/docs/docs/macos-native.md`
- `site/src/content/docs/docs/tunnel-diagnostics.md`
- `site/src/content/docs/docs/host-observability.md`
- `site/src/content/docs/docs/repo-ops.md`
- `deployment/windows/README.md`
- `deployment/macos/README.md`
- `deployment/agent-bridge/README.md`
- the target installation's `serverfs --version`, `serverfs doctor`, service status and current config parser behavior.

Historical `dev_plan_v*.md` files are evidence of past releases, not current-state instructions.

## Procedure

1. **Prove the deployment identity.** Capture ServerFS version, platform, deployment owner and the exact configuration file/Compose shape in use. Do not expose credential values. Proceed when version, owner and active config are evidenced on the target host.
2. **Run the least invasive product health surface first.** Prefer `serverfs doctor` for native deployments and configuration-aware checks. On Linux, render the exact Compose shape before assuming runtime failure. Record the exit status and redacted error or successful result before selecting the next check.
3. **Check ownership/lifecycle.** Determine whether the intended current-user service/container is actually the owner. Look for duplicate/manual processes only through supported status/process evidence; do not kill by executable name. Proceed when the runtime owner is proven or report ownership as the blocker.
4. **Check the narrow failing boundary.** Examples:
   - filesystem/workdir: parser/doctor + target root accessibility;
   - generic HTTP: MCP container state, Host/Origin policy and exact published endpoint;
   - Tunnel: route to `serverfs-tunnel` and use its layered health evidence;
   - API Key/OAuth: route to `serverfs-auth` after confirming the configured mode;
   - Repo Ops: first distinguish public tool-schema exposure, per-workdir `repo_ops` policy, Host Bridge transport, named-action policy/integrity, and the fixed Git operation itself. Use `inspect_repo(actions/status)` when exposed; do not submit an Agent task as a diagnostic fallback. Route policy repair to `serverfs-configure` and Host Bridge lifecycle/transport repair to `serverfs-agent-bridge`.
   - Agent tools/runtime: route to `serverfs-agent-bridge` after confirming Agent policy and Bridge status.
   - For Agent problems, follow the **Correlated Agent Diagnostics (v0.22+ / feature-detect)** section in `skills/serverfs-agent-bridge/SKILL.md`: check the installed version or `serverfs diagnostics --help` (or equivalent capability listing) first. Only when advertised, use `serverfs diagnostics snapshot --format json` and `serverfs diagnostics task <task-id> --format json`. These are not v0.21 guarantees; fall back to the version-supported Agent status/runtime surfaces when absent.
5. **Bound logs and redact.** Read only the recent logs needed to explain the failure. Never dump `.env`, provider environment, keys, bearer tokens or unrestricted logs. Retain only redacted evidence tied to the observed symptom.
6. **Form one evidence-backed diagnosis.** If evidence supports multiple causes, state what would distinguish them before changing anything. Finish this step with a supported cause or a specific read-only check that distinguishes the remaining hypotheses.
7. **Remediate only when authorized.** Apply the smallest correction using the subsystem's dedicated skill, then repeat the original failing check. Report the repeated check’s observed result; unresolved or unexecuted checks remain explicit blockers.

## Platform branches

### Linux / Docker Compose

Start with the exact Compose files the deployment uses. Common read-only checks include:

```bash
docker compose config --quiet
docker compose ps
```

If the deployment uses the Host Bridge for Agent delegation and/or Repo Ops, include `-f compose.agent.yml`; for generic HTTP include `-f compose.http.yml`; for OAuth include `-f compose.oauth.yml`. Do not validate a different Compose shape from the one actually deployed, and do not infer Agent mode merely from the presence of the Host Bridge overlay.

Inspect bounded logs only for the affected service. A container being `Up` is not proof that Tunnel control-plane or MCP response delivery is healthy.

### Windows native

v0.23.2 supports validated native Windows 11 x64 Deterministic Repo Ops. If an explicitly enabled Windows Repo Ops workdir is missing the capability, diagnose native policy/Bridge synchronization, reparse-point policy, Git-for-Windows execution, and writer-lease/process-tree state rather than treating it as unsupported. Run the ServerFS executable from the stable current-user connector environment and use the real config:

```powershell
& $serverfs doctor --config serverfs.toml
& $serverfs service status
& $serverfs service status --format common-json
# Or: --format common-human
```

The opt-in v0.21 `common-json`/`common-human` formats provide a bounded, sanitized status projection. They omit sensitive identifiers and use `unknown`/`not-checked` when a fact is unavailable; they do not replace `doctor` or its configuration-aware checks. The default status output remains unchanged. The intended persistent owner is the current-user Task Scheduler lifecycle. Do not substitute SCM/LocalSystem, Administrator execution, a repository `.venv`, or the legacy manual launcher as a repair shortcut.

### macOS native

v0.23.1+ supports validated native Apple Silicon macOS Deterministic Repo Ops. If an explicitly enabled macOS Repo Ops workdir is missing the capability, diagnose native policy/Bridge synchronization and writer-lease/process-lifecycle state rather than treating it as unsupported. Use the supported native CLI surfaces:

```bash
serverfs doctor --config serverfs.toml
serverfs service status
serverfs service status --format common-json
# Or: serverfs service status --format common-human
```

The opt-in v0.21 `common-json`/`common-human` formats provide a bounded, sanitized status projection. They omit sensitive identifiers and use `unknown`/`not-checked` when a fact is unavailable; they do not replace `serverfs doctor` or its configuration-aware checks. The default status output remains unchanged.

If status reports that background-item approval is required, that is a user-controlled macOS state. Use `serverfs service open-settings` only when remediation is requested; do not bypass approval with raw launchd/root mechanisms.

## Safety / stop conditions

Stop and report evidence instead of continuing when:

- the platform/deployment family cannot be established;
- the current configuration source is unknown;
- a proposed repair requires root/Administrator or changes the established ownership model;
- the target contains unknown local/source modifications that would be overwritten;
- a secret would need to be printed or moved into a workdir to proceed;
- the failure belongs to an optional subsystem whose user intent is unclear;
- `doctor`, config parsing or Compose rendering fails after a proposed change;
- a process/service identity cannot be proven safely.

Never delete auth state, Agent state, credentials, locks, sockets or service registrations merely because they look stale. Use the subsystem's documented recovery path.

## Verification

Verification must repeat the evidence that originally failed and add the narrow product-level health proof appropriate to the platform.

Minimum examples:

- configuration parses/renders successfully;
- `serverfs doctor` exits successfully where applicable;
- intended managed service/container owns the runtime;
- the failing subsystem reports healthy/expected state;
- no unintended optional subsystem was enabled or disabled.

For Tunnel, Auth or Agent problems, use the dedicated skill's verification contract rather than treating a generic process-up state as sufficient. For Repo Ops, verify the public deterministic surface directly: effective workdir enablement plus `inspect_repo(actions/status)` and, when relevant, the exact named action or Git operation that failed. Never use `submit_agent_task` merely to prove or work around Repo Ops behavior.

## Success criteria

Diagnosis is complete when:

- the failing boundary is identified with observable evidence;
- facts and hypotheses are clearly separated;
- any remediation performed is minimal and authorized;
- the original failure is re-tested and resolved, or the remaining blocker is explicitly demonstrated;
- no credential, operator configuration or platform ownership boundary was weakened.

## Failure handling

If the cause remains unresolved, report:

1. exact checks executed and their outcomes;
2. the smallest remaining set of plausible causes;
3. the next read-only discriminator for each cause;
4. checks not executed and why.

Do not claim a fix from source inspection alone, and do not continue with random configuration changes after repeated equivalent failures.
