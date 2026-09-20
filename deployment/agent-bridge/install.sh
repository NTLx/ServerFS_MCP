#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  deployment/agent-bridge/install.sh [options]

Options:
  --env-file PATH        ServerFS .env (default: <repo>/.env)
  --uv-bin PATH          Absolute uv executable (default: command -v uv)
  --no-start             Stage the update without starting/restarting the user service

Everything is installed under the CURRENT USER:
  ~/.local/share/serverfs-agent-bridge
  ~/.local/state/serverfs-agent-bridge
  ~/.config/serverfs-agent-bridge
  ~/.config/systemd/user

The Unix socket and lock files live below the persistent
~/.local/share/serverfs-agent-bridge/runtime tree. This keeps Docker bind-mount
directory inodes stable across Bridge restart, logout and host reboot.

No sudo/root, system user/group creation, /etc, /opt or /var/lib writes are used.
EOF
}

if [[ $EUID -eq 0 ]]; then
  echo "Do not run the Phase E installer as root; use the normal login user." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
UV_BIN=""
START_SERVICE=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --uv-bin)
      UV_BIN="${2:-}"
      shift 2
      ;;
    --no-start)
      START_SERVICE=false
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "${HOME:-}" || "$HOME" != /* ]]; then
  echo "HOME must be an absolute user home directory" >&2
  exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ServerFS env file is missing: $ENV_FILE" >&2
  exit 1
fi

if [[ -z "$UV_BIN" ]]; then
  UV_BIN="$(command -v uv || true)"
fi
if [[ -z "$UV_BIN" || "$UV_BIN" != /* || ! -x "$UV_BIN" ]]; then
  echo "uv is required as an absolute executable path; use --uv-bin if needed" >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl is required for the systemd user service" >&2
  exit 1
fi
if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "A working systemd user manager is required (systemctl --user)." >&2
  if [[ -z "${XDG_RUNTIME_DIR:-}" ]]; then
    echo "XDG_RUNTIME_DIR is unset, so this process cannot reach the user bus." >&2
    echo "From a non-login shell (cron, CI, an agent session) run first:" >&2
    echo "  export XDG_RUNTIME_DIR=/run/user/$(id -u)" >&2
  fi
  exit 1
fi

CONFIG_DIR="$HOME/.config/serverfs-agent-bridge"
CONFIG_PATH="$CONFIG_DIR/config.json"
PROVIDER_ENV="$CONFIG_DIR/provider.env"
DATA_ROOT="$HOME/.local/share/serverfs-agent-bridge"
RELEASES_DIR="$DATA_ROOT/releases"
CURRENT_LINK="$DATA_ROOT/current"
PREVIOUS_LINK="$DATA_ROOT/previous"
RUNTIME_ROOT="$DATA_ROOT/runtime"
SOCKET_DIR="$RUNTIME_ROOT/socket"
LOCK_DIR="$RUNTIME_ROOT/locks"
STATE_DIR="$HOME/.local/state/serverfs-agent-bridge"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/serverfs-agent-bridge.service"
SERVICE_NAME="serverfs-agent-bridge.service"

for path in   "$CONFIG_DIR"   "$DATA_ROOT"   "$RELEASES_DIR"   "$RUNTIME_ROOT"   "$STATE_DIR"   "$UNIT_DIR"   "$SOCKET_DIR"   "$LOCK_DIR"
do
  if [[ -L "$path" ]]; then
    echo "Refusing symlink deployment directory: $path" >&2
    exit 1
  fi
done

mkdir -p   "$CONFIG_DIR"   "$RELEASES_DIR"   "$RUNTIME_ROOT"   "$STATE_DIR"   "$UNIT_DIR"   "$SOCKET_DIR"   "$LOCK_DIR"
chmod 700 "$CONFIG_DIR" "$DATA_ROOT" "$RELEASES_DIR" "$RUNTIME_ROOT" "$STATE_DIR"
chmod 750 "$SOCKET_DIR" "$LOCK_DIR"

if [[ ! -f "$REPO_ROOT/agent_bridge/pyproject.toml" || ! -f "$REPO_ROOT/agent_bridge/uv.lock" ]]; then
  echo "agent_bridge source tree is incomplete" >&2
  exit 1
fi

release_stamp="$(date -u +%Y%m%d%H%M%S)"
git_sha="$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD 2>/dev/null || echo source)"
if [[ "$git_sha" != "source" ]]; then
  git_dirty="$(git -C "$REPO_ROOT" status --porcelain --untracked-files=normal -- \
    agent_bridge/pyproject.toml agent_bridge/uv.lock agent_bridge/src 2>/dev/null || true)"
  if [[ -n "$git_dirty" ]]; then
    git_sha="${git_sha}-dirty"
  fi
fi
RELEASE_DIR="$RELEASES_DIR/${release_stamp}-${git_sha}"
STAGE="$(mktemp -d "$RELEASES_DIR/.stage.XXXXXX")"
cleanup() {
  rm -rf "$STAGE"
}
trap cleanup EXIT

cp "$REPO_ROOT/agent_bridge/pyproject.toml" "$STAGE/"
cp "$REPO_ROOT/agent_bridge/uv.lock" "$STAGE/"
cp -R "$REPO_ROOT/agent_bridge/src" "$STAGE/src"

if [[ -e "$RELEASE_DIR" ]]; then
  echo "Release path already exists: $RELEASE_DIR" >&2
  exit 1
fi
mv "$STAGE" "$RELEASE_DIR"
trap - EXIT

if ! (
  cd "$RELEASE_DIR"
  "$UV_BIN" sync --frozen --no-dev --no-editable
); then
  rm -rf "$RELEASE_DIR"
  echo "uv sync failed; current installed release was not changed" >&2
  exit 1
fi

if [[ ! -e "$PROVIDER_ENV" ]]; then
  cp "$SCRIPT_DIR/provider.env.example" "$PROVIDER_ENV"
fi
if [[ -L "$PROVIDER_ENV" || ! -f "$PROVIDER_ENV" ]]; then
  rm -rf "$RELEASE_DIR"
  echo "provider.env must be a regular non-symlink file" >&2
  exit 1
fi
chmod 600 "$PROVIDER_ENV"

CONFIG_NEXT="$CONFIG_DIR/.config.next.json"
if ! python3 "$SCRIPT_DIR/render_config.py" \
  --env-file "$ENV_FILE" \
  --output "$CONFIG_NEXT"
then
  rm -rf "$RELEASE_DIR"
  echo "config rendering failed; current installed release was not changed" >&2
  exit 1
fi
chmod 600 "$CONFIG_NEXT"

if ! "$RELEASE_DIR/.venv/bin/python" -c \
  'import sys; from pathlib import Path; from serverfs_agent_bridge.config import BridgeConfig; BridgeConfig.load(Path(sys.argv[1]))' \
  "$CONFIG_NEXT"
then
  rm -f "$CONFIG_NEXT"
  rm -rf "$RELEASE_DIR"
  echo "rendered config is rejected by the installed Bridge; current release was not changed" >&2
  exit 1
fi

old_current=""
if [[ -L "$CURRENT_LINK" ]]; then
  old_current="$(readlink -f "$CURRENT_LINK")"
  case "$old_current" in
    "$RELEASES_DIR"/*) ;;
    *)
      rm -f "$CONFIG_NEXT"
      rm -rf "$RELEASE_DIR"
      echo "Current application target escapes the releases directory" >&2
      exit 1
      ;;
  esac
  if [[ ! -d "$old_current" ]]; then
    rm -f "$CONFIG_NEXT"
    rm -rf "$RELEASE_DIR"
    echo "Current application target is missing: $old_current" >&2
    exit 1
  fi
elif [[ -e "$CURRENT_LINK" ]]; then
  rm -f "$CONFIG_NEXT"
  rm -rf "$RELEASE_DIR"
  echo "Current application path must be a symlink: $CURRENT_LINK" >&2
  exit 1
fi

old_previous=""
if [[ -L "$PREVIOUS_LINK" ]]; then
  old_previous="$(readlink -f "$PREVIOUS_LINK")"
  case "$old_previous" in
    "$RELEASES_DIR"/*) ;;
    *)
      rm -f "$CONFIG_NEXT"
      rm -rf "$RELEASE_DIR"
      echo "Previous application target escapes the releases directory" >&2
      exit 1
      ;;
  esac
  if [[ ! -d "$old_previous" ]]; then
    rm -f "$CONFIG_NEXT"
    rm -rf "$RELEASE_DIR"
    echo "Previous application target is missing: $old_previous" >&2
    exit 1
  fi
elif [[ -e "$PREVIOUS_LINK" ]]; then
  rm -f "$CONFIG_NEXT"
  rm -rf "$RELEASE_DIR"
  echo "Previous application path must be a symlink: $PREVIOUS_LINK" >&2
  exit 1
fi

if [[ -e "$CONFIG_PATH" && ( -L "$CONFIG_PATH" || ! -f "$CONFIG_PATH" ) ]]; then
  rm -f "$CONFIG_NEXT"
  rm -rf "$RELEASE_DIR"
  echo "Existing Bridge config must be a regular non-symlink file: $CONFIG_PATH" >&2
  exit 1
fi
if [[ -e "$UNIT_PATH" && ( -L "$UNIT_PATH" || ! -f "$UNIT_PATH" ) ]]; then
  rm -f "$CONFIG_NEXT"
  rm -rf "$RELEASE_DIR"
  echo "Existing user unit must be a regular non-symlink file: $UNIT_PATH" >&2
  exit 1
fi

CONFIG_BACKUP=""
UNIT_BACKUP=""
if [[ -f "$CONFIG_PATH" ]]; then
  CONFIG_BACKUP="$(mktemp "$CONFIG_DIR/.config.previous.XXXXXX")"
  cp -p "$CONFIG_PATH" "$CONFIG_BACKUP"
fi
if [[ -f "$UNIT_PATH" ]]; then
  UNIT_BACKUP="$(mktemp "$UNIT_DIR/.serverfs-agent-bridge.service.previous.XXXXXX")"
  cp -p "$UNIT_PATH" "$UNIT_BACKUP"
fi

cleanup_activation_backups() {
  if [[ -n "$CONFIG_BACKUP" ]]; then
    rm -f "$CONFIG_BACKUP"
  fi
  if [[ -n "$UNIT_BACKUP" ]]; then
    rm -f "$UNIT_BACKUP"
  fi
}
trap cleanup_activation_backups EXIT

was_enabled=false
if systemctl --user is-enabled --quiet "$SERVICE_NAME"; then
  was_enabled=true
fi

was_active=false
if systemctl --user is-active --quiet "$SERVICE_NAME"; then
  was_active=true
  if [[ "$START_SERVICE" == "true" ]]; then
    systemctl --user stop "$SERVICE_NAME"
  fi
fi

restore_failed_activation() {
  local reason="$1"
  local recovery_failed=false
  local current_restore="$DATA_ROOT/.current.restore.$$"
  local previous_restore="$DATA_ROOT/.previous.restore.$$"

  echo "Activation failed ($reason); restoring the previous user-scoped deployment." >&2
  set +e

  rm -f "$current_restore" "$previous_restore"

  if [[ -n "$old_current" ]]; then
    if ! ln -s "$old_current" "$current_restore" || ! mv -Tf "$current_restore" "$CURRENT_LINK"; then
      recovery_failed=true
    fi
  elif ! rm -f "$CURRENT_LINK"; then
    recovery_failed=true
  fi

  if [[ -n "$CONFIG_BACKUP" && -f "$CONFIG_BACKUP" ]]; then
    if mv -f "$CONFIG_BACKUP" "$CONFIG_PATH"; then
      CONFIG_BACKUP=""
    else
      recovery_failed=true
    fi
  elif ! rm -f "$CONFIG_PATH"; then
    recovery_failed=true
  fi

  if [[ -n "$old_previous" ]]; then
    if ! ln -s "$old_previous" "$previous_restore" || ! mv -Tf "$previous_restore" "$PREVIOUS_LINK"; then
      recovery_failed=true
    fi
  elif ! rm -f "$PREVIOUS_LINK"; then
    recovery_failed=true
  fi

  if [[ -n "$UNIT_BACKUP" && -f "$UNIT_BACKUP" ]]; then
    if mv -f "$UNIT_BACKUP" "$UNIT_PATH"; then
      UNIT_BACKUP=""
    else
      recovery_failed=true
    fi
  elif ! rm -f "$UNIT_PATH"; then
    recovery_failed=true
  fi

  if ! systemctl --user daemon-reload; then
    recovery_failed=true
  fi
  if [[ "$START_SERVICE" == "true" ]]; then
    if [[ "$was_enabled" == "true" ]]; then
      if ! systemctl --user enable "$SERVICE_NAME" >/dev/null 2>&1; then
        recovery_failed=true
      fi
    else
      systemctl --user disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi
    if [[ "$was_active" == "true" ]]; then
      if ! systemctl --user start "$SERVICE_NAME"; then
        recovery_failed=true
      fi
    else
      systemctl --user stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi
  fi

  rm -f "$current_restore" "$previous_restore"
  rm -rf "$RELEASE_DIR"

  set -e
  if [[ "$recovery_failed" == "true" ]]; then
    echo "Automatic restore was incomplete; inspect the user service before retrying." >&2
  else
    echo "Previous user-scoped deployment restored." >&2
  fi
}

CURRENT_NEW="$DATA_ROOT/.current.new.$$"
PREVIOUS_NEW="$DATA_ROOT/.previous.new.$$"
rm -f "$CURRENT_NEW" "$PREVIOUS_NEW"

if ! ln -s "$RELEASE_DIR" "$CURRENT_NEW" || ! mv -Tf "$CURRENT_NEW" "$CURRENT_LINK"; then
  restore_failed_activation "current symlink switch"
  exit 1
fi
if ! mv -f "$CONFIG_NEXT" "$CONFIG_PATH"; then
  restore_failed_activation "config switch"
  exit 1
fi

if [[ -n "$old_current" ]]; then
  if ! ln -s "$old_current" "$PREVIOUS_NEW" || ! mv -Tf "$PREVIOUS_NEW" "$PREVIOUS_LINK"; then
    restore_failed_activation "previous symlink switch"
    exit 1
  fi
fi

if ! cp "$SCRIPT_DIR/serverfs-agent-bridge.service" "$UNIT_PATH" || ! chmod 644 "$UNIT_PATH"; then
  restore_failed_activation "user unit install"
  exit 1
fi
if ! systemctl --user daemon-reload; then
  restore_failed_activation "systemd daemon-reload"
  exit 1
fi
if [[ "$START_SERVICE" == "true" ]] && ! systemctl --user enable --now "$SERVICE_NAME"; then
  restore_failed_activation "service enable/start"
  exit 1
fi

rm -f "$CURRENT_NEW" "$PREVIOUS_NEW"

if [[ "$START_SERVICE" == "false" ]]; then
  if [[ "$was_active" == "true" ]]; then
    echo "Update staged with --no-start; the existing Bridge process was left running."
    echo "Restart the user service explicitly when ready to activate the staged release."
  else
    echo "Update staged with --no-start; the user service remains stopped."
    echo "Start the user service explicitly when ready to activate the staged release."
  fi
fi

cat <<EOF
ServerFS Agent Bridge installed entirely in the current user's scope.

Application:
  $CURRENT_LINK
Config:
  $CONFIG_PATH
Provider env:
  $PROVIDER_ENV
User service:
  $UNIT_PATH
Runtime socket dir:
  $SOCKET_DIR
Runtime lock dir:
  $LOCK_DIR

No sudo/root operation was performed.

Next validate:
  python3 $SCRIPT_DIR/verify_host.py

Then validate the Compose overlay:
  docker compose --env-file "$ENV_FILE" -f compose.yml -f compose.agent.yml config
EOF
