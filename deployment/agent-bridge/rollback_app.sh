#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
  echo "Do not run rollback_app.sh as root; use the normal login user." >&2
  exit 1
fi

if [[ -z "${HOME:-}" || "$HOME" != /* ]]; then
  echo "HOME must be an absolute user home directory" >&2
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

DATA_ROOT="$HOME/.local/share/serverfs-agent-bridge"
CURRENT="$DATA_ROOT/current"
PREVIOUS="$DATA_ROOT/previous"
SERVICE_NAME="serverfs-agent-bridge.service"

if [[ ! -L "$CURRENT" ]]; then
  echo "Current application symlink is missing: $CURRENT" >&2
  exit 1
fi
if [[ ! -L "$PREVIOUS" ]]; then
  echo "No previous application is available: $PREVIOUS" >&2
  exit 1
fi

current_target="$(readlink -f "$CURRENT")"
previous_target="$(readlink -f "$PREVIOUS")"
case "$current_target" in
  "$DATA_ROOT"/releases/*) ;;
  *) echo "Current target escapes user deployment root" >&2; exit 1 ;;
esac
case "$previous_target" in
  "$DATA_ROOT"/releases/*) ;;
  *) echo "Previous target escapes user deployment root" >&2; exit 1 ;;
esac
if [[ ! -d "$current_target" || ! -d "$previous_target" ]]; then
  echo "Current/previous release target is missing" >&2
  exit 1
fi
for target in "$current_target" "$previous_target"; do
  if [[ ! -x "$target/.venv/bin/serverfs-agent-bridge" ]]; then
    echo "Rollback target has no executable Agent Bridge entrypoint: $target" >&2
    exit 1
  fi
done

CURRENT_NEW="$DATA_ROOT/.current.rollback.$$"
PREVIOUS_NEW="$DATA_ROOT/.previous.rollback.$$"
CURRENT_RESTORE="$DATA_ROOT/.current.restore.$$"
PREVIOUS_RESTORE="$DATA_ROOT/.previous.restore.$$"

cleanup_temporary_links() {
  rm -f "$CURRENT_NEW" "$PREVIOUS_NEW" "$CURRENT_RESTORE" "$PREVIOUS_RESTORE"
}
trap cleanup_temporary_links EXIT

# Prepare both candidate links before stopping a live service. Clear only this
# process-specific temp names first so a stale file from a reused PID cannot strand
# the service down.
rm -f "$CURRENT_NEW" "$PREVIOUS_NEW"
ln -s "$previous_target" "$CURRENT_NEW"
ln -s "$current_target" "$PREVIOUS_NEW"

was_active=false
if systemctl --user is-active --quiet "$SERVICE_NAME"; then
  was_active=true
  systemctl --user stop "$SERVICE_NAME"
fi

restore_failed_rollback() {
  local reason="$1"
  local recovery_failed=false

  echo "Rollback failed ($reason); restoring the original application links." >&2
  set +e

  rm -f "$CURRENT_RESTORE" "$PREVIOUS_RESTORE"
  if ! ln -s "$current_target" "$CURRENT_RESTORE" || ! mv -Tf "$CURRENT_RESTORE" "$CURRENT"; then
    recovery_failed=true
  fi
  if ! ln -s "$previous_target" "$PREVIOUS_RESTORE" || ! mv -Tf "$PREVIOUS_RESTORE" "$PREVIOUS"; then
    recovery_failed=true
  fi

  if [[ "$was_active" == "true" ]]; then
    if ! systemctl --user start "$SERVICE_NAME"; then
      recovery_failed=true
    fi
  fi

  set -e
  if [[ "$recovery_failed" == "true" ]]; then
    echo "Automatic rollback recovery was incomplete; inspect the user service." >&2
  else
    echo "Original user-scoped deployment restored." >&2
  fi
}

if ! mv -Tf "$CURRENT_NEW" "$CURRENT"; then
  restore_failed_rollback "current symlink switch"
  exit 1
fi
if ! mv -Tf "$PREVIOUS_NEW" "$PREVIOUS"; then
  restore_failed_rollback "previous symlink switch"
  exit 1
fi

if [[ "$was_active" == "true" ]] && ! systemctl --user start "$SERVICE_NAME"; then
  restore_failed_rollback "service restart"
  exit 1
fi

trap - EXIT
cleanup_temporary_links

cat <<EOF
User-scoped Agent Bridge application rollback completed.

Current:
  $(readlink -f "$CURRENT")
Previous:
  $(readlink -f "$PREVIOUS")

No config, provider settings, Docker Compose files or system resources changed.
EOF
