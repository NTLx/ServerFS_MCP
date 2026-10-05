"""Phase D tests for the native ``[agent]`` model (§7, §7.1, §9).

The load-bearing rule in this file is the **upgrade gate**: a v0.10 config — one with no
``[agent]`` section at all — must parse to exactly the filesystem-only result it produced before
Phase D. Everything else here is startup validation, because ``serverfs.toml`` is operator trust
input: a contradiction must abort startup rather than surface later as a request-time surprise.

Two rules are asserted as *absences* rather than as error messages:

- an enabled runtime that no workdir allowlists is **not** an error. Staging a runtime before
  assigning it is legitimate, and no frozen contract requires usage.
- the proxy endpoint is never present in the parsed model at all. It arrives through the
  ``SERVERFS_AGENT_PROXY_URL`` namespace at runtime (§7.1), so a model that could hold it would be
  a model that could write a secret into ``serverfs.toml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp.native_config import (
    MANDATORY_NO_PROXY,
    NativeAgentSettings,
    NativeConfigError,
    load_native_config,
)
from serverfs_mcp.workdirs import (
    AGENT_MODE_DISABLED,
    AGENT_MODE_REVIEW,
    AGENT_MODE_WORKSPACE_WRITE,
)

V010_CONFIG = """\
[server]
log_level = "INFO"

[[workdirs]]
alias = "projects"
path = "/srv/projects"
read_only = false
"""


def _write(tmp_path: Path, content: str) -> Path:
    config = tmp_path / "serverfs.toml"
    config.write_text(content, encoding="utf-8")
    return config


#: A minimal valid workdir body. Every config needs at least one, so the ``[agent]``-focused
#: helpers below append this rather than each restating it.
_MINIMAL_WORKDIR = '\n[[workdirs]]\nalias = "p"\npath = "/srv/p"\nread_only = true\n'


def _agent(tmp_path: Path, body: str, workdir: str = "") -> NativeAgentSettings:
    config = _write(tmp_path, f"[agent]\n{body}\n{workdir}{_MINIMAL_WORKDIR}")
    _workdirs, settings = load_native_config(config)
    assert settings.agent is not None
    return settings.agent


class TestV010UpgradeGate:
    """An absent or disabled [agent] must leave the v0.10 surface untouched."""

    def test_absent_agent_section_is_the_v010_result(self, tmp_path: Path) -> None:
        workdirs, settings = load_native_config(_write(tmp_path, V010_CONFIG))
        assert settings.agent is None
        assert settings.agent_enabled is False
        # The workdir model is byte-for-byte what v0.10 produced: no Agent policy anywhere.
        assert [(w.agent_mode, w.agent_runtimes) for w in workdirs] == [
            (AGENT_MODE_DISABLED, frozenset())
        ]

    def test_explicitly_disabled_agent_is_the_v010_result(self, tmp_path: Path) -> None:
        workdirs, settings = load_native_config(
            _write(tmp_path, f"{V010_CONFIG}\n[agent]\nenabled = false\n")
        )
        assert settings.agent is not None
        assert settings.agent.enabled is False
        assert settings.agent_enabled is False
        assert [(w.agent_mode, w.agent_runtimes) for w in workdirs] == [
            (AGENT_MODE_DISABLED, frozenset())
        ]

    def test_disabled_agent_defaults_every_runtime_off(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, "enabled = false")
        assert agent.enabled_runtimes == frozenset()
        assert agent.proxy.enabled is False

    def test_disabled_agent_refuses_workdir_agent_policy(self, tmp_path: Path) -> None:
        """Configuring authorization that nothing will honour is a contradiction, not a no-op."""
        config = _write(
            tmp_path,
            f"{V010_CONFIG}\n[agent]\nenabled = false\n"
            '[[workdirs]]\nalias = "other"\npath = "/srv/other"\n'
            'read_only = false\nagent_mode = "review"\n',
        )
        with pytest.raises(NativeConfigError, match="not enabled"):
            load_native_config(config)


class TestAgentDefaults:
    """Defaults are the Bridge's own LifecycleLimits, written once."""

    def test_lifecycle_defaults_match_the_bridge(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, "enabled = true")
        assert (agent.task_timeout_seconds, agent.interaction_timeout_seconds) == (7200, 1800)
        assert (agent.max_active_tasks, agent.retention_seconds) == (4, 168 * 60 * 60)

    def test_per_runtime_proxy_defaults_are_the_measured_ones(self, tmp_path: Path) -> None:
        """Phase 0F: Codex needs the proxy on WorkPC; Qoder is direct; Claude is unmeasured."""
        agent = _agent(tmp_path, "enabled = true")
        assert agent.codex.use_proxy is True
        assert agent.qoder.use_proxy is False
        assert agent.claude.use_proxy is False

    def test_runtime_binaries_default_to_the_bridge_names(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, "enabled = true")
        assert (agent.codex.binary, agent.claude.binary, agent.qoder.binary) == (
            "codex",
            "claude",
            "qodercli",
        )

    def test_enabled_runtimes_reflects_only_enabled_ones(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, "enabled = true\n\n[agent.codex]\nenabled = true\n")
        assert agent.enabled_runtimes == frozenset({"codex"})

    def test_lifecycle_timeouts_are_configurable(self, tmp_path: Path) -> None:
        agent = _agent(
            tmp_path,
            "enabled = true\ntask_timeout_seconds = 60\nmax_active_tasks = 1\n"
            "retention_seconds = 3600\ninteraction_timeout_seconds = 30",
        )
        assert agent.task_timeout_seconds == 60
        assert agent.max_active_tasks == 1
        assert agent.retention_seconds == 3600
        assert agent.interaction_timeout_seconds == 30

    @pytest.mark.parametrize(
        "key", ["task_timeout_seconds", "max_active_tasks", "retention_seconds"]
    )
    def test_non_positive_timeouts_fail(self, tmp_path: Path, key: str) -> None:
        with pytest.raises(NativeConfigError, match="positive integer"):
            _agent(tmp_path, f"enabled = true\n{key} = 0")

    def test_non_integer_timeout_fails(self, tmp_path: Path) -> None:
        with pytest.raises(NativeConfigError, match="positive integer"):
            _agent(tmp_path, 'enabled = true\nmax_active_tasks = "four"')

    def test_unknown_agent_key_fails(self, tmp_path: Path) -> None:
        with pytest.raises(NativeConfigError, match="unknown \\[agent\\] keys"):
            _agent(tmp_path, "enabled = true\nretention_hours = 24")


