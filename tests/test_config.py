"""Config parsing tests."""

from __future__ import annotations

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
    def test_invalid_int_falls_back(self) -> None:
        s = settings_from_env({"SERVERFS_MAX_READ_BYTES": "not-a-number"})
        assert s.max_read_bytes == Settings().max_read_bytes

    def test_nonpositive_int_falls_back(self) -> None:
        s = settings_from_env({"SERVERFS_MAX_READ_LINES": "0"})
        assert s.max_read_lines == Settings().max_read_lines

    def test_bool_variants(self) -> None:
        assert settings_from_env({"SERVERFS_ALLOW_HIDDEN": "1"}).allow_hidden is True
        assert settings_from_env({"SERVERFS_ALLOW_HIDDEN": "Yes"}).allow_hidden is True
        assert settings_from_env({"SERVERFS_ALLOW_HIDDEN": "false"}).allow_hidden is False
        assert settings_from_env({"SERVERFS_ALLOW_HIDDEN": ""}).allow_hidden is False

    def test_log_level_normalization(self) -> None:
        assert settings_from_env({"SERVERFS_LOG_LEVEL": "debug"}).log_level == "DEBUG"
        assert settings_from_env({"SERVERFS_LOG_LEVEL": ""}).log_level == "INFO"
        assert settings_from_env({"SERVERFS_LOG_LEVEL": "bogus"}).log_level == "INFO"


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
