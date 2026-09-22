# Phase E — user-scoped Agent Bridge deployment

Phase E wires the frozen Phase A–D contracts into a real Linux deployment.

The deployment has one non-negotiable rule:

> **Everything supplied by this project runs in the current user's permission
> scope. No sudo/root, system users/groups, system-level systemd units, /etc,
> /opt or /var/lib installation is required.**

Docker itself is an external prerequisite. Whether the current user is allowed
to use Docker is outside ServerFS.

## Deployment shape

```text
ChatGPT
  -> OpenAI Secure MCP Tunnel
  -> serverfs-mcp container
       /run/serverfs-agent-bridge  <- user-owned host runtime dir, bind RO
       /run/serverfs-agent-locks   <- user-owned host runtime dir, bind RO
  -> host Unix socket
  -> systemctl --user serverfs-agent-bridge.service
  -> native Codex / Claude Code environment of the SAME login user
```

The base `compose.yml` remains Agent-unaware. Agent deployment is opt-in via
`compose.agent.yml`.

## User-owned paths

The default deployment uses only:

```text
~/.config/serverfs-agent-bridge/
  config.json
  provider.env

~/.config/systemd/user/
  serverfs-agent-bridge.service

~/.local/share/serverfs-agent-bridge/
  releases/
  current -> releases/<version>
  previous -> releases/<previous-version>

~/.local/state/serverfs-agent-bridge/
  state.sqlite3 (+ WAL/SHM as needed)

~/.local/share/serverfs-agent-bridge/runtime/
  socket/
    bridge.sock
  locks/
    01.lock .. 16.lock
```

No project script creates a system user, system group or system-owned
configuration directory.

## Identity contract

Agent-enabled deployment requests that `serverfs-mcp` run as the current
host user:

```text
SERVERFS_UID=$(id -u)
SERVERFS_GID=$(id -g)

SERVERFS_AGENT_PEER_UID=<uid measured by step 3>
SERVERFS_AGENT_PEER_GID=<gid measured by step 3>
```

The Bridge itself runs as that same user through `systemctl --user`. The
measured peer values are accepted only when they equal the current user's
`id -u` / `id -g`.

The real host-kernel `SO_PEERCRED` identity is always measured. If rootless
Docker, userns-remap or another mapping causes the actual socket peer UID/GID
to differ from the login user, the default Phase E deployment **fails closed**.
Do not fix that mismatch by running ServerFS as root, creating a privileged
group or changing system ownership.

The enforcement point is the Bridge, not these checks: `render_config.py` and
`verify_host.py` merely compare configured values against the current user, so
neither can tell a genuinely measured peer from one assumed to be `id -u`. The
actual gate is `_check_peer` in
`agent_bridge/src/serverfs_agent_bridge/protocol.py`, which reads the
kernel-reported peer of every connection and rejects a mismatch with
`PEER_NOT_AUTHORIZED` — falling back to the same rejection when `SO_PEERCRED`
is unavailable. The step 3 probe is an early warning that a userns mismatch
exists, not the authorization itself: filling `SERVERFS_AGENT_PEER_UID/GID`
from `id -u` discards that warning without changing what the Bridge accepts.

Runtime assets use the user's existing primary group:

```text
socket dir   0750
bridge.sock  0660
lock dir     0750
lock files   0640
```

The two runtime directories are mounted read-only into the MCP container.
The container opens existing lock files read-only and uses `flock`; it never
creates or changes host lock files.

## 1. Prepare the single ServerFS `.env`

Phase E deliberately uses the same repository-root `.env` as the base ServerFS
deployment. There is no second Agent env file and no overlay precedence model.
`.env.example` documents the complete configuration surface; copy it only when
bootstrapping a new deployment, never over an existing `.env` with real tunnel
credentials.

For an existing deployment, add/review the Phase E keys in `.env` directly. Set:

```env
SERVERFS_UID=<id -u>
SERVERFS_GID=<id -g>
SERVERFS_AGENT_BRIDGE_ENABLED=false
SERVERFS_AGENT_BRIDGE_TIMEOUT_SECONDS=30
SERVERFS_AGENT_PEER_UID=
SERVERFS_AGENT_PEER_GID=
SERVERFS_AGENT_BRIDGE_HOST_SOCKET_DIR=/home/<user>/.local/share/serverfs-agent-bridge/runtime/socket
SERVERFS_AGENT_LOCK_HOST_DIR=/home/<user>/.local/share/serverfs-agent-bridge/runtime/locks
SERVERFS_AGENT_BRIDGE_STATE_DIR=/home/<user>/.local/state/serverfs-agent-bridge
```