class TestProxyPolicy:
    """``[agent.proxy]`` is policy only. v0.11 supports exactly one source."""

    def test_proxy_defaults_off_and_env_sourced(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, "enabled = true")
        assert agent.proxy.enabled is False
        assert agent.proxy.source == "env"

    def test_proxy_can_be_enabled_with_the_env_source(self, tmp_path: Path) -> None:
        agent = _agent(
            tmp_path, 'enabled = true\n\n[agent.proxy]\nenabled = true\nsource = "env"\n'
        )
        assert (agent.proxy.enabled, agent.proxy.source) == (True, "env")

    @pytest.mark.parametrize("source", ["file", "registry", "winhttp", "system", "keyring"])
    def test_other_proxy_sources_are_refused(self, tmp_path: Path, source: str) -> None:
        """Naming a source with no consumer would be a promise the release cannot keep."""
        with pytest.raises(NativeConfigError, match="agent.proxy.source must be one of"):
            _agent(
                tmp_path,
                f'enabled = true\n\n[agent.proxy]\nenabled = true\nsource = "{source}"',
            )

    def test_model_carries_no_endpoint_field(self, tmp_path: Path) -> None:
        """The parsed model must be structurally incapable of holding the endpoint."""
        agent = _agent(tmp_path, "enabled = true\n\n[agent.proxy]\nenabled = true\n")
        assert not hasattr(agent.proxy, "url")
        assert not hasattr(agent, "proxy_url")
        assert "url" not in {f for f in agent.proxy.__dataclass_fields__}

    def test_mandatory_no_proxy_is_the_measured_loopback_set(self) -> None:
        assert MANDATORY_NO_PROXY == ("127.0.0.1", "localhost", "::1")


