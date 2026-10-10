from __future__ import annotations

import json
from pathlib import Path

import pytest

from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.models import AgentMode


def write_config(tmp_path: Path, workdir: Path, **overrides) -> Path:
    data = {
        "socket_path": str(tmp_path / "run" / "bridge.sock"),
        "state_dir": str(tmp_path / "state"),
        "lock_dir": str(tmp_path / "locks"),
        "enable_fake_runtime": True,
        "workdirs": [
            {
                "slot": 1,
                "alias": "repo",
                "host_path": str(workdir),
                "read_only": True,
                "agent_mode": "review",
                "agent_runtimes": ["fake"],
            }
        ],
    }
    data.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_load_review_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = BridgeConfig.load(write_config(tmp_path, repo))
    policy = config.policies.get("repo")
    assert policy.mode is AgentMode.REVIEW
    assert policy.read_only is True
    assert policy.runtimes == frozenset({"fake"})
    assert config.qoder.enabled is False
    assert config.jev.enabled is False
    assert config.jev.api_key is None
    assert config.limits.task_timeout_seconds == 7200
    assert config.limits.interaction_timeout_seconds == 1800
    assert config.limits.max_active_tasks == 4
    assert config.limits.retention_seconds == 168 * 60 * 60
    assert config.limits.result_spool_threshold_bytes == 262_144
    assert config.proxy is None


def test_lifecycle_limits_are_strict_positive_integers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    valid = {
        "task_timeout_seconds": 60,
        "interaction_timeout_seconds": 30,
        "max_active_tasks": 2,
        "retention_seconds": 3600,
    }
    config = BridgeConfig.load(write_config(tmp_path, repo, limits=valid))
    assert config.limits.task_timeout_seconds == 60
    assert config.limits.interaction_timeout_seconds == 30
    assert config.limits.max_active_tasks == 2
    assert config.limits.retention_seconds == 3600
    assert config.limits.result_spool_threshold_bytes == 262_144

    for key in valid:
        invalid = dict(valid)
        invalid[key] = 0
        with pytest.raises(ValueError, match=key):
            BridgeConfig.load(write_config(tmp_path, repo, limits=invalid))


def test_proxy_config_supports_jev_auth_and_rejects_agent_auth(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    proxy = {"url": "http://user:secret@proxy.internal:7890", "authenticated": True}
    config = BridgeConfig.load(
        write_config(
            tmp_path,
            repo,
            proxy=proxy,
            jev={"api_key": "jev-test-secret-123", "use_proxy": True},
        )
    )
    assert config.jev.use_proxy is True
    assert config.proxy is not None
    assert config.proxy.authenticated is True
    assert "secret" not in repr(config.proxy)

    with pytest.raises(ValueError, match="credentialless"):
        BridgeConfig.load(
            write_config(
                tmp_path,
                repo,
                proxy=proxy,
                codex={"enabled": False, "use_proxy": True},
            )
        )


def test_result_spool_threshold_is_bounded_by_spool_capacity(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = BridgeConfig.load(
        write_config(tmp_path, repo, limits={"result_spool_threshold_bytes": 2048})
    )
    assert config.limits.result_spool_threshold_bytes == 2048

    with pytest.raises(ValueError, match="must not exceed"):
        BridgeConfig.load(
            write_config(
                tmp_path,
                repo,
                limits={"result_spool_threshold_bytes": 8 * 1024 * 1024 + 1},
            )
        )


def test_jev_config_is_opt_in_and_secret_repr_is_redacted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = "jev-test-secret-123"
    config = BridgeConfig.load(write_config(tmp_path, repo, jev={"api_key": secret}))

    assert config.jev.enabled is True
    assert config.jev.api_key == secret
    assert secret not in repr(config)
    assert secret not in repr(config.jev)


@pytest.mark.parametrize("value", ["has whitespace", "bad\nkey", 123])
def test_jev_api_key_rejects_invalid_values(tmp_path: Path, value: object) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="jev.api_key"):
        BridgeConfig.load(write_config(tmp_path, repo, jev={"api_key": value}))


def test_workspace_write_plus_read_only_fails_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = write_config(tmp_path, repo)
    data = json.loads(path.read_text())
    data["workdirs"][0]["agent_mode"] = "workspace-write"
    path.write_text(json.dumps(data))

    with pytest.raises(ValueError, match="requires a writable"):
        BridgeConfig.load(path)


def test_unknown_agent_mode_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = write_config(tmp_path, repo)
    data = json.loads(path.read_text())
    data["workdirs"][0]["agent_mode"] = "unrestricted"
    path.write_text(json.dumps(data))

    with pytest.raises(ValueError):
        BridgeConfig.load(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("read_only", "false"),
        ("slot", "1"),
        ("agent_runtimes", "fake"),
    ],
)
def test_security_fields_do_not_coerce_json_values(
    tmp_path: Path, field: str, value: object
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = write_config(tmp_path, repo)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["workdirs"][0][field] = value
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError):
        BridgeConfig.load(path)