Use the real absolute home path; `.env` values are not shell-expanded. Leave
`SERVERFS_AGENT_PEER_UID/GID` empty until step 3 measures the real container
peer identity. Keep `SERVERFS_AGENT_BRIDGE_ENABLED=false` until that measurement
and the provider/workdir review are complete. The base `compose.yml` does not pass
Agent settings into the container, so these values remain inert without
`compose.agent.yml`.

Provider secrets and shell-only environment are intentionally **not** stored in
`.env`; they remain in the user-owned `provider.env` described in step 4.

## 2. Enable Agent policy for selected workdirs

Agent policy lives in the same `.env` as each workdir's alias/path/read-only state.

Example:

```env
WORKDIR_01_ALIAS=ServerFS
WORKDIR_01_PATH=/srv/ServerFS_MCP
WORKDIR_01_READ_ONLY=false
WORKDIR_01_AGENT_MODE=workspace-write
WORKDIR_01_AGENT_RUNTIMES=codex,claude
```

Native Codex/Claude currently require `workspace-write`, therefore the same
workdir must have `READ_ONLY=false`.

Leave Agent mode disabled for workdirs that should not delegate Agents.

## 3. Measure the real container peer identity

Terminal A:

```bash
python3 deployment/agent-bridge/measure_peercred.py
```

It prints a temporary user-owned probe socket path and one-time token.

Terminal B uses the real ServerFS image/user configuration:

```bash
PROBE_TOKEN='<printed token>'

docker compose \
  --env-file .env \
  -f compose.yml \
  run --rm --no-deps \
  -e PROBE_TOKEN="$PROBE_TOKEN" \
  -v "$HOME/.local/share/serverfs-agent-bridge/peer-probe:/peer:ro" \
  serverfs-mcp \
  python -c 'import os,socket; s=socket.socket(socket.AF_UNIX); s.connect("/peer/peer.sock"); s.sendall((os.environ["PROBE_TOKEN"]+"\n").encode()); s.close()'
```

The probe must report:

```text
uid=<id -u>
gid=<id -g>
user_scope_compatible=true
```

Copy the measured `uid`/`gid` into `SERVERFS_AGENT_PEER_UID/GID`. They must
match `id -u` / `id -g` for this user-scoped deployment.

If the probe reports `false`, stop. The project's user-scoped deployment does
not ask for a privileged ownership workaround.

## 4. Provider binaries and environment

For providers enabled by workdir policy, `.env` must contain their absolute
executable paths:

```bash
command -v codex
command -v claude
```

Example:

```env
SERVERFS_CODEX_BIN=/home/me/.local/bin/codex
SERVERFS_CLAUDE_BIN=/home/me/.local/bin/claude
```

Provider authentication/settings remain the same user's native files.

The user service deliberately does not source `.bashrc` or `.zshrc`.
If the direct CLI depends on environment variables, put only the required
values in:

```text
~/.config/serverfs-agent-bridge/provider.env
```

Mode is `0600`.

Do not copy the entire interactive shell environment.

## 5. Install/update the Bridge as the current user

After step 3, copy the **measured** peer UID/GID into `.env`, set
`SERVERFS_AGENT_BRIDGE_ENABLED=true`, and review the selected workdir/provider
settings. Then run:

```bash
deployment/agent-bridge/install.sh
```

The installer:

- refuses to run as root;
- installs a locked Agent Bridge release below `~/.local/share`;
- keeps the previous release for rollback;
- renders `~/.config/serverfs-agent-bridge/config.json` atomically as 0600;
- installs `~/.config/systemd/user/serverfs-agent-bridge.service`;
- uses `systemctl --user`;
- starts/restarts only the current user's Bridge;
- restores the prior app/config/unit/service state if activation of an update fails;
- labels releases copied from a modified `agent_bridge/` tree with a `-dirty` suffix;
- never edits provider settings/authentication;
- never writes to a system directory.

