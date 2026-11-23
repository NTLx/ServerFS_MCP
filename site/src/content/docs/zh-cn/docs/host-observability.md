---
title: 只读宿主机可观测性
description: 不授予 Agent Bridge 或 Shell 权限，通过只读 workdir 发布范围有限的 Linux 宿主机状态。
---

低权限宿主机可观测性应发布生成后的状态文件，而不是直接暴露宿主机：

```text
host collector -> approved status directory -> read-only ServerFS workdir -> MCP client
```

这是运维者按需管理的外部 Linux 示例，不是内置采集器或通用组件状态 API。纯文件 Core 不需要 Agent Bridge、Docker Socket 或 Shell 权限。组件生命周期诊断应遵循实际 Compose project 与平台指南。

## 确定发布范围

将状态目录视为发布边界，只发布明确获准的字段：uptime/load、内存总量、指定文件系统容量，以及可选的指定服务、容器或 GPU 摘要。不要发布原始环境变量、无限制日志、容器 inspect 输出、命令、凭据或私有业务数据。

以下最小示例无需 Docker 访问或高权限采集。增加其他采集器前，先审核数据源、字段白名单、所需权限及输出。Docker Socket 权限很高，不要仅为生成摘要而授予。

## 创建专用目录

选择实际非 root 采集用户（示例为当前登录用户）、容器真实数字 GID 与全新专用路径，避免与其他暴露根目录重叠。默认容器 GID 为 10001；使用前确认所选部署配置和 user namespace 映射。

```bash
# 仅用于全新专用目录，不复用无关数据。
sudo install -d -m 2750 -o "$USER" -g 10001 /srv/serverfs-status
stat -c '%a %u:%g %n' /srv/serverfs-status
```

setgid 目录使新文件继承目录组；最终快照权限为 0640。确认采集用户可写、实际非 root Core 身份可遍历和读取。不要通过开放所有用户读取权限解决身份不匹配。

## 采集完整快照

在对外 workdir 之外安装由运维者控制的可执行脚本，例如 `/usr/local/bin/serverfs-status-collect`。以采集用户而非 root 执行。此例假定 GNU/Linux 工具环境。

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

所有必要命令成功后才发布。同目录 rename 原子发布完整文件；失败时保留上一次成功快照。时间戳可用于判断是否过期。不要把过期或缺失的文件视为当前健康证据，采集失败应单独诊断。

## 定期刷新与本地验证

先以实际采集用户执行已安装脚本，检查完整快照与权限，再对外发布。需要无人值守刷新时，使用以非 root 用户运行的 systemd service 和 timer 即可。

`/etc/systemd/system/serverfs-status.service`：

```ini
[Unit]
Description=Refresh approved ServerFS host status

[Service]
Type=oneshot
User=YOUR_COLLECTOR_USER
ExecStart=/usr/local/bin/serverfs-status-collect
```

`/etc/systemd/system/serverfs-status.timer`：

```ini
[Unit]
Description=Refresh ServerFS host status

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

替换为实际非 root 用户。获得安装这两个新 unit 的授权后，先验证，再仅启动所选 timer：

```bash
sudo systemd-analyze verify /etc/systemd/system/serverfs-status.{service,timer}
sudo systemctl daemon-reload
sudo systemctl start serverfs-status.service
sudo systemctl enable --now serverfs-status.timer
systemctl list-timers serverfs-status.timer
systemctl status serverfs-status.service --no-pager
```

也可改用用户 cron，但不要同时使用。Timer 状态本身不能证明快照新鲜。

## 通过 ServerFS 发布

选择空闲 workdir slot；此例使用 02，为其他用途保留 01。在所选 Core 的私有 env 文件中：

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

纯文件 Core 选择禁用 Bridge overlay；若已使用签名 Bridge，此额外纯文件 slot 不得与授权 Bridge 根目录重叠。按 [Linux 部署指南](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/linux/README.md)执行 Core 范围内的部署与配置变更；新增 bind mount 需重建所选 Core，不重启无关 Connector 或 Bridge owner。

检查实际暴露目录，包括意外条目和符号链接：

```bash
find /srv/serverfs-status -mindepth 1 -maxdepth 1 -printf '%y %M %u:%g %p\n'
cat /srv/serverfs-status/status.md
```

预期只有获准发布的文件（采集时短暂存在的隐藏临时文件除外），不暴露脚本或其他状态。经认证的 MCP 调用 `list_workdirs`、读取 `status.md`、验证无害写入被拒绝，并在刷新后检查 UTC 时间戳。这些验收与采集器、timer 就绪状态分开。

退役时，仅停止并禁用此 timer（或移除对应 cron），通过限定范围的 Core 配置变更移除发布 slot，再验证其已不可访问。只有获得授权才保留或删除专用快照目录。不要直接将 Docker Socket、`/proc`、`/sys`、`/var/run`、凭据目录或大范围日志加入 workdir。需要主动工程操作时，使用独立授权的 [Agent Bridge](../agent-bridge/) 或 [Repo Ops](../repo-ops/) 的实际能力，不扩大快照发布边界。
