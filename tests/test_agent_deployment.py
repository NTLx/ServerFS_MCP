from __future__ import annotations

import importlib.util
import os
import re
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO_ROOT / "deployment" / "agent-bridge" / "render_config.py"
_SPEC = importlib.util.spec_from_file_location("serverfs_render_agent_config", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
render = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render)

_VERIFY_MODULE_PATH = _REPO_ROOT / "deployment" / "agent-bridge" / "verify_host.py"
_VERIFY_SPEC = importlib.util.spec_from_file_location(
    "serverfs_verify_agent_host",
    _VERIFY_MODULE_PATH,
)
assert _VERIFY_SPEC is not None and _VERIFY_SPEC.loader is not None
verify_host = importlib.util.module_from_spec(_VERIFY_SPEC)
_VERIFY_SPEC.loader.exec_module(verify_host)


def make_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def valid_env(tmp_path: Path) -> dict[str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    codex = make_executable(tmp_path / "codex")
    return {
        "SERVERFS_IMAGE": "serverfs-mcp:dev",
        "SERVERFS_UID": str(os.getuid()),
        "SERVERFS_GID": str(os.getgid()),
        "SERVERFS_AGENT_BRIDGE_ENABLED": "true",
        "SERVERFS_AGENT_PEER_UID": str(os.getuid()),
        "SERVERFS_AGENT_PEER_GID": str(os.getgid()),
        "SERVERFS_AGENT_BRIDGE_HOST_SOCKET_DIR": str(tmp_path / "runtime/socket"),
        "SERVERFS_AGENT_LOCK_HOST_DIR": str(tmp_path / "runtime/locks"),
        "SERVERFS_AGENT_BRIDGE_STATE_DIR": str(tmp_path / "state"),
        "SERVERFS_CODEX_BIN": str(codex),
        "WORKDIR_01_ALIAS": "repo",
        "WORKDIR_01_PATH": str(repo),
        "WORKDIR_01_READ_ONLY": "false",
        "WORKDIR_01_AGENT_MODE": "workspace-write",
        "WORKDIR_01_AGENT_RUNTIMES": "codex",
    }


def test_build_config_uses_same_user_identity_and_user_paths(tmp_path: Path) -> None:
    values = valid_env(tmp_path)
    config = render.build_config(values)

    assert config["allowed_peer_uid"] == os.getuid()
    assert config["allowed_peer_gid"] == os.getgid()
    assert config["socket_path"] == str(tmp_path / "runtime/socket/bridge.sock")
    assert config["lock_dir"] == str(tmp_path / "runtime/locks")
    assert config["state_dir"] == str(tmp_path / "state")
    assert config["enable_fake_runtime"] is False
    assert config["codex"]["enabled"] is True
    assert config["claude"]["enabled"] is False
    assert "jev" not in config
    assert config["workdirs"] == [
        {
            "slot": 1,
            "alias": "repo",
            "host_path": str((tmp_path / "repo").resolve()),
            "read_only": False,
            "agent_mode": "workspace-write",
            "agent_runtimes": ["codex"],
        }
    ]


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("SERVERFS_UID", "current user id"),
        ("SERVERFS_AGENT_PEER_UID", "current user id"),
        ("SERVERFS_GID", "primary group id"),
        ("SERVERFS_AGENT_PEER_GID", "primary group id"),
    ],
)
def test_identity_must_match_current_user(tmp_path: Path, key: str, message: str) -> None:
    values = valid_env(tmp_path)
    values[key] = str((os.getuid() if "UID" in key else os.getgid()) + 1)
    with pytest.raises(render.ConfigRenderError, match=message):
        render.build_config(values)


def test_jev_api_key_is_opt_in_and_rendered_only_when_nonempty(tmp_path: Path) -> None:
    values = valid_env(tmp_path)
    assert "jev" not in render.build_config(values)

    values["SERVERFS_JEV_API_KEY"] = "jev-test-secret-123"
    config = render.build_config(values)
    assert config["jev"] == {"api_key": "jev-test-secret-123"}

    values["SERVERFS_JEV_API_KEY"] = "bad key"
    with pytest.raises(render.ConfigRenderError, match="SERVERFS_JEV_API_KEY"):
        render.build_config(values)


def test_single_env_is_source_of_truth_and_ignores_unrelated_keys(tmp_path: Path) -> None:
    values = valid_env(tmp_path)
    values["SERVERFS_LOG_LEVEL"] = "DEBUG"
    values["CONTROL_PLANE_BASE_URL"] = "https://api.openai.com"

    config = render.build_config(values)

    assert config["workdirs"][0]["agent_mode"] == "workspace-write"
    assert "SERVERFS_LOG_LEVEL" not in str(config)
    assert "CONTROL_PLANE_BASE_URL" not in str(config)