`--no-start` is a staging mode. It installs/renders/switches the release and reloads the
user unit, but it does not stop, start or restart the Bridge service. If the service was
already active, that existing process keeps running until you explicitly restart it; if
it was inactive, it remains inactive.

`install.sh` and every `systemctl --user` command below need the systemd user
bus. A login shell already has it; a non-login shell (cron, CI, an agent
session that PAM did not set up) usually does not, and must export it first:

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
```

In this deployment, `Failed to connect to bus: No medium found` from
`systemctl --user` commonly means the user manager exists but this process lacks the
runtime-directory context needed to reach it. Confirm the user manager separately if
exporting `XDG_RUNTIME_DIR` does not resolve the error.
`install.sh` checks the bus before its first filesystem write, so it exits
without leaving a half-installed tree rather than writing a unit file it
cannot enable.

Inspect:

```bash
systemctl --user status serverfs-agent-bridge.service
journalctl --user -u serverfs-agent-bridge.service -n 100 --no-pager
```

The project does **not** run `loginctl enable-linger`. Whether a user service
continues after logout is a host policy decision outside ServerFS.

For unattended recovery after logout/reboot, Phase E release validation must
record:

```bash
loginctl show-user "$(id -un)" -p Linger --value
```

and require `yes`. If host policy does not permit linger, Agent delegation is
supported only while that user's systemd manager is active; this must be
documented operationally rather than worked around with a root/system Bridge.

## 6. Verify the host-side user deployment

```bash
python3 deployment/agent-bridge/verify_host.py
```

It verifies:

- all deployment assets are owned by the current user;
- allowed peer UID/GID equal the current user;
- user runtime/state paths match the contract;
- socket and all 16 lock files have expected modes;
- installed Bridge entrypoint exists;
- enabled provider binaries exist and are executable;
- a real `runtime.list` RPC succeeds over the Unix socket.

Because the user unit is `Type=simple`, systemd can report the service started just
before the Bridge binds `bridge.sock`. Verification waits briefly for that startup-only
race; structural, ownership, mode and protocol errors still fail immediately.

No root is used for verification.

## 7. Validate the Compose Agent overlay

```bash
docker compose \
  --env-file .env \
  -f compose.yml \
  -f compose.agent.yml \
  config
```

The rendered `serverfs-mcp` service should gain only:

- Agent Bridge environment variables;
- per-workdir Agent mode/runtime variables;
- read-only socket-dir bind;
- read-only lock-dir bind.

Its UID/GID must be the current user's UID/GID from `.env`.

The base `compose.yml` remains usable without any Agent runtime directories
and keeps the default 11-tool surface.

## 8. Deploy the MCP container with Agent overlay

```bash
docker compose \
  --env-file .env \
  -f compose.yml \
  -f compose.agent.yml \
  up -d
```

Then prove container -> host Bridge connectivity:

```bash
docker compose \
  --env-file .env \
  -f compose.yml \
  -f compose.agent.yml \
  exec serverfs-mcp python -c '
import asyncio
from pathlib import Path
from serverfs_mcp.agent_client import AgentBridgeClient
async def main():
    c = AgentBridgeClient(Path("/run/serverfs-agent-bridge/bridge.sock"))
    print(await c.call("runtime.list", {}))
