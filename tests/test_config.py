"""Config parsing tests."""

from __future__ import annotations

import pytest

from serverfs_mcp.config import Settings, settings_from_env


class TestDefaults:
    def test_empty_env_gives_defaults(self) -> None:
        s = settings_from_env({})
        assert s == Settings()

    def test_partial_env(self) -> None:
        s = settings_from_env({"SERVERFS_MAX_READ_LINES": "42", "SERVERFS_ALLOW_HIDDEN": "true"})
        assert s.max_read_lines == 42
        assert s.allow_hidden is True


class TestRobustParsing:
    def test_invalid_policy_int_fails_closed(self) -> None:
        with pytest.raises(ValueError):
            settings_from_env({"SERVERFS_MAX_READ_BYTES": "not-a-number"})

    def test_nonpositive_policy_int_fails_closed(self) -> None:
        with pytest.raises(ValueError):
            settings_from_env({"SERVERFS_MAX_READ_LINES": "0"})

    @pytest.mark.parametrize("key", ["SERVERFS_ALLOW_HIDDEN", "SERVERFS_DISABLE_DEFAULT_DENY"])
    def test_invalid_policy_bool_fails_closed(self, key: str) -> None:
        with pytest.raises(ValueError):
            settings_from_env({key: "maybe"})

    def test_bool_variants(self) -> None:
        assert settings_from_env({"SERVERFS_ALLOW_HIDDEN": "1"}).allow_hidden is True
        assert settings_from_env({"SERVERFS_ALLOW_HIDDEN": "Yes"}).allow_hidden is True
        assert settings_from_env({"SERVERFS_ALLOW_HIDDEN": "false"}).allow_hidden is False
        assert settings_from_env({"SERVERFS_ALLOW_HIDDEN": ""}).allow_hidden is False

    def test_log_level_normalization(self) -> None:
        assert settings_from_env({"SERVERFS_LOG_LEVEL": "debug"}).log_level == "DEBUG"
        assert settings_from_env({"SERVERFS_LOG_LEVEL": ""}).log_level == "INFO"
        assert settings_from_env({"SERVERFS_LOG_LEVEL": "bogus"}).log_level == "INFO"


class TestAgentBridgeConfig:
    def test_defaults_are_disabled(self) -> None:
        s = settings_from_env({})
        assert s.agent_bridge_enabled is False
        assert s.agent_bridge_socket == "/run/serverfs-agent-bridge/bridge.sock"
        assert s.agent_bridge_timeout_seconds == 30.0
        assert s.agent_lock_dir == "/run/serverfs-agent-locks"

    @pytest.mark.parametrize(
        "env",
        [
            {"SERVERFS_AGENT_MODE": "disabled", "SERVERFS_AGENT_RUNTIMES": "codex"},
            {"SERVERFS_AGENT_MODE": "review", "SERVERFS_AGENT_RUNTIMES": ""},
        ],
    )
    def test_invalid_global_agent_pair_fails_closed(self, env: dict[str, str]) -> None:
        with pytest.raises(ValueError):
            settings_from_env(env)

    def test_explicit_agent_bridge_settings(self) -> None:
        s = settings_from_env(
            {
                "SERVERFS_AGENT_BRIDGE_ENABLED": "true",
                "SERVERFS_AGENT_BRIDGE_SOCKET": "/tmp/bridge.sock",
                "SERVERFS_AGENT_BRIDGE_TIMEOUT_SECONDS": "7.5",
                "SERVERFS_AGENT_LOCK_DIR": "/tmp/locks",
            }
        )
        assert s.agent_bridge_enabled is True
        assert s.agent_bridge_socket == "/tmp/bridge.sock"
        assert s.agent_bridge_timeout_seconds == 7.5
        assert s.agent_lock_dir == "/tmp/locks"

    def test_invalid_enable_value_fails_closed(self) -> None:
        assert (
            settings_from_env({"SERVERFS_AGENT_BRIDGE_ENABLED": "definitely"}).agent_bridge_enabled
            is False
        )


class TestDenyConfig:
    def test_extra_deny_globs_default_empty(self) -> None:
        assert settings_from_env({}).extra_deny_globs == ()
        assert settings_from_env({"SERVERFS_EXTRA_DENY_GLOBS": ""}).extra_deny_globs == ()
        assert settings_from_env({"SERVERFS_EXTRA_DENY_GLOBS": " , ,"}).extra_deny_globs == ()

    def test_extra_deny_globs_parsed(self) -> None:
        s = settings_from_env({"SERVERFS_EXTRA_DENY_GLOBS": "*.sqlite, backup_*, secrets_*.json"})
        assert s.extra_deny_globs == ("*.sqlite", "backup_*", "secrets_*.json")

    def test_disable_default_deny_default_false(self) -> None:
        assert settings_from_env({}).disable_default_deny is False
        assert (
            settings_from_env({"SERVERFS_DISABLE_DEFAULT_DENY": ""}).disable_default_deny is False
        )

    def test_disable_default_deny_true(self) -> None:
        assert (
            settings_from_env({"SERVERFS_DISABLE_DEFAULT_DENY": "true"}).disable_default_deny
            is True
        )
        assert (
            settings_from_env({"SERVERFS_DISABLE_DEFAULT_DENY": "1"}).disable_default_deny is True
        )