@pytest.mark.parametrize("value", ["0", "nan", "inf", "-inf"])
def test_invalid_bridge_timeout_fails_closed(tmp_path: Path, value: str) -> None:
    values = valid_env(tmp_path)
    values["SERVERFS_AGENT_BRIDGE_TIMEOUT_SECONDS"] = value
    with pytest.raises(render.ConfigRenderError, match="must be positive"):
        render.build_config(values)


@pytest.mark.parametrize(
    "key",
    [
        "SERVERFS_AGENT_BRIDGE_HOST_SOCKET_DIR",
        "SERVERFS_AGENT_LOCK_HOST_DIR",
        "SERVERFS_AGENT_BRIDGE_STATE_DIR",
    ],
)
def test_runtime_paths_must_be_absolute(tmp_path: Path, key: str) -> None:
    values = valid_env(tmp_path)
    values[key] = "relative/path"
    with pytest.raises(render.ConfigRenderError, match="must be an absolute path"):
        render.build_config(values)


def test_missing_measured_peer_identity_fails_closed(tmp_path: Path) -> None:
    values = valid_env(tmp_path)
    values["SERVERFS_AGENT_PEER_UID"] = ""
    with pytest.raises(render.ConfigRenderError, match="measured host credentials"):
        render.build_config(values)


def test_enabled_provider_requires_absolute_executable(tmp_path: Path) -> None:
    values = valid_env(tmp_path)
    values["SERVERFS_CODEX_BIN"] = "codex"
    with pytest.raises(render.ConfigRenderError, match="absolute executable"):
        render.build_config(values)


def test_agent_policy_mismatch_fails_before_render(tmp_path: Path) -> None:
    values = valid_env(tmp_path)
    values["WORKDIR_01_READ_ONLY"] = "true"
    with pytest.raises(render.ConfigRenderError, match="READ_ONLY=false"):
        render.build_config(values)


def test_duplicate_alias_fails_before_render(tmp_path: Path) -> None:
    values = valid_env(tmp_path)
    repo2 = tmp_path / "repo2"
    repo2.mkdir()
    values.update(
        {
            "WORKDIR_02_ALIAS": "repo",
            "WORKDIR_02_PATH": str(repo2),
            "WORKDIR_02_READ_ONLY": "true",
        }
    )
    with pytest.raises(render.ConfigRenderError, match="duplicates"):
        render.build_config(values)


def test_disabled_slot_cannot_be_read_write(tmp_path: Path) -> None:
    values = valid_env(tmp_path)
    values["WORKDIR_02_READ_ONLY"] = "false"
    with pytest.raises(render.ConfigRenderError, match="disabled slot"):
        render.build_config(values)


def test_no_agent_enabled_workdir_fails(tmp_path: Path) -> None:
    values = valid_env(tmp_path)
    values["WORKDIR_01_AGENT_MODE"] = "disabled"
    values["WORKDIR_01_AGENT_RUNTIMES"] = ""
    with pytest.raises(render.ConfigRenderError, match="at least one workdir"):
        render.build_config(values)


def test_tunnel_secrets_are_not_rendered_into_bridge_config(tmp_path: Path) -> None:
    values = valid_env(tmp_path)
    # These sentinels must not be a substring of tmp_path: pytest derives the
    # directory name from this test's own name, and every path under tmp_path is
    # rendered verbatim into the config, so a sentinel like "tunnel_secret"
    # would match the path rather than a leaked value.
    values["CONTROL_PLANE_API_KEY"] = "SENTINEL-API-KEY-7f31"
    values["CONTROL_PLANE_TUNNEL_ID"] = "SENTINEL-TUNNEL-ID-7f31"
    config = render.build_config(values)
    encoded = str(config)
    assert "SENTINEL-API-KEY-7f31" not in encoded
    assert "SENTINEL-TUNNEL-ID-7f31" not in encoded


def test_env_parser_never_executes_shell_syntax(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        'SAFE="$(touch /tmp/serverfs-render-should-not-exist)"\nQUOTED="hello world"\n',
        encoding="utf-8",
    )
    marker = Path("/tmp/serverfs-render-should-not-exist")
    marker.unlink(missing_ok=True)
    values = render.load_env_file(path)
    assert values["SAFE"] == "$(touch /tmp/serverfs-render-should-not-exist)"
    assert values["QUOTED"] == "hello world"
    assert not marker.exists()


def test_atomic_output_is_private_user_file(tmp_path: Path) -> None:
    values = valid_env(tmp_path)
    config = render.build_config(values)
    output = tmp_path / "config.json"

    render.write_atomic(output, config)

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.stat().st_uid == os.getuid()
    assert '"enable_fake_runtime": false' in output.read_text(encoding="utf-8")


