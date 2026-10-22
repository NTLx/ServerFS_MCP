---
title: Read-only Host Observability
description: Expose sanitized Linux host status through a read-only ServerFS workdir without Agent Bridge or shell authority.
---

A useful low-authority ServerFS pattern is to publish **generated status files**, not the host itself:

```text
host collectors -> sanitized status directory -> read-only ServerFS workdir -> MCP client
```

This gives an AI client inspectable operational context without adding shell access, Docker socket access or Agent Bridge authority.

This remains a complementary external Linux pattern: it grants no Docker or shell authority. v0.21's opt-in common `service status` projection is limited to native macOS/Windows and does not replace this Linux pattern; Linux Compose has no fabricated native service status projection.

## Security boundary

Treat the status directory as a publication boundary. Collect only an allowlisted set of facts that you are willing to expose.

Good candidates include:

- uptime and load;
- memory totals/usage;
- filesystem capacity for selected mounts;
- selected container name/state/health summaries;
- selected service state;
- optional GPU utilization and memory summaries.

Do **not** publish raw environment dumps, unrestricted journal/application logs, `docker inspect` output, secret files, command histories, access tokens or private application payloads. Prefer allowlisting fields over trying to redact arbitrary output after collection.

## Directory and permissions

Assume the default ServerFS container identity `10001:10001` and a dedicated host directory:

```bash
sudo install -d -m 0750 -o "$USER" -g 10001 /srv/serverfs-status
```

Collectors should create final files with group-readable, non-world-readable permissions such as `0640`. If your deployment overrides `SERVERFS_UID`/`SERVERFS_GID`, use those values instead.

Configure the directory as an explicitly read-only workdir:

```env
WORKDIR_02_ALIAS=host-status
WORKDIR_02_PATH=/srv/serverfs-status
WORKDIR_02_DESCRIPTION="Sanitized host status snapshots"
WORKDIR_02_READ_ONLY=true
```

## Minimal collector

Create a host-side script such as `/usr/local/bin/serverfs-status-collect`:

```bash
#!/usr/bin/env bash
set -euo pipefail

out=/srv/serverfs-status
umask 0027

tmp=$(mktemp "$out/.status.XXXXXX")
trap 'rm -f "$tmp"' EXIT

{
  printf '# Host status\n\n'
  printf '## Uptime\n'
  uptime
  printf '\n## Memory\n'
  free -h
  printf '\n## Filesystems\n'
  df -h / /srv 2>/dev/null || true
  printf '\n## Containers\n'
  docker ps --format 'table {{.Names}}\t{{.Status}}' 2>/dev/null || true
  if command -v nvidia-smi >/dev/null 2>&1; then
    printf '\n## GPU\n'
    nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total \
      --format=csv,noheader,nounits 2>/dev/null || true
  fi
} >"$tmp"

chmod 0640 "$tmp"
mv -f "$tmp" "$out/status.md"
trap - EXIT
```

The temporary file and final `mv` keep readers from observing a half-written snapshot. Keep the script itself outside every exposed workdir.

## Refresh periodically with systemd

A simple system service can run the collector on a timer. Example `/etc/systemd/system/serverfs-status.service`:

```ini
[Unit]
Description=Refresh sanitized ServerFS host status

[Service]
Type=oneshot
User=YOUR_COLLECTOR_USER
ExecStart=/usr/local/bin/serverfs-status-collect
```

And `/etc/systemd/system/serverfs-status.timer`:

```ini
[Unit]
Description=Refresh sanitized ServerFS host status periodically

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
```

After replacing `YOUR_COLLECTOR_USER` with the intended non-root account:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now serverfs-status.timer
systemctl list-timers serverfs-status.timer
```

A cron entry is also sufficient for simple hosts:

```cron
*/5 * * * * /usr/local/bin/serverfs-status-collect
```

## Verify the publication boundary

Before adding the workdir, inspect exactly what will be exposed:

```bash
find /srv/serverfs-status -maxdepth 1 -type f -printf '%M %u:%g %p\n'
sed -n '1,200p' /srv/serverfs-status/status.md
```

Then verify through ServerFS that the workdir is read-only. Do not add the Docker socket, `/proc`, `/sys`, `/var/run`, service credential directories or broad log trees as workdirs just to obtain observability.

This recipe deliberately keeps collection authority on the host side and publication authority in a narrow read-only directory. If you need active host changes or delegated engineering work, use the separately gated [Agent Bridge](./agent-bridge/) rather than widening this workdir.
