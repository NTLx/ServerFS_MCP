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


class TestHttpConfig:
    def test_defaults_preserve_openai_tunnel_internal_authority(self) -> None:
        s = settings_from_env({})
        assert s.http_host == "0.0.0.0"
        assert s.http_port == 8000
        assert s.http_path == "/mcp"
        assert s.http_allowed_hosts == ("serverfs-mcp:8000",)
        assert s.http_allowed_origins == ()
        assert s.http_api_key == ""

    def test_explicit_generic_http_settings(self) -> None:
        s = settings_from_env(
            {
                "SERVERFS_HTTP_HOST": "127.0.0.1",
                "SERVERFS_HTTP_PORT": "9000",
                "SERVERFS_HTTP_PATH": "/serverfs",
                "SERVERFS_HTTP_ALLOWED_HOSTS": "mcp.example.com, mcp.example.com:*",
                "SERVERFS_HTTP_ALLOWED_ORIGINS": "https://app.example.com,http://localhost:3000",
            }
        )
        assert s.http_host == "127.0.0.1"
        assert s.http_port == 9000
        assert s.http_path == "/serverfs"
        assert s.http_allowed_hosts == ("mcp.example.com", "mcp.example.com:*")
        assert s.http_allowed_origins == (
            "https://app.example.com",
            "http://localhost:3000",
        )

    def test_default_allowed_host_follows_configured_port(self) -> None:
        assert settings_from_env({"SERVERFS_HTTP_PORT": "9000"}).http_allowed_hosts == (
            "serverfs-mcp:9000",
        )

    def test_static_api_key_is_optional_and_strips_operator_whitespace(self) -> None:
        assert settings_from_env({"SERVERFS_HTTP_API_KEY": "  test-secret  "}).http_api_key == (
            "test-secret"
        )
        assert settings_from_env({"SERVERFS_HTTP_API_KEY": "   "}).http_api_key == ""

    def test_static_api_key_and_oauth_are_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="SERVERFS_HTTP_API_KEY"):
            settings_from_env(
                {
                    "SERVERFS_HTTP_API_KEY": "test-secret",
                    "SERVERFS_OAUTH_ENABLED": "true",
                }
            )

    @pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
    def test_invalid_port_fails_closed(self, value: str) -> None:
        with pytest.raises(ValueError):
            settings_from_env({"SERVERFS_HTTP_PORT": value})

    @pytest.mark.parametrize("value", ["mcp", "//mcp", "/mcp?x=1", "/mcp#fragment"])
    def test_invalid_path_fails_closed(self, value: str) -> None:
        with pytest.raises(ValueError):
            settings_from_env({"SERVERFS_HTTP_PATH": value})

    @pytest.mark.parametrize(
        "value",
        [
            "https://mcp.example.com",
            "mcp.example.com/path",
            "mcp example.com",
            "*.example.com",
            "mcp.*.example.com",
        ],
    )
    def test_invalid_allowed_host_fails_closed(self, value: str) -> None:
        with pytest.raises(ValueError):
            settings_from_env({"SERVERFS_HTTP_ALLOWED_HOSTS": value})

    @pytest.mark.parametrize(
        "value",
        ["mcp.example.com", "https://app.example.com/path", "https://user@app.example.com"],
    )
    def test_invalid_allowed_origin_fails_closed(self, value: str) -> None:
        with pytest.raises(ValueError):
            settings_from_env({"SERVERFS_HTTP_ALLOWED_ORIGINS": value})