def test_base_compose_remains_agent_unaware() -> None:
    text = (_REPO_ROOT / "compose.yml").read_text(encoding="utf-8")
    assert "SERVERFS_AGENT_BRIDGE_" not in text
    assert "WORKDIR_01_AGENT_MODE" not in text
    assert "/run/serverfs-agent-bridge" not in text
    assert "/run/serverfs-agent-locks" not in text


def test_agent_overlay_uses_user_host_dirs_and_read_only_container_mounts() -> None:
    text = (_REPO_ROOT / "compose.agent.yml").read_text(encoding="utf-8")
    assert "source: ${SERVERFS_AGENT_BRIDGE_HOST_SOCKET_DIR}" in text
    assert "target: /run/serverfs-agent-bridge" in text
    assert "source: ${SERVERFS_AGENT_LOCK_HOST_DIR}" in text
    assert "target: /run/serverfs-agent-locks" in text
    assert text.count("read_only: true") >= 2
    assert "create_host_path: false" in text
    assert "SERVERFS_AGENT_BRIDGE_SOCKET: /run/serverfs-agent-bridge/bridge.sock" in text
    assert "SERVERFS_AGENT_LOCK_DIR: /run/serverfs-agent-locks" in text
    assert "SERVERFS_CODEX_BIN" not in text
    assert "SERVERFS_CLAUDE_BIN" not in text
    assert "provider.env" not in text
    assert "network_mode:" not in text
    assert "networks:" not in text


def test_env_example_has_single_agent_policy_pair_for_all_slots() -> None:
    text = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert text.count("SERVERFS_AGENT_MODE=disabled") == 1
    assert text.count("SERVERFS_AGENT_RUNTIMES=") == 1
    assert text.count("SERVERFS_BINARY_TRANSFER_ENABLED=false") == 1
    assert text.count("SERVERFS_MAX_BINARY_TRANSFER_BYTES=8388608") == 1
    assert text.count("SERVERFS_JEV_API_KEY=") == 1
    for slot in range(1, 17):
        prefix = f"WORKDIR_{slot:02d}"
        assert text.count(f"{prefix}_AGENT_MODE=") == 1
        assert text.count(f"{prefix}_AGENT_RUNTIMES=") == 1


def test_systemd_unit_is_user_scoped_lifecycle_only() -> None:
    text = (_REPO_ROOT / "deployment" / "agent-bridge" / "serverfs-agent-bridge.service").read_text(
        encoding="utf-8"
    )

    assert "WantedBy=default.target" in text
    assert "WorkingDirectory=%h/.local/share/serverfs-agent-bridge/current" in text
    assert "EnvironmentFile=-%h/.config/serverfs-agent-bridge/provider.env" in text
    assert (
        "ExecStart=%h/.local/share/serverfs-agent-bridge/current/.venv/bin/"
        "serverfs-agent-bridge --config "
        "%h/.config/serverfs-agent-bridge/config.json"
    ) in text

    forbidden_directives = (
        "User",
        "Group",
        "SupplementaryGroups",
        "StateDirectory",
        "ProtectHome",
        "ProtectSystem",
        "PrivateUsers",
        "NoNewPrivileges",
        "RestrictAddressFamilies",
    )
    for directive in forbidden_directives:
        assert re.search(rf"(?m)^\\s*{directive}=", text) is None
    for forbidden in ("/etc/", "/opt/", "/var/lib/", "ExecStartPre=+"):
        assert forbidden not in text


def test_installer_and_rollback_are_user_scoped() -> None:
    install = (_REPO_ROOT / "deployment" / "agent-bridge" / "install.sh").read_text(
        encoding="utf-8"
    )
    rollback = (_REPO_ROOT / "deployment" / "agent-bridge" / "rollback_app.sh").read_text(
        encoding="utf-8"
    )

    for text in (install, rollback):
        assert "sudo " not in text
        assert "groupadd" not in text
        assert "/etc/" not in text
        assert "/opt/" not in text
        assert "/var/lib/" not in text
        assert re.search(r"(?m)^\s*systemctl\s+(?!--user\b)", text) is None

    assert "Do not run the Phase E installer as root" in install
    assert ".env.agent" not in install
    assert "--agent-env-file" not in install
    assert 'ENV_FILE="$REPO_ROOT/.env"' in install
    assert '--env-file "$ENV_FILE"' in install
    assert 'DATA_ROOT="$HOME/.local/share/serverfs-agent-bridge"' in install
    assert 'CONFIG_DIR="$HOME/.config/serverfs-agent-bridge"' in install
    assert 'STATE_DIR="$HOME/.local/state/serverfs-agent-bridge"' in install
    assert "serverfs-agent-bridge.service" in install
    assert "systemctl --user enable --now" in install
    assert "systemctl --user show-environment" in install
    assert "systemctl --user is-enabled --quiet" in install
    assert "restore_failed_activation" in install
    assert "Activation failed (" in install
    assert "Previous user-scoped deployment restored." in install
    assert 'git_sha="${git_sha}-dirty"' in install
    assert "--no-editable" in install
    assert "BridgeConfig.load" in install
    assert "current" in install and "previous" in install

    assert "Do not run rollback_app.sh as root" in rollback
    assert "systemctl --user" in rollback
    assert "systemctl --user show-environment" in rollback
    assert "restore_failed_rollback" in rollback
    assert "Original user-scoped deployment restored." in rollback
    assert ".current.rollback.$$" in rollback
    assert ".previous.rollback.$$" in rollback
    assert rollback.index("systemctl --user show-environment") < rollback.index("mv -Tf")
    assert rollback.index('ln -s "$previous_target" "$CURRENT_NEW"') < rollback.index(
        'systemctl --user stop "$SERVICE_NAME"'
    )
    assert install.index("systemctl --user show-environment") < install.index("mkdir -p")