class TestWorkdirAgentPolicy:
    """Per-workdir mode and runtime allowlist, validated at startup."""

    def test_workspace_write_workdir_parses(self, tmp_path: Path) -> None:
        config = _write(
            tmp_path,
            "[agent]\nenabled = true\n\n[agent.codex]\nenabled = true\n\n"
            '[[workdirs]]\nalias = "project"\npath = "/srv/p"\nread_only = false\n'
            'agent_mode = "workspace-write"\nagent_runtimes = ["codex"]\n',
        )
        workdirs, _ = load_native_config(config)
        assert workdirs[0].agent_mode == AGENT_MODE_WORKSPACE_WRITE
        assert workdirs[0].agent_runtimes == frozenset({"codex"})

    def test_review_mode_needs_no_allowlist(self, tmp_path: Path) -> None:
        config = _write(
            tmp_path,
            '[agent]\nenabled = true\n\n[[workdirs]]\nalias = "p"\npath = "/srv/p"\n'
            'read_only = true\nagent_mode = "review"\n',
        )
        workdirs, _ = load_native_config(config)
        assert workdirs[0].agent_mode == AGENT_MODE_REVIEW

    def test_unknown_mode_fails(self, tmp_path: Path) -> None:
        config = _write(
            tmp_path,
            '[[workdirs]]\nalias = "p"\npath = "/srv/p"\nagent_mode = "yolo"\n',
        )
        with pytest.raises(NativeConfigError, match="agent_mode must be one of"):
            load_native_config(config)

    def test_unknown_runtime_fails_startup(self, tmp_path: Path) -> None:
        config = _write(
            tmp_path,
            '[agent]\nenabled = true\n\n[[workdirs]]\nalias = "p"\npath = "/srv/p"\n'
            'read_only = false\nagent_mode = "workspace-write"\nagent_runtimes = ["gemini"]\n',
        )
        with pytest.raises(NativeConfigError, match="unknown runtime 'gemini'"):
            load_native_config(config)

    def test_duplicate_runtime_fails_startup(self, tmp_path: Path) -> None:
        config = _write(
            tmp_path,
            '[agent]\nenabled = true\n\n[[workdirs]]\nalias = "p"\npath = "/srv/p"\n'
            'read_only = false\nagent_mode = "workspace-write"\n'
            'agent_runtimes = ["codex", "codex"]\n',
        )
        with pytest.raises(NativeConfigError, match="duplicate runtime"):
            load_native_config(config)

    def test_disabled_mode_with_runtimes_fails(self, tmp_path: Path) -> None:
        config = _write(
            tmp_path,
            '[agent]\nenabled = true\n\n[[workdirs]]\nalias = "p"\npath = "/srv/p"\n'
            'read_only = false\nagent_mode = "disabled"\nagent_runtimes = ["codex"]\n',
        )
        with pytest.raises(NativeConfigError, match="requires an agent_mode other than"):
            load_native_config(config)

    def test_workspace_write_requires_writable_workdir(self, tmp_path: Path) -> None:
        config = _write(
            tmp_path,
            '[[workdirs]]\nalias = "p"\npath = "/srv/p"\nread_only = true\n'
            'agent_mode = "workspace-write"\n',
        )
        with pytest.raises(NativeConfigError, match="requires read_only = false"):
            load_native_config(config)

    def test_native_runtime_requires_workspace_write_mode(self, tmp_path: Path) -> None:
        """A native runtime may only submit with workspace-write; review cannot host it."""
        config = _write(
            tmp_path,
            "[agent]\nenabled = true\n\n[agent.codex]\nenabled = true\n\n"
            '[[workdirs]]\nalias = "p"\npath = "/srv/p"\nread_only = false\n'
            'agent_mode = "review"\nagent_runtimes = ["codex"]\n',
        )
        with pytest.raises(NativeConfigError, match="requires agent_mode = 'workspace-write'"):
            load_native_config(config)

    def test_workdir_allowlisting_a_disabled_runtime_fails(self, tmp_path: Path) -> None:
        config = _write(
            tmp_path,
            '[agent]\nenabled = true\n\n[[workdirs]]\nalias = "p"\npath = "/srv/p"\n'
            'read_only = false\nagent_mode = "workspace-write"\nagent_runtimes = ["qoder"]\n',
        )
        with pytest.raises(NativeConfigError, match="agent.qoder.enabled is false"):
            load_native_config(config)

    def test_duplicate_alias_still_fails(self, tmp_path: Path) -> None:
        config = _write(
            tmp_path,
            '[agent]\nenabled = true\n\n[[workdirs]]\nalias = "p"\npath = "/srv/a"\n'
            '[[workdirs]]\nalias = "p"\npath = "/srv/b"\n',
        )
        with pytest.raises(NativeConfigError, match="duplicate"):
            load_native_config(config)


class TestNonRequirements:
    """Rules the plan deliberately does *not* impose."""

    def test_enabled_runtime_used_by_no_workdir_is_allowed(self, tmp_path: Path) -> None:
        """Staging a runtime before assigning it to a workdir is legitimate."""
        config = _write(
            tmp_path,
            "[agent]\nenabled = true\n\n[agent.codex]\nenabled = true\n\n"
            '[[workdirs]]\nalias = "p"\npath = "/srv/p"\nread_only = false\n',
        )
        workdirs, settings = load_native_config(config)
        assert settings.agent_enabled is True
        assert workdirs[0].agent_runtimes == frozenset()

    def test_no_legacy_slot_key_is_accepted(self, tmp_path: Path) -> None:
        """Native workdirs must not acquire the Compose slot back."""
        config = _write(
            tmp_path,
            '[agent]\nenabled = true\n\n[[workdirs]]\nalias = "p"\npath = "/srv/p"\nslot = 3\n',
        )
        with pytest.raises(NativeConfigError, match="unknown keys: slot"):
            load_native_config(config)


class TestSecretHygiene:
    """``serverfs.toml`` stores policy, never credentials."""

    @pytest.mark.parametrize(
        "key",
        ["api_key", "proxy_url", "control_plane_key", "tunnel_password", "token"],
    )
    def test_secret_shaped_keys_are_refused(self, tmp_path: Path, key: str) -> None:
        """A secret has no consumer here, so an unknown key is the correct refusal."""
        with pytest.raises(NativeConfigError, match="unknown"):
            _agent(tmp_path, f'enabled = true\n{key} = "value"')

    def test_proxy_section_refuses_a_url_key(self, tmp_path: Path) -> None:
        with pytest.raises(NativeConfigError, match="unknown \\[agent.proxy\\] keys"):
            _agent(
                tmp_path,
                'enabled = true\n\n[agent.proxy]\nenabled = true\nurl = "http://127.0.0.1:8080"\n',
            )
