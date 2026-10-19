"""Configuration tests for the optional serverfs-auth process."""

from __future__ import annotations

import pytest

from serverfs_mcp.auth_config import AuthServiceSettings, auth_settings_from_env


class TestAuthDefaults:
    def test_empty_env_is_disabled_and_default_deny(self) -> None:
        settings = auth_settings_from_env({})
        assert settings == AuthServiceSettings()
        assert settings.enabled_providers == ()
        assert settings.allowed_principals == ()
        assert settings.proxy is None

    def test_disabled_master_switch_does_not_require_provider_credentials(self) -> None:
        settings = auth_settings_from_env({"SERVERFS_OAUTH_GITHUB_ENABLED": "true"})
        assert settings.oauth_enabled is False
        assert settings.github_enabled is True
        assert settings.github_client_id == ""
        assert settings.github_client_secret == ""


class TestAuthEnabledValidation:
    @staticmethod
    def valid_env() -> dict[str, str]:
        return {
            "SERVERFS_OAUTH_ENABLED": "true",
            "SERVERFS_AUTH_PUBLIC_URL": "https://auth.example.com",
            "SERVERFS_OAUTH_INTROSPECTION_SECRET": "shared-secret",
            "SERVERFS_OAUTH_GITHUB_ENABLED": "true",
            "SERVERFS_OAUTH_GITHUB_CLIENT_ID": "client-id",
            "SERVERFS_OAUTH_GITHUB_CLIENT_SECRET": "client-secret",
        }

    def test_enabled_minimal_valid_configuration(self) -> None:
        settings = auth_settings_from_env(self.valid_env())
        assert settings.oauth_enabled is True
        assert settings.public_url == "https://auth.example.com"
        assert settings.enabled_providers == ("github",)
        assert settings.introspection_secret == "shared-secret"

    def test_loopback_http_public_url_is_allowed_for_tests(self) -> None:
        env = self.valid_env()
        env["SERVERFS_AUTH_PUBLIC_URL"] = "http://127.0.0.1:8082"
        assert auth_settings_from_env(env).public_url == "http://127.0.0.1:8082"

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("SERVERFS_AUTH_PUBLIC_URL", "http://auth.example.com"),
            ("SERVERFS_AUTH_PUBLIC_URL", "https://user@auth.example.com"),
            ("SERVERFS_AUTH_PUBLIC_URL", "https://auth.example.com?x=1"),
            ("SERVERFS_AUTH_PORT", "0"),
            ("SERVERFS_AUTH_PORT", "65536"),
            ("SERVERFS_AUTH_ACCESS_TOKEN_TTL_SECONDS", "0"),
            ("SERVERFS_AUTH_REFRESH_TOKEN_TTL_SECONDS", "never"),
        ],
    )
    def test_invalid_values_fail_closed(self, key: str, value: str) -> None:
        env = self.valid_env()
        env[key] = value
        with pytest.raises(ValueError):
            auth_settings_from_env(env)

    def test_enabled_requires_public_url(self) -> None:
        env = self.valid_env()
        env.pop("SERVERFS_AUTH_PUBLIC_URL")
        with pytest.raises(ValueError, match="SERVERFS_AUTH_PUBLIC_URL"):
            auth_settings_from_env(env)

    def test_enabled_requires_introspection_secret(self) -> None:
        env = self.valid_env()
        env.pop("SERVERFS_OAUTH_INTROSPECTION_SECRET")
        with pytest.raises(ValueError, match="SERVERFS_OAUTH_INTROSPECTION_SECRET"):
            auth_settings_from_env(env)

    def test_enabled_requires_at_least_one_provider(self) -> None:
        env = self.valid_env()
        env["SERVERFS_OAUTH_GITHUB_ENABLED"] = "false"
        with pytest.raises(ValueError, match="at least one enabled OAuth provider"):
            auth_settings_from_env(env)

    def test_enabled_provider_requires_credentials(self) -> None:
        env = self.valid_env()
        env.pop("SERVERFS_OAUTH_GITHUB_CLIENT_SECRET")
        with pytest.raises(ValueError, match="SERVERFS_OAUTH_GITHUB_ENABLED"):
            auth_settings_from_env(env)

    @pytest.mark.parametrize(
        "key",
        [
            "SERVERFS_OAUTH_ENABLED",
            "SERVERFS_AUTH_DCR_ENABLED",
            "SERVERFS_AUTH_USE_PROXY",
            "SERVERFS_OAUTH_GITHUB_ENABLED",
            "SERVERFS_OAUTH_DINGTALK_ENABLED",
            "SERVERFS_OAUTH_FEISHU_ENABLED",
            "SERVERFS_OAUTH_WECHAT_ENABLED",
        ],
    )
    def test_boolean_settings_are_strict(self, key: str) -> None:
        env = self.valid_env()
        env[key] = "maybe"
        with pytest.raises(ValueError):
            auth_settings_from_env(env)


class TestAdmissionConfig:
    def test_allowed_principals_are_exact_and_provider_qualified(self) -> None:
        settings = auth_settings_from_env(
            {"SERVERFS_AUTH_ALLOWED_PRINCIPALS": "github:123, feishu:ou_abc,wechat:openid123"}
        )
        assert settings.allowed_principals == (
            "github:123",
            "feishu:ou_abc",
            "wechat:openid123",
        )

    @pytest.mark.parametrize(
        "value",
        [
            "github:*",
            "github:",
            "unknown:123",
            "github:123,github:123",
            "github:user name",
        ],
    )
    def test_invalid_allowed_principal_fails_closed(self, value: str) -> None:
        with pytest.raises(ValueError):
            auth_settings_from_env({"SERVERFS_AUTH_ALLOWED_PRINCIPALS": value})


class TestAuthProxyConfig:
    def test_proxy_is_ignored_when_auth_proxy_is_disabled(self) -> None:
        settings = auth_settings_from_env({"SERVERFS_PROXY_PORT": "not-a-port"})
        assert settings.use_proxy is False
        assert settings.proxy is None

    def test_enabled_proxy_reuses_shared_serverfs_proxy_contract(self) -> None:
        settings = auth_settings_from_env(
            {
                "SERVERFS_AUTH_USE_PROXY": "true",
                "SERVERFS_PROXY_HOST": "proxy.example.com",
                "SERVERFS_PROXY_PORT": "8080",
                "SERVERFS_PROXY_USERNAME": "user",
                "SERVERFS_PROXY_PASSWORD": "pass",
            }
        )
        assert settings.proxy is not None
        assert settings.proxy.host == "proxy.example.com"
        assert settings.proxy.port == 8080
        assert settings.proxy.authenticated is True

    def test_enabled_proxy_requires_shared_proxy_host_and_port(self) -> None:
        with pytest.raises(ValueError, match="SERVERFS_AUTH_USE_PROXY"):
            auth_settings_from_env({"SERVERFS_AUTH_USE_PROXY": "true"})