def test_unknown_runtime_and_unknown_config_field_fail(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = write_config(tmp_path, repo)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["workdirs"][0]["agent_runtimes"] = ["not-a-runtime"]
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown agent runtime"):
        BridgeConfig.load(path)

    data = json.loads(path.read_text(encoding="utf-8"))
    data["unexpected"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown bridge config field"):
        BridgeConfig.load(path)


def test_missing_or_non_directory_host_path_fails(tmp_path: Path) -> None:
    path = write_config(tmp_path, tmp_path / "missing")
    with pytest.raises(ValueError, match="host_path must exist"):
        BridgeConfig.load(path)

    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    path = write_config(tmp_path, file_path)
    with pytest.raises(ValueError, match="host_path must be a directory"):
        BridgeConfig.load(path)


def test_codex_config_is_strict_and_fail_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    path = write_config(
        tmp_path,
        repo,
        codex={
            "enabled": True,
            "autostart": False,
            "codex_home": str(codex_home),
            "codex_bin": "codex",
            "request_timeout_seconds": 5,
            "event_idle_timeout_seconds": 60,
            "max_message_bytes": 4096,
        },
    )
    config = BridgeConfig.load(path)
    assert config.codex.enabled is True
    assert config.codex.control_socket == (
        codex_home / "app-server-control" / "app-server-control.sock"
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    data["codex"]["autostart"] = "false"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON boolean"):
        BridgeConfig.load(path)

    data["codex"]["autostart"] = False
    data["codex"]["unexpected"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown codex field"):
        BridgeConfig.load(path)


def test_claude_config_is_strict_and_fail_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = write_config(
        tmp_path,
        repo,
        claude={
            "enabled": True,
            "claude_bin": "claude",
            "probe_timeout_seconds": 5,
            "event_idle_timeout_seconds": 60,
        },
    )
    config = BridgeConfig.load(path)
    assert config.claude.enabled is True
    assert config.claude.claude_bin == "claude"

    data = json.loads(path.read_text(encoding="utf-8"))
    data["claude"]["enabled"] = "true"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON boolean"):
        BridgeConfig.load(path)

    data["claude"]["enabled"] = True
    data["claude"]["claude_bin"] = "claude\n--danger"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid character"):
        BridgeConfig.load(path)

    data["claude"]["claude_bin"] = "claude"
    data["claude"]["unexpected"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown claude field"):
        BridgeConfig.load(path)


def test_claude_allowlist_requires_enabled_runtime_and_workspace_write(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = write_config(tmp_path, repo, enable_fake_runtime=False)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["workdirs"][0]["agent_runtimes"] = ["claude"]
    data["workdirs"][0]["agent_mode"] = "review"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="claude.enabled is false"):
        BridgeConfig.load(path)

    data["claude"] = {"enabled": True}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="requires agent_mode=workspace-write"):
        BridgeConfig.load(path)

    data["workdirs"][0]["agent_mode"] = "workspace-write"
    data["workdirs"][0]["read_only"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    config = BridgeConfig.load(path)
    assert config.policies.get("repo").mode is AgentMode.WORKSPACE_WRITE


def test_qoder_config_is_strict_and_fail_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = write_config(
        tmp_path,
        repo,
        qoder={
            "enabled": True,
            "qoder_bin": "qodercli",
            "probe_timeout_seconds": 5,
            "event_idle_timeout_seconds": 60,
        },
    )
    config = BridgeConfig.load(path)
    assert config.qoder.enabled is True
    assert config.qoder.qoder_bin == "qodercli"

    data = json.loads(path.read_text(encoding="utf-8"))
    data["qoder"]["enabled"] = "true"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON boolean"):
        BridgeConfig.load(path)

    data["qoder"]["enabled"] = True
    data["qoder"]["qoder_bin"] = "qodercli\n--danger"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid character"):
        BridgeConfig.load(path)

    data["qoder"]["qoder_bin"] = "qodercli"
    data["qoder"]["unexpected"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown qoder field"):
        BridgeConfig.load(path)


def test_qoder_allowlist_requires_enabled_runtime_and_workspace_write(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = write_config(tmp_path, repo, enable_fake_runtime=False)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["workdirs"][0]["agent_runtimes"] = ["qoder"]
    data["workdirs"][0]["agent_mode"] = "review"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="qoder.enabled is false"):
        BridgeConfig.load(path)

    data["qoder"] = {"enabled": True}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="requires agent_mode=workspace-write"):
        BridgeConfig.load(path)

    data["workdirs"][0]["agent_mode"] = "workspace-write"
    data["workdirs"][0]["read_only"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    config = BridgeConfig.load(path)
    assert config.policies.get("repo").mode is AgentMode.WORKSPACE_WRITE


def test_codex_allowlist_requires_enabled_runtime_and_workspace_write(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    path = write_config(tmp_path, repo, enable_fake_runtime=False)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["workdirs"][0]["agent_runtimes"] = ["codex"]
    data["workdirs"][0]["agent_mode"] = "review"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="codex.enabled is false"):
        BridgeConfig.load(path)

    data["codex"] = {
        "enabled": True,
        "autostart": False,
        "codex_home": str(codex_home),
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="requires agent_mode=workspace-write"):
        BridgeConfig.load(path)

    data["workdirs"][0]["agent_mode"] = "workspace-write"
    data["workdirs"][0]["read_only"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    config = BridgeConfig.load(path)
    assert config.policies.get("repo").mode is AgentMode.WORKSPACE_WRITE


@pytest.mark.parametrize("field", ["allowed_peer_uid", "allowed_peer_gid"])
def test_peer_credentials_do_not_coerce_or_accept_negative_values(
    tmp_path: Path, field: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = write_config(tmp_path, repo)
    data = json.loads(path.read_text(encoding="utf-8"))
    data[field] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        BridgeConfig.load(path)

    data[field] = -1
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        BridgeConfig.load(path)