def test_peercred_probe_is_user_scoped_and_uses_linux_so_peercred() -> None:
    text = (_REPO_ROOT / "deployment" / "agent-bridge" / "measure_peercred.py").read_text(
        encoding="utf-8"
    )
    assert "socket.SO_PEERCRED" in text
    assert "struct.unpack" in text
    assert "secrets.token_urlsafe" in text
    assert "user_scope_compatible" in text
    assert "Path.home()" in text
    assert "sudo " not in text


def test_peercred_probe_creates_its_own_directory_tree(tmp_path: Path) -> None:
    # The documented order runs the probe before install.sh creates the
    # deployment tree, so the probe must build its own missing parent
    # directories. Reaching accept() proves that; a regression fails with an
    # unhandled FileNotFoundError from mkdir instead. The names stay short
    # because an AF_UNIX sun_path is capped near 107 bytes.
    directory = tmp_path / "probe-root" / "peer-probe"

    result = subprocess.run(
        [
            "timeout",
            "3",
            sys.executable,
            str(_REPO_ROOT / "deployment/agent-bridge/measure_peercred.py"),
            "--directory",
            str(directory),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert "Traceback" not in result.stderr, result.stderr
    assert f"listening={directory / 'peer.sock'}" in result.stdout
    assert "token=" in result.stdout
    # `timeout` reports 124 only because the probe was still blocked in accept().
    assert result.returncode == 124, (result.returncode, result.stdout, result.stderr)
    assert directory.is_dir()


def test_verify_host_waits_for_bridge_socket_startup_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "bridge.sock"
    listener: socket.socket | None = None
    sleep_calls = 0

    def fake_probe(path: Path) -> None:
        assert path == socket_path

    def fake_sleep(_seconds: float) -> None:
        nonlocal listener, sleep_calls
        sleep_calls += 1
        if listener is None:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            os.chmod(socket_path, 0o660)

    monkeypatch.setattr(verify_host, "_probe_bridge", fake_probe)
    monkeypatch.setattr(verify_host.time, "sleep", fake_sleep)

    try:
        verify_host._wait_for_bridge_ready(
            socket_path,
            timeout_seconds=1.0,
            retry_interval_seconds=0.01,
        )
    finally:
        if listener is not None:
            listener.close()
        socket_path.unlink(missing_ok=True)

    assert sleep_calls >= 1


def test_verify_host_retries_transient_rpc_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A socket that exists but cannot serve RPC yet is transient, not fatal:
    # connect()/recv() failures must be retried the same way a missing socket is.
    socket_path = tmp_path / "bridge.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chmod(socket_path, 0o660)

    attempts = 0

    def flaky_probe(_path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionRefusedError("bridge is still binding")

    monkeypatch.setattr(verify_host, "_probe_bridge", flaky_probe)
    monkeypatch.setattr(verify_host.time, "sleep", lambda _seconds: None)

    try:
        verify_host._wait_for_bridge_ready(
            socket_path,
            timeout_seconds=1.0,
            retry_interval_seconds=0.01,
        )
    finally:
        listener.close()
        socket_path.unlink(missing_ok=True)

    assert attempts == 3


def test_verify_host_distinguishes_missing_socket_from_unready_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The two deadlines name different root causes; a socket that exists but
    # never answers must not be reported as "did not appear".
    socket_path = tmp_path / "bridge.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chmod(socket_path, 0o660)

    def failing_probe(_path: Path) -> None:
        raise ConnectionRefusedError("bridge never becomes ready")

    ticks = iter([0.0, *([1_000.0] * 8)])
    monkeypatch.setattr(verify_host, "_probe_bridge", failing_probe)
    monkeypatch.setattr(verify_host.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(verify_host.time, "monotonic", lambda: next(ticks, 1_000.0))

    try:
        with pytest.raises(verify_host.VerifyError, match="did not become ready"):
            verify_host._wait_for_bridge_ready(
                socket_path,
                timeout_seconds=1.0,
                retry_interval_seconds=0.01,
            )
    finally:
        listener.close()
        socket_path.unlink(missing_ok=True)


def test_verify_host_runtime_readiness_retries_until_all_enabled_are_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def fake_probe(_path: Path) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        return {
            "runtimes": [
                {"name": "claude", "available": True},
                {"name": "codex", "available": attempts >= 3},
            ]
        }

    monkeypatch.setattr(verify_host, "_probe_bridge", fake_probe)
    monkeypatch.setattr(verify_host.time, "sleep", lambda _seconds: None)

    verify_host._wait_for_enabled_runtimes_ready(
        Path("/unused"),
        {"codex", "claude"},
        timeout_seconds=1.0,
        retry_interval_seconds=0.01,
    )

    assert attempts == 3


def test_verify_host_runtime_readiness_times_out_without_starting_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_probe(_path: Path) -> dict[str, object]:
        return {
            "runtimes": [
                {"name": "claude", "available": True},
                {"name": "codex", "available": False},
            ]
        }

    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(verify_host, "_probe_bridge", fake_probe)
    monkeypatch.setattr(verify_host.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(verify_host.time, "monotonic", lambda: next(ticks, 2.0))

    with pytest.raises(verify_host.VerifyError, match="codex"):
        verify_host._wait_for_enabled_runtimes_ready(
            Path("/unused"),
            {"codex", "claude"},
            timeout_seconds=1.0,
            retry_interval_seconds=0.01,
        )


def test_deployment_executables_never_require_privileged_install() -> None:
    names = [
        "install.sh",
        "rollback_app.sh",
        "measure_peercred.py",
        "verify_host.py",
    ]
    for name in names:
        text = (_REPO_ROOT / "deployment/agent-bridge" / name).read_text(encoding="utf-8")
        assert "sudo " not in text
        assert "groupadd" not in text
        assert "must run as root" not in text


def test_phase_e_has_no_second_repository_env_layer() -> None:
    assert not (_REPO_ROOT / ".env.agent.example").exists()
    assert ".env.agent" not in (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    for path in (
        _REPO_ROOT / "compose.agent.yml",
        _REPO_ROOT / "deployment/agent-bridge/install.sh",
        _REPO_ROOT / "deployment/agent-bridge/render_config.py",
        _REPO_ROOT / "deployment/agent-bridge/README.md",
    ):
        text = path.read_text(encoding="utf-8")
        assert ".env.agent" not in text
        assert "--agent-env-file" not in text


def test_single_env_example_contains_user_scoped_agent_deployment() -> None:
    text = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert not (_REPO_ROOT / ".env.agent.example").exists()
    for key in (
        "SERVERFS_UID=",
        "SERVERFS_GID=",
        "SERVERFS_AGENT_BRIDGE_ENABLED=false",
        "SERVERFS_AGENT_PEER_UID=",
        "SERVERFS_AGENT_PEER_GID=",
        "SERVERFS_AGENT_BRIDGE_HOST_SOCKET_DIR=",
        "SERVERFS_AGENT_LOCK_HOST_DIR=",
        "SERVERFS_AGENT_BRIDGE_STATE_DIR=",
        "SERVERFS_CODEX_BIN=",
        "SERVERFS_CLAUDE_BIN=",
    ):
        assert key in text
    assert re.search(r"(?m)^SERVERFS_AGENT_PEER_UID=$", text)
    assert re.search(r"(?m)^SERVERFS_AGENT_PEER_GID=$", text)
    assert "single deployment configuration source" in text
    assert "measure_peercred.py" in text
    assert "provider.env" in text


def test_deployment_readme_uses_real_state_store_filename() -> None:
    text = (_REPO_ROOT / "deployment/agent-bridge/README.md").read_text(encoding="utf-8")
    assert "state.sqlite3" in text
    assert "bridge.sqlite3" not in text


def test_render_config_has_exactly_one_env_input() -> None:
    text = (_REPO_ROOT / "deployment/agent-bridge/render_config.py").read_text(encoding="utf-8")

    assert text.count('add_argument("--env-file"') == 1
    assert "def build_config(values: dict[str, str])" in text
    assert "values = load_env_file(args.env_file)" in text
    # The definition plus that one call site; a second parser would add a third.
    assert text.count("load_env_file(") == 2


def test_single_env_surface_is_complete() -> None:
    assert (_REPO_ROOT / ".env.example").is_file()
    assert not (_REPO_ROOT / ".env.agent.example").exists()

    overlay = (_REPO_ROOT / "compose.agent.yml").read_text(encoding="utf-8")
    assert overlay.count("--env-file") == 1


def test_documented_deployment_entrypoints_are_executable() -> None:
    # The README invokes these two directly as `deployment/agent-bridge/<name>`
    # rather than through `bash`, and git records the executable bit, so a
    # checkout without it fails with "Permission denied" on the documented
    # command. The .py helpers are documented as `python3 <path>` and need no bit.
    for name in ("install.sh", "rollback_app.sh"):
        path = _REPO_ROOT / "deployment" / "agent-bridge" / name
        assert os.access(path, os.X_OK), f"{name} must be executable"


# ---------------------------------------------------------------------------
# Transactional recovery, exercised through real subprocess runs.
#
# The installer and rollback script are only reachable as executables, so these
# drive `bash` against a temporary HOME, a fake `systemctl` and a fake `uv`. The
# fake systemd keeps enabled/active as files, which makes "the previous
# deployment state was restored" an assertion instead of a log reading. No real
# user service, deployment tree or Docker stack is touched.
# ---------------------------------------------------------------------------

_FAKE_SYSTEMCTL = """#!/usr/bin/env bash
set -u
log="${FAKE_SYSTEMCTL_LOG:?}"
state="${FAKE_SYSTEMCTL_STATE:?}"
printf '%s\\n' "$*" >>"$log"

if [[ "${1:-}" != "--user" ]]; then
  echo "fake systemctl: only --user is supported" >&2
  exit 1
fi
shift

enabled=false
active=false
[[ -f "$state/enabled" ]] && enabled=true
[[ -f "$state/active" ]] && active=true

command="${1:-}"
shift || true

case "$command" in
  show-environment | daemon-reload)
    exit 0
    ;;
  is-enabled)
    [[ "$enabled" == "true" ]] || exit 1
    exit 0
    ;;
  is-active)
    [[ "$active" == "true" ]] || exit 1
    exit 0
    ;;
  enable)
    if [[ "${1:-}" == "--now" ]]; then
      if [[ -n "${FAKE_SYSTEMCTL_FAIL_ENABLE_NOW:-}" ]]; then
        exit 1
      fi
      : >"$state/enabled"
      : >"$state/active"
      exit 0
    fi
    if [[ -n "${FAKE_SYSTEMCTL_FAIL_ENABLE:-}" ]]; then
      exit 1
    fi
    : >"$state/enabled"
    ;;
  disable)
    rm -f "$state/enabled"
    ;;
  stop)
    rm -f "$state/active"
    ;;
  start)
    if [[ -n "${FAKE_SYSTEMCTL_FAIL_FIRST_START:-}" && ! -f "$state/.start_attempted" ]]; then
      : >"$state/.start_attempted"
      exit 1
    fi
    : >"$state/active"
    ;;
esac
exit 0
"""

_FAKE_UV = """#!/usr/bin/env bash
set -euo pipefail
mkdir -p .venv/bin
for name in python serverfs-agent-bridge; do
  cat >".venv/bin/$name" <<'INNER'
#!/usr/bin/env bash
exit 0
INNER
  chmod 755 ".venv/bin/$name"
done
exit 0
"""


def _write_script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _seed_release(releases: Path, name: str) -> Path:
    release = releases / name
    _write_script(release / ".venv/bin/python", "#!/usr/bin/env bash\nexit 0\n")
    _write_script(release / ".venv/bin/serverfs-agent-bridge", "#!/usr/bin/env bash\nexit 0\n")
    return release


def _fake_deployment_harness(
    tmp_path: Path,
    *,
    fail_enable_now: bool = False,
    fail_first_start: bool = False,
) -> dict[str, object]:
    home = tmp_path / "home"
    (home / ".config/serverfs-agent-bridge").mkdir(parents=True)
    unit_dir = home / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    data_root = home / ".local/share/serverfs-agent-bridge"
    releases = data_root / "releases"
    releases.mkdir(parents=True)

    state = tmp_path / "systemctl-state"
    state.mkdir()
    log = tmp_path / "systemctl.log"
    log.write_text("", encoding="utf-8")

    fake_bin = tmp_path / "fake-bin"
    _write_script(fake_bin / "systemctl", _FAKE_SYSTEMCTL)

    environment = {
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_SYSTEMCTL_LOG": str(log),
        "FAKE_SYSTEMCTL_STATE": str(state),
    }
    if fail_enable_now:
        environment["FAKE_SYSTEMCTL_FAIL_ENABLE_NOW"] = "1"
    if fail_first_start:
        environment["FAKE_SYSTEMCTL_FAIL_FIRST_START"] = "1"

    return {
        "home": home,
        "data_root": data_root,
        "releases": releases,
        "config": home / ".config/serverfs-agent-bridge/config.json",
        "unit": unit_dir / "serverfs-agent-bridge.service",
        "state": state,
        "log": log,
        "env": environment,
        "uv_bin": _write_script(fake_bin / "uv", _FAKE_UV),
    }


def _set_service_state(harness: dict[str, object], *, enabled: bool, active: bool) -> None:
    state = harness["state"]
    assert isinstance(state, Path)
    for name, value in (("enabled", enabled), ("active", active)):
        marker = state / name
        if value:
            marker.touch()
        else:
            marker.unlink(missing_ok=True)


def _service_state(harness: dict[str, object]) -> tuple[bool, bool]:
    state = harness["state"]
    assert isinstance(state, Path)
    return (state / "enabled").exists(), (state / "active").exists()


def _deployment_env_file(tmp_path: Path) -> Path:
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    codex = make_executable(tmp_path / "codex")
    runtime = tmp_path / "runtime"
    env_file = tmp_path / "serverfs.env"
    env_file.write_text(
        "\n".join(
            [
                f"SERVERFS_UID={os.getuid()}",
                f"SERVERFS_GID={os.getgid()}",
                "SERVERFS_AGENT_BRIDGE_ENABLED=true",
                f"SERVERFS_AGENT_PEER_UID={os.getuid()}",
                f"SERVERFS_AGENT_PEER_GID={os.getgid()}",
                f"SERVERFS_AGENT_BRIDGE_HOST_SOCKET_DIR={runtime}/socket",
                f"SERVERFS_AGENT_LOCK_HOST_DIR={runtime}/locks",
                f"SERVERFS_AGENT_BRIDGE_STATE_DIR={tmp_path}/state",
                f"SERVERFS_CODEX_BIN={codex}",
                "WORKDIR_01_ALIAS=e2e",
                f"WORKDIR_01_PATH={workdir}",
                "WORKDIR_01_READ_ONLY=false",
                "WORKDIR_01_AGENT_MODE=workspace-write",
                "WORKDIR_01_AGENT_RUNTIMES=codex",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return env_file


def _run_deployment_script(
    harness: dict[str, object],
    argv: list[str],
) -> subprocess.CompletedProcess[str]:
    environment = harness["env"]
    assert isinstance(environment, dict)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(_REPO_ROOT),
        timeout=120,
    )


def test_installer_activation_failure_restores_previous_deployment(tmp_path: Path) -> None:
    harness = _fake_deployment_harness(tmp_path, fail_enable_now=True)
    old_release = _seed_release(harness["releases"], "A")
    previous_release = _seed_release(harness["releases"], "B")
    data_root = harness["data_root"]
    assert isinstance(data_root, Path)
    (data_root / "current").symlink_to(old_release)
    (data_root / "previous").symlink_to(previous_release)
    config = harness["config"]
    unit = harness["unit"]
    assert isinstance(config, Path) and isinstance(unit, Path)
    config.write_text("OLD-CONFIG\n", encoding="utf-8")
    unit.write_text("OLD-UNIT\n", encoding="utf-8")
    _set_service_state(harness, enabled=True, active=True)

    result = _run_deployment_script(
        harness,
        [
            "bash",
            str(_REPO_ROOT / "deployment/agent-bridge/install.sh"),
            "--env-file",
            str(_deployment_env_file(tmp_path)),
            "--uv-bin",
            str(harness["uv_bin"]),
        ],
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert (data_root / "current").resolve() == old_release.resolve()
    assert (data_root / "previous").resolve() == previous_release.resolve()
    assert config.read_text(encoding="utf-8") == "OLD-CONFIG\n"
    assert unit.read_text(encoding="utf-8") == "OLD-UNIT\n"
    assert _service_state(harness) == (True, True)
    releases = harness["releases"]
    assert isinstance(releases, Path)
    assert sorted(path.name for path in releases.iterdir()) == ["A", "B"]


def test_installer_first_run_failure_leaves_no_previous_deployment(tmp_path: Path) -> None:
    harness = _fake_deployment_harness(tmp_path, fail_enable_now=True)
    _set_service_state(harness, enabled=False, active=False)

    result = _run_deployment_script(
        harness,
        [
            "bash",
            str(_REPO_ROOT / "deployment/agent-bridge/install.sh"),
            "--env-file",
            str(_deployment_env_file(tmp_path)),
            "--uv-bin",
            str(harness["uv_bin"]),
        ],
    )

    assert result.returncode != 0, result.stdout + result.stderr
    data_root = harness["data_root"]
    config = harness["config"]
    unit = harness["unit"]
    releases = harness["releases"]
    assert isinstance(data_root, Path)
    assert isinstance(config, Path) and isinstance(unit, Path)
    assert isinstance(releases, Path)
    assert not os.path.lexists(data_root / "current")
    assert not os.path.lexists(data_root / "previous")
    assert not os.path.lexists(config)
    assert not os.path.lexists(unit)
    assert _service_state(harness) == (False, False)
    assert list(releases.iterdir()) == []


def test_rollback_restart_failure_restores_original_links(tmp_path: Path) -> None:
    harness = _fake_deployment_harness(tmp_path, fail_first_start=True)
    release_a = _seed_release(harness["releases"], "A")
    release_b = _seed_release(harness["releases"], "B")
    data_root = harness["data_root"]
    assert isinstance(data_root, Path)
    (data_root / "current").symlink_to(release_a)
    (data_root / "previous").symlink_to(release_b)
    _set_service_state(harness, enabled=True, active=True)

    result = _run_deployment_script(
        harness,
        ["bash", str(_REPO_ROOT / "deployment/agent-bridge/rollback_app.sh")],
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert (data_root / "current").resolve() == release_a.resolve()
    assert (data_root / "previous").resolve() == release_b.resolve()
    assert _service_state(harness) == (True, True)
    # The first `systemctl --user start` was rejected, the recovery retry was not.
    state = harness["state"]
    assert isinstance(state, Path)
    assert (state / ".start_attempted").exists()
    leftovers = [path.name for path in data_root.iterdir() if path.name.startswith(".")]
    assert leftovers == []


def test_normal_update_moves_current_to_previous(tmp_path: Path) -> None:
    harness = _fake_deployment_harness(tmp_path)
    old_release = _seed_release(harness["releases"], "A")
    data_root = harness["data_root"]
    assert isinstance(data_root, Path)
    (data_root / "current").symlink_to(old_release)
    _set_service_state(harness, enabled=True, active=True)

    result = _run_deployment_script(
        harness,
        [
            "bash",
            str(_REPO_ROOT / "deployment/agent-bridge/install.sh"),
            "--env-file",
            str(_deployment_env_file(tmp_path)),
            "--uv-bin",
            str(harness["uv_bin"]),
        ],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    releases = harness["releases"]
    assert isinstance(releases, Path)
    new_release = (data_root / "current").resolve()
    assert new_release != old_release.resolve()
    assert new_release.parent == releases
    assert (data_root / "previous").resolve() == old_release.resolve()
    assert _service_state(harness) == (True, True)


def test_no_start_stages_update_without_stopping_active_service(tmp_path: Path) -> None:
    harness = _fake_deployment_harness(tmp_path)
    old_release = _seed_release(harness["releases"], "A")
    data_root = harness["data_root"]
    assert isinstance(data_root, Path)
    (data_root / "current").symlink_to(old_release)
    _set_service_state(harness, enabled=True, active=True)

    result = _run_deployment_script(
        harness,
        [
            "bash",
            str(_REPO_ROOT / "deployment/agent-bridge/install.sh"),
            "--env-file",
            str(_deployment_env_file(tmp_path)),
            "--uv-bin",
            str(harness["uv_bin"]),
            "--no-start",
        ],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (data_root / "current").resolve() != old_release.resolve()
    assert (data_root / "previous").resolve() == old_release.resolve()
    assert _service_state(harness) == (True, True)
    assert "existing Bridge process was left running" in result.stdout

    log = harness["log"]
    assert isinstance(log, Path)
    commands = log.read_text(encoding="utf-8").splitlines()
    for lifecycle in ("stop", "start", "enable", "disable"):
        assert not any(line.startswith(f"--user {lifecycle} ") for line in commands)


def test_normal_rollback_swaps_current_and_previous(tmp_path: Path) -> None:
    harness = _fake_deployment_harness(tmp_path)
    release_a = _seed_release(harness["releases"], "A")
    release_b = _seed_release(harness["releases"], "B")
    data_root = harness["data_root"]
    assert isinstance(data_root, Path)
    (data_root / "current").symlink_to(release_a)
    (data_root / "previous").symlink_to(release_b)
    _set_service_state(harness, enabled=True, active=True)

    result = _run_deployment_script(
        harness,
        ["bash", str(_REPO_ROOT / "deployment/agent-bridge/rollback_app.sh")],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (data_root / "current").resolve() == release_b.resolve()
    assert (data_root / "previous").resolve() == release_a.resolve()
    assert _service_state(harness) == (True, True)
    leftovers = [path.name for path in data_root.iterdir() if path.name.startswith(".")]
    assert leftovers == []
