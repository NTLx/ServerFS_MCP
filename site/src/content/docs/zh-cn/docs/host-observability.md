---
title: 只读宿主机可观测性
description: 不授予 Agent Bridge 或 shell 权限，通过只读 ServerFS workdir 暴露经过脱敏的 Linux 宿主机状态。
---

一种低权限、很适合 ServerFS 的模式，是发布**生成后的状态文件**，而不是直接暴露宿主机：

```text
host collectors -> sanitized status directory -> read-only ServerFS workdir -> MCP client
```

这样可以向 AI 客户端提供可检查的运维上下文，同时不增加 shell、Docker socket 或 Agent Bridge 权限。

这仍是互补的外部 Linux 模式，不会授予 Docker 或 shell 权限。v0.21 的 opt-in 通用 `service status` 投影仅适用于原生 macOS/Windows，不会取代此 Linux 模式；Linux Compose 不提供虚构的原生 service status 投影。

## 安全边界

把状态目录视为一个发布边界，只采集明确允许公开的字段。

适合的内容包括：

- uptime 与 load；
- 内存总量与使用量；
- 指定挂载点的文件系统容量；
- 指定容器的名称、状态与 health 摘要；
- 指定服务状态；
- 可选的 GPU 利用率与显存摘要。

不要发布完整环境变量、无限制 journal/应用日志、`docker inspect` 输出、secret 文件、命令历史、访问令牌或私有业务数据。应优先采用字段 allowlist，而不是先收集任意输出再尝试事后脱敏。

## 目录与权限

以下示例假定 ServerFS 容器使用默认身份 `10001:10001`，并使用独立宿主机目录：

```bash
sudo install -d -m 0750 -o "$USER" -g 10001 /srv/serverfs-status
```

Collector 生成的最终文件建议使用 `0640`：组可读、其他用户不可读。如果部署覆盖了 `SERVERFS_UID`/`SERVERFS_GID`，应改用实际值。

将该目录配置为显式只读 workdir：

```env
WORKDIR_02_ALIAS=host-status
WORKDIR_02_PATH=/srv/serverfs-status
WORKDIR_02_DESCRIPTION="Sanitized host status snapshots"
WORKDIR_02_READ_ONLY=true
```

## 最小 Collector

可在宿主机创建 `/usr/local/bin/serverfs-status-collect`：

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

临时文件加最终 `mv` 可以避免读者看到写到一半的快照。Collector 脚本本身应放在所有暴露 workdir 之外。

## 使用 systemd 定期刷新

可以使用简单的 systemd service + timer。示例 `/etc/systemd/system/serverfs-status.service`：

```ini
[Unit]
Description=Refresh sanitized ServerFS host status

[Service]
Type=oneshot
User=YOUR_COLLECTOR_USER
ExecStart=/usr/local/bin/serverfs-status-collect
```

以及 `/etc/systemd/system/serverfs-status.timer`：

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

把 `YOUR_COLLECTOR_USER` 替换为预期的非 root 用户后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now serverfs-status.timer
systemctl list-timers serverfs-status.timer
```

简单主机也可以使用 cron：

```cron
*/5 * * * * /usr/local/bin/serverfs-status-collect
```

## 验证发布边界

在添加 workdir 前，先人工检查究竟会发布什么：

```bash
find /srv/serverfs-status -maxdepth 1 -type f -printf '%M %u:%g %p\n'
sed -n '1,200p' /srv/serverfs-status/status.md
```

随后通过 ServerFS 验证该 workdir 确实只读。不要为了获得可观测性而把 Docker socket、`/proc`、`/sys`、`/var/run`、服务 credential 目录或大范围日志目录直接加入 workdir。

该方案刻意把“采集权限”留在宿主机，把“发布权限”限制在一个狭窄的只读目录。如果需要主动修改宿主机或委托工程任务，应使用独立受控的 [Agent Bridge](../agent-bridge/)，而不是扩大这个 workdir 的权限。
