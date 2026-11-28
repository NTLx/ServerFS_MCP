---
title: Read-only Host Observability
description: Publish a narrow, read-only Linux host status snapshot without Agent Bridge or shell authority.
---

For low-authority host observability, publish generated status files instead of exposing the host:

```text
host collector -> approved status directory -> read-only ServerFS workdir -> MCP client
```

This is an optional operator-managed Linux recipe, not a built-in collector or universal component status API. The file-only Core needs no Agent Bridge, Docker socket or shell authority. For component lifecycle diagnosis, follow the actual Compose project and platform runbooks.

## Choose what to publish

Treat the status directory as a publication boundary. Publish only explicitly approved fields: uptime/load, memory totals, capacity of selected filesystems, and optionally specifically selected service/container/GPU summaries. Do not include raw environments, unrestricted logs, container inspection output, commands, credentials or private application payloads.

The minimal example below has no Docker access or elevated collection privileges. If adding other collectors, review the exact source, field allowlist, privileges and output first. Docker socket access is highly privileged; do not grant it merely to produce a summary.

## Prepare a dedicated directory

Choose an existing non-root collector account (the current login user in this example), the actual numeric container GID and a new dedicated path that does not overlap any other exposed root. The default container GID is 10001; confirm the selected deployment's settings and user namespace mapping before using this value.

```bash
# Only for a new, dedicated directory; do not reuse unrelated data.
sudo install -d -m 2750 -o "$USER" -g 10001 /srv/serverfs-status
stat -c '%a %u:%g %n' /srv/serverfs-status
```

The setgid directory inherits its group on newly created files; final snapshots are mode 0640. Verify the collector can write and the actual nonroot Core identity can traverse/read. Do not make the directory world-readable to fix an identity mismatch.

## Collect a complete snapshot

Install an operator-controlled executable script outside exposed workdirs, for example `/usr/local/bin/serverfs-status-collect`. Run as the collector user, not root. This example assumes GNU/Linux tools.

```bash
#!/usr/bin/env bash
set -euo pipefail

out=/srv/serverfs-status
umask 0027
tmp=$(mktemp "$out/.status.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT

{
  printf '# Host status\n\nGenerated UTC: '
  date -u '+%Y-%m-%dT%H:%M:%SZ'
  printf '\n## Uptime and load\n'
  uptime
  printf '\n## Memory (MiB)\n'
  free -m
  printf '\n## Root filesystem capacity\n'
  df -h --output=size,used,avail,pcent /
} >"$tmp"

chmod 0640 "$tmp"
mv -f -- "$tmp" "$out/status.md"
trap - EXIT
```

All required commands must succeed before publication. Same-directory rename publishes the complete file atomically; failure leaves the last good snapshot intact. Its timestamp tells readers whether that snapshot is stale. Do not infer current host health from a stale or missing file. Inspect collector failures separately.

## Refresh and verify locally

Run the installed script once as its actual collector account. Check the entire snapshot and permissions before exposing it. For unattended refresh, a system service (running as that non-root user) and timer are sufficient.

`/etc/systemd/system/serverfs-status.service`:

```ini
[Unit]
Description=Refresh approved ServerFS host status

[Service]
Type=oneshot
User=YOUR_COLLECTOR_USER
ExecStart=/usr/local/bin/serverfs-status-collect
```

`/etc/systemd/system/serverfs-status.timer`:

```ini
[Unit]
Description=Refresh ServerFS host status

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

Substitute the actual non-root account. After explicit authorization to install these new units, verify them and start only this timer:

```bash
sudo systemd-analyze verify /etc/systemd/system/serverfs-status.{service,timer}
sudo systemctl daemon-reload
sudo systemctl start serverfs-status.service
sudo systemctl enable --now serverfs-status.timer
systemctl list-timers serverfs-status.timer
systemctl status serverfs-status.service --no-pager
```

A user's cron entry can be used instead, not in addition. Timer status alone is not evidence of a fresh snapshot.

## Publish through ServerFS

Choose a free workdir slot; the example uses slot 02, leaving slot 01 available for other purposes. In the selected Core's private env file:

```dotenv
WORKDIR_02_ALIAS=host-status
WORKDIR_02_PATH=/srv/serverfs-status
WORKDIR_02_DESCRIPTION="Approved host status snapshots"
WORKDIR_02_READ_ONLY=true
WORKDIR_02_ALLOW_HIDDEN=false
WORKDIR_02_BINARY_TRANSFER_ENABLED=false
WORKDIR_02_AGENT_MODE=disabled
WORKDIR_02_AGENT_RUNTIMES=
WORKDIR_02_REPO_OPS_ENABLED=false
```

For a file-only Core, select the disabled Bridge overlay. In a signed Bridge deployment, this extra file-only slot must remain disjoint from the authorized Bridge root. Follow the [Linux deployment runbook](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/deployment/linux/README.md) for Core-scoped deployment/configuration; adding a bind mount requires recreating the selected Core, not restarting unrelated Connectors or Bridge owners.

Verify the exact exposed directory, including unexpected entries and symlinks:

```bash
find /srv/serverfs-status -mindepth 1 -maxdepth 1 -printf '%y %M %u:%g %p\n'
cat /srv/serverfs-status/status.md
```

Expect only approved published files (apart from short-lived hidden collector temporaries); do not expose the script or other state. Through authenticated MCP, check `list_workdirs`, read `status.md`, verify a harmless write is denied and check the UTC timestamp after refresh. These checks are separate from collector/timer readiness.

For retirement, stop and disable only this timer (or remove its cron entry), remove the publication slot through a scoped Core configuration change, and verify it is no longer accessible. Retain or delete the dedicated snapshot directory only as authorized. Never add Docker socket, `/proc`, `/sys`, `/var/run`, credential directories or broad logs as workdirs. If active engineering is needed, use separately authorized [Agent Bridge](../agent-bridge/) or [Repo Ops](../repo-ops/) within their actual capabilities rather than widening the snapshot boundary.