class TestOAuthConfig:
    def test_defaults_are_disabled_and_v015_compatible(self) -> None:
        s = settings_from_env({})
        assert s.oauth_enabled is False
        assert s.oauth_resource_url == ""
        assert s.oauth_issuer_url == ""
        assert s.oauth_required_scopes == ("serverfs:access",)
        assert s.oauth_introspection_url == "http://serverfs-auth:8082/internal/introspect"
        assert s.oauth_introspection_secret == ""
        assert s.oauth_introspection_timeout_seconds == 2.0

    def test_enabled_resource_server_settings(self) -> None:
        s = settings_from_env(
            {
                "SERVERFS_OAUTH_ENABLED": "true",
                "SERVERFS_OAUTH_RESOURCE_URL": "https://mcp.example.com/mcp",
                "SERVERFS_OAUTH_ISSUER_URL": "https://auth.example.com",
                "SERVERFS_OAUTH_INTROSPECTION_SECRET": "shared-secret",
                "SERVERFS_OAUTH_REQUIRED_SCOPES": "serverfs:access,serverfs:test",
                "SERVERFS_OAUTH_INTROSPECTION_TIMEOUT_SECONDS": "1.5",
            }
        )
        assert s.oauth_enabled is True
        assert s.oauth_resource_url == "https://mcp.example.com/mcp"
        assert s.oauth_issuer_url == "https://auth.example.com"
        assert s.oauth_required_scopes == ("serverfs:access", "serverfs:test")
        assert s.oauth_introspection_secret == "shared-secret"
        assert s.oauth_introspection_timeout_seconds == 1.5

    def test_loopback_http_urls_are_allowed_for_tests(self) -> None:
        s = settings_from_env(
            {
                "SERVERFS_OAUTH_ENABLED": "true",
                "SERVERFS_OAUTH_RESOURCE_URL": "http://127.0.0.1:8000/mcp",
                "SERVERFS_OAUTH_ISSUER_URL": "http://localhost:8082",
                "SERVERFS_OAUTH_INTROSPECTION_SECRET": "shared-secret",
            }
        )
        assert s.oauth_enabled is True

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("SERVERFS_OAUTH_RESOURCE_URL", "http://mcp.example.com/mcp"),
            ("SERVERFS_OAUTH_ISSUER_URL", "http://auth.example.com"),
            ("SERVERFS_OAUTH_RESOURCE_URL", "https://user@mcp.example.com/mcp"),
            ("SERVERFS_OAUTH_ISSUER_URL", "https://auth.example.com?x=1"),
        ],
    )
    def test_invalid_public_oauth_url_fails_closed(self, key: str, value: str) -> None:
        env = {
            "SERVERFS_OAUTH_ENABLED": "true",
            "SERVERFS_OAUTH_RESOURCE_URL": "https://mcp.example.com/mcp",
            "SERVERFS_OAUTH_ISSUER_URL": "https://auth.example.com",
            "SERVERFS_OAUTH_INTROSPECTION_SECRET": "shared-secret",
        }
        env[key] = value
        with pytest.raises(ValueError):
            settings_from_env(env)

    @pytest.mark.parametrize(
        "missing",
        [
            "SERVERFS_OAUTH_RESOURCE_URL",
            "SERVERFS_OAUTH_ISSUER_URL",
            "SERVERFS_OAUTH_INTROSPECTION_SECRET",
        ],
    )
    def test_enabled_oauth_requires_resource_server_contract(self, missing: str) -> None:
        env = {
            "SERVERFS_OAUTH_ENABLED": "true",
            "SERVERFS_OAUTH_RESOURCE_URL": "https://mcp.example.com/mcp",
            "SERVERFS_OAUTH_ISSUER_URL": "https://auth.example.com",
            "SERVERFS_OAUTH_INTROSPECTION_SECRET": "shared-secret",
        }
        env.pop(missing)
        with pytest.raises(ValueError, match=missing):
            settings_from_env(env)

    def test_invalid_master_switch_fails_closed(self) -> None:
        with pytest.raises(ValueError):
            settings_from_env({"SERVERFS_OAUTH_ENABLED": "maybe"})

    @pytest.mark.parametrize("value", ["", "serverfs access", "serverfs:access,serverfs:access"])
    def test_invalid_required_scopes_fail_closed(self, value: str) -> None:
        with pytest.raises(ValueError):
            settings_from_env({"SERVERFS_OAUTH_REQUIRED_SCOPES": value})


class TestFileIngressConfig:
    def test_defaults_are_disabled(self) -> None:
        s = settings_from_env({})
        assert s.file_ingress_enabled is False
        assert s.file_ingress_timeout_seconds == 30.0

    def test_explicit_settings(self) -> None:
        s = settings_from_env(
            {
                "SERVERFS_FILE_INGRESS_ENABLED": "true",
                "SERVERFS_FILE_INGRESS_TIMEOUT_SECONDS": "7.5",
            }
        )
        assert s.file_ingress_enabled is True
        assert s.file_ingress_timeout_seconds == 7.5

    def test_invalid_enable_value_fails_closed(self) -> None:
        with pytest.raises(ValueError):
            settings_from_env({"SERVERFS_FILE_INGRESS_ENABLED": "maybe"})


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