asyncio.run(main())
'
```

This must work with both host runtime mounts read-only.

## 9. Native provider parity

Compare the same user's direct CLIs:

```bash
"$SERVERFS_CODEX_BIN" --version
"$SERVERFS_CLAUDE_BIN" --version
```

with `list_agent_runtimes`.

If direct CLI works but the Bridge runtime is unavailable, inspect only the
user service environment and provider-native configuration. Do not add a
ServerFS sandbox/permission override.

For Codex, `runtime.list` reports the version self-reported by the managed App Server
daemon, not merely the version of the `codex` executable on `PATH`. If those versions
differ after a CLI upgrade, treat the native daemon as stale before release acceptance.
For current Codex releases, prefer the provider-native `codex app-server daemon update`
flow over a blind `restart`: the update flow is designed to prepare/validate a compatible
managed daemon package and migrate legacy daemon layouts before replacing a running
server. It may interrupt active work. If the provider reports `unsupported`, distinguish
an externally/unmanaged app-server from a damaged managed installation before doing
anything else. A responsive unmanaged app-server must be retired before `daemon bootstrap`:
Codex intentionally refuses to bootstrap while the control socket is served by a process
that is not owned by its daemon manager. After proving there are no active tasks, stop that
known same-user unmanaged app-server gracefully, wait for its socket to disappear, then run
`codex app-server daemon bootstrap` (without `--remote-control` unless remote control is
actually required) and re-check `daemon version` plus Bridge `runtime.list`. Do not paper
over version mismatch in Bridge reporting, manually delete a live control socket, or use
SIGKILL as a migration shortcut.

## 10. Final ChatGPT E2E

Only after host/container verification:

1. confirm the OpenAI tunnel is healthy;
2. refresh/reconnect the ServerFS MCP integration if the old 11-tool schema is
   cached;
3. verify the 19-tool surface;
4. call `list_agent_runtimes`;
5. submit disposable Codex and Claude tasks and poll them;
6. exercise native approval/question paths when providers request them;
7. verify an Agent writer causes simultaneous ServerFS mutation
   `WORKDIR_BUSY`;
8. cancel a long-running task and verify lease release.

Use a disposable read-write workdir first.

## Update and rollback

Re-running:

```bash
deployment/agent-bridge/install.sh
```

installs a new user-owned release and keeps the previous one.

Rollback:

```bash
deployment/agent-bridge/rollback_app.sh
```

The script first proves the systemd user bus is reachable and validates both release
entrypoints. It prepares unique candidate symlinks before stopping a live service, then
swaps `current` and `previous`. If either swap or the service restart fails, it attempts
to restore the original links and original running state before exiting non-zero. It does
not alter config, provider settings or Compose.

To disable Agent delegation entirely:

```bash
docker compose --env-file .env -f compose.yml up -d --force-recreate serverfs-mcp
systemctl --user disable --now serverfs-agent-bridge.service
```

Without `compose.agent.yml`, Agent env/mounts disappear and ServerFS returns
to the default 11-tool surface.

## Crash/restart behavior

SIGTERM/SIGINT triggers graceful Bridge shutdown and socket cleanup.

After an abnormal crash, a same-user stale Unix socket is recovered only when
the Bridge proves there is no active listener and the socket inode/owner did
not change during the check. Active sockets, symlinks, ordinary files and
other-user sockets remain fail-closed.

## Post-release verification and rollback

Before host acceptance, run both independent code gates: the repository-root gate from
`AGENTS.md`, the complete `agent_bridge/` uv/ruff/pytest gate, and
`bash -n deployment/agent-bridge/*.sh` for deployment shell syntax. Root `pytest`
collects only `tests/` and does not validate `agent_bridge/tests/`.

For the ServerFS v0.5.0 package, verify the target host proves:

- install/update/rollback require no sudo/root;
- real peer UID/GID equal the current login user;
- user service start/stop/restart and abnormal-crash recovery work;
- socket/lock ownership and modes are correct;
- real read-only bind mounts work;
- container-to-Bridge RPC works;
- provider environment parity holds;
- Codex managed-daemon version matches the selected direct CLI after upgrades;
- Codex and Claude runtime discovery works;
- Agent mode exposes 19 tools without binary transfer and 21 tools with binary transfer;
- real submit/poll/HITL/cancel works;
- shared writer lease works across host/container;
- rollback implementation and recovery tests remain green; the live base
  11-tool rollback/re-cutover drill was **WAIVED BY MAINTAINER for v0.3.0**
  (2026-09-20) as a historical release decision and is not repeated as a v0.5.0
  verification requirement;
- no provider credentials enter the MCP container;
- MCP container still has no Internet egress;
- unattended deployments either have user linger enabled or explicitly
  document that Agent availability begins only when the user manager is active.

Without `compose.agent.yml`, Agent env/mounts disappear and ServerFS returns
to the default 11-tool surface. Workdir `AGENT_MODE/RUNTIMES` values in
`.env` are inert because the base Compose does not pass them into the
container.

If post-release verification fails, use the documented rollback script to restore the
previous user-scoped Bridge release, configuration and unit state; after publication,
do not recreate or move the `v0.5.0` tag.
