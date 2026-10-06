"""Phase D tests for the native supervisor's Agent lifecycle (§15 D5, D6, D7).

Two halves, and the first is the more important one:

- **The Agent-disabled fast path.** This is the hard upgrade gate. A config without ``[agent]`` must
  behave exactly as v0.10 did, and "behaves as v0.10" is asserted structurally — no Job Object, no
  Bridge process, no Agent state tree, no runtime resolution, no proxy requirement — rather than by
  observing that nothing appeared.
- **The Agent-enabled path.** Startup order, authenticated readiness, containment, and the bounded
  shutdown sequence.

The readiness tests use the real Named Pipe through ``AgentBridgeClient``, because a mocked
readiness check would prove only that the mock returns.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from serverfs_mcp import supervisor
from serverfs_mcp.agent_lifecycle import (
    AgentLifecycleError,
    agent_proxy_from_environment,
    await_graceful_exit,
    bootstrap_frame_bytes,
    bridge_child_environment,
    render_request_from_config,
    request_graceful_shutdown,
    spawn_bridge,
    wait_for_bridge_readiness,
)
from serverfs_mcp.agent_proxy import AgentProxyConfig
from serverfs_mcp.native_config import load_native_config
from serverfs_mcp.windows_job import WindowsJob

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native lifecycle")

V010_CONFIG = """\
[server]
log_level = "INFO"

[[workdirs]]
alias = "repo"
path = "{root}"
read_only = false
"""

#: Agent-enabled, with the optional proxy block placed *before* [[workdirs]] so it stays a top-level
#: table. Appending it after a [[workdirs]] entry would silently nest it inside that workdir.
AGENT_CONFIG = """\
[server]
log_level = "INFO"

[agent]
enabled = true

[agent.codex]
enabled = true

[[workdirs]]
alias = "repo"
path = "{root}"
read_only = false
agent_mode = "workspace-write"
agent_runtimes = ["codex"]
"""

AGENT_PROXY_CONFIG = """\
[server]
log_level = "INFO"

[agent]
enabled = true

[agent.codex]
enabled = true

[agent.proxy]
enabled = true
source = "env"

[[workdirs]]
alias = "repo"
path = "{root}"
read_only = false
agent_mode = "workspace-write"
agent_runtimes = ["codex"]
"""

#: Marker values planted in the parent environment. Dummy strings, never real credentials.
POLLUTION = {
    "CONTROL_PLANE_FAKE_SECRET": "marker-control-plane",
    "TUNNEL_CLIENT_FAKE_SECRET": "marker-tunnel-client",
    "SERVERFS_PROXY_PASSWORD": "marker-tunnel-proxy",
    "SERVERFS_AGENT_PROXY_URL": "http://127.0.0.1:19999",
    "SERVERFS_AGENT_NO_PROXY": "marker-no-proxy",
    "HTTP_PROXY": "http://127.0.0.1:19080",
    "HTTPS_PROXY": "http://127.0.0.1:19080",
    "ALL_PROXY": "http://127.0.0.1:19080",
    "NO_PROXY": "marker.example",
}


def _write_config(tmp_path: Path, template: str, root: Path) -> Path:
    config = tmp_path / "serverfs.toml"
    # A TOML basic string cannot carry a bare backslash, and a Windows temp path is full of them.
    # Escaping here rather than in each template keeps the templates readable as TOML.
    escaped = str(root).replace("\\", "\\\\")
    config.write_text(template.format(root=escaped), encoding="utf-8")
    return config


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


class TestAgentDisabledFastPath:
    """The v0.10 upgrade gate, asserted structurally."""

    def test_absent_agent_section_is_not_detected_as_enabled(self, tmp_path: Path, workdir: Path):
        config = _write_config(tmp_path, V010_CONFIG, workdir)
        assert supervisor._agent_delegation_enabled(str(config)) is False

    def test_explicitly_disabled_is_not_detected_as_enabled(self, tmp_path: Path, workdir: Path):
        config = _write_config(tmp_path, V010_CONFIG + "\n[agent]\nenabled = false\n", workdir)
        assert supervisor._agent_delegation_enabled(str(config)) is False

    def test_enabled_is_detected(self, tmp_path: Path, workdir: Path):
        config = _write_config(tmp_path, AGENT_CONFIG, workdir)
        assert supervisor._agent_delegation_enabled(str(config)) is True

    def test_unparseable_config_falls_back_to_filesystem_only(self, tmp_path: Path):
        """The serve child reports the configuration error, as it did in v0.10."""
        config = tmp_path / "serverfs.toml"
        config.write_text("this is not valid toml [[[", encoding="utf-8")
        assert supervisor._agent_delegation_enabled(str(config)) is False

    def test_missing_config_falls_back_to_filesystem_only(self, tmp_path: Path):
        assert supervisor._agent_delegation_enabled(str(tmp_path / "absent.toml")) is False

    def test_disabled_path_creates_no_agent_state(self, tmp_path: Path, workdir: Path, monkeypatch):
        """No data home, no Job Object, no Bridge: the gate is about what is *not* touched."""
        config = _write_config(tmp_path, V010_CONFIG, workdir)
        data_home = tmp_path / "data-home"
        monkeypatch.setenv("SERVERFS_DATA_HOME", str(data_home))

        called: list[str] = []
        monkeypatch.setattr(
            supervisor, "forward_stdio", lambda command, env: called.append("v010") or 0
        )
        # A Job Object creation would be observable here; assert the Agent path was not entered.
        monkeypatch.setattr(
            supervisor,
            "run_with_agent",
            lambda *a, **k: pytest.fail("the Agent lifecycle ran with Agent disabled"),
        )
        assert supervisor.main(["--config", str(config)]) == 0
        assert called == ["v010"]
        assert not data_home.exists(), "the disabled path created an Agent data tree"

    def test_v010_environment_scrub_is_unchanged(self):
        """The v0.10 scrub keeps exactly its own contract.

        It predates the Agent namespace, so ``SERVERFS_AGENT_*`` is *not* in its prefix list — the
        wider scrub is D3's ``bridge_environment``, used only for the Bridge child. This test pins
        the v0.10 behaviour so a Phase D change cannot quietly widen or narrow what the stdio child
        receives.
        """
        env = supervisor.sanitized_environment(dict(POLLUTION))
        # v0.10 removes these namespaces and every proxy spelling.
        for name in (
            "CONTROL_PLANE_FAKE_SECRET",
            "TUNNEL_CLIENT_FAKE_SECRET",
            "SERVERFS_PROXY_PASSWORD",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        ):
            assert name not in env, name
        # And it does not know about the Agent namespace, which is a later addition.
        assert "SERVERFS_AGENT_PROXY_URL" in env

    def test_the_bridge_child_scrub_is_stricter_than_the_v010_one(self):
        """The Bridge child gets the wider scrub, so nothing Agent-related reaches it."""
        bridge_env = bridge_child_environment(None, source=dict(POLLUTION))
        assert bridge_env == {}
        assert supervisor.sanitized_environment(dict(POLLUTION)) != {}

    def test_v010_scrub_preserves_provider_environment(self):
        env = supervisor.sanitized_environment({"PATH": "p", "USERPROFILE": "u", "CODEX_HOME": "c"})
        assert env == {"PATH": "p", "USERPROFILE": "u", "CODEX_HOME": "c"}


class TestBridgeChildEnvironment:
    """The Bridge starts free of proxy variables and credential namespaces."""

    def test_pollution_is_absent(self) -> None:
        env = bridge_child_environment(None, source=dict(POLLUTION))
        for name in POLLUTION:
            assert name not in env, name

    def test_no_proxy_variable_survives_in_any_case(self) -> None:
        env = bridge_child_environment(
            None,
            source={
                **POLLUTION,
                "http_proxy": "x",
                "https_proxy": "x",
                "all_proxy": "x",
                "no_proxy": "x",
            },
        )
        assert not [
            n for n in env if n.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
        ]

    def test_provider_native_environment_survives(self) -> None:
        source = {
            "PATH": "p",
            "HOME": "h",
            "USERPROFILE": "u",
            "LOCALAPPDATA": "l",
            "CODEX_HOME": "c",
        }
        assert bridge_child_environment(None, source=source) == source

    def test_endpoint_is_not_placed_in_the_environment(self) -> None:
        """The endpoint travels on stdin, so the Bridge env carries no Agent namespace at all."""
        proxy = AgentProxyConfig(url="http://127.0.0.1:18080", no_proxy="127.0.0.1")
        env = bridge_child_environment(proxy, source=dict(POLLUTION))
        assert "18080" not in json.dumps(env)
        assert not [n for n in env if n.upper().startswith("SERVERFS_AGENT_")]


class TestBootstrapFrame:
    """One bounded frame, runtime material only."""

    def test_frame_carries_the_endpoint_when_configured(self) -> None:
        proxy = AgentProxyConfig(url="http://127.0.0.1:18080", no_proxy="corp.example,127.0.0.1")
        frame = json.loads(bootstrap_frame_bytes(proxy).decode("utf-8"))
        assert frame["version"] == 1
        assert frame["agent_proxy"]["enabled"] is True
        assert frame["agent_proxy"]["url"] == "http://127.0.0.1:18080"
        assert "127.0.0.1" in frame["agent_proxy"]["no_proxy"]

    def test_frame_without_a_proxy_is_explicitly_disabled(self) -> None:
        frame = json.loads(bootstrap_frame_bytes(None).decode("utf-8"))
        assert frame["agent_proxy"] == {"enabled": False}

    def test_frame_is_one_line(self) -> None:
        assert bootstrap_frame_bytes(None).count(b"\n") == 1


class TestRenderRequest:
    """Only Agent-configured workdirs cross to the Bridge."""

    def test_disabled_workdirs_are_omitted(self, tmp_path: Path, workdir: Path):
        # {root} is left in place so the shared helper does the escaping: a TOML basic string cannot
        # carry the bare backslashes a Windows temp path is full of.
        config = _write_config(
            tmp_path,
            AGENT_CONFIG + '\n[[workdirs]]\nalias = "plain"\npath = "{root}"\n',
            workdir,
        )
        workdirs, settings = load_native_config(config)
        request = render_request_from_config(workdirs, settings)
        assert [entry["alias"] for entry in request["workdirs"]] == ["repo"]

    def test_render_request_carries_no_endpoint(self, tmp_path: Path, workdir: Path):
        """The endpoint must not travel; the boolean policy legitimately does.

        Checking for the substring "proxy" would be wrong now that ``use_proxy`` is part of the
        request — that is the routing policy and it is supposed to be there. What must be absent is
        the endpoint value itself.
        """
        config = _write_config(tmp_path, AGENT_CONFIG, workdir)
        workdirs, settings = load_native_config(config)
        request = render_request_from_config(workdirs, settings)
        blob = json.dumps(request)
        assert "127.0.0.1" not in blob
        assert "agent_proxy" not in request
        # The policy itself is present, which is the point of the runtime-policy fix.
        assert request["runtimes"]["codex"]["use_proxy"] is True
        assert "codex_bin" in request["runtimes"]["codex"]

    def test_render_request_refuses_a_disabled_configuration(self, tmp_path: Path, workdir: Path):
        config = _write_config(tmp_path, V010_CONFIG, workdir)
        workdirs, settings = load_native_config(config)
        with pytest.raises(AgentLifecycleError, match="does not enable Agent"):
            render_request_from_config(workdirs, settings)

    def test_render_request_needs_an_agent_workdir(self, tmp_path: Path, workdir: Path):
        template = '[agent]\nenabled = true\n\n[[workdirs]]\nalias = "repo"\npath = "{root}"\n'
        config = _write_config(tmp_path, template, workdir)
        workdirs, settings = load_native_config(config)
        with pytest.raises(AgentLifecycleError, match="no workdir configures an agent_mode"):
            render_request_from_config(workdirs, settings)


class TestProxyFromEnvironment:
    def test_disabled_configuration_yields_no_proxy(self, tmp_path: Path, workdir: Path):
        config = _write_config(tmp_path, V010_CONFIG, workdir)
        _workdirs, settings = load_native_config(config)
        assert agent_proxy_from_environment(settings.agent, dict(POLLUTION)) is None

    def test_enabled_without_an_endpoint_fails_the_startup(self, tmp_path: Path, workdir: Path):
        config = _write_config(
            tmp_path,
            AGENT_CONFIG + '\n[agent.proxy]\nenabled = true\nsource = "env"\n',
            workdir,
        )
        _workdirs, settings = load_native_config(config)
        with pytest.raises(AgentLifecycleError):
            agent_proxy_from_environment(settings.agent, {})

    def test_credential_bearing_endpoint_fails_the_startup(self, tmp_path: Path, workdir: Path):
        config = _write_config(
            tmp_path,
            AGENT_CONFIG + '\n[agent.proxy]\nenabled = true\nsource = "env"\n',
            workdir,
        )
        _workdirs, settings = load_native_config(config)
        env = {"SERVERFS_AGENT_PROXY_URL": "http://user:pass@127.0.0.1:8080"}
        with pytest.raises(AgentLifecycleError, match="credentials"):
            agent_proxy_from_environment(settings.agent, env)


class TestReadiness:
    """Authenticated readiness, never a sleep."""

    def test_a_dead_process_fails_immediately(self, tmp_path: Path) -> None:
        """A Bridge that exited must surface as itself, not as a generic timeout."""
        dead = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
        dead.wait(timeout=30)
        with pytest.raises(AgentLifecycleError, match="exited before becoming ready"):
            asyncio.run(
                wait_for_bridge_readiness(Path(r"\\.\pipe\serverfs-absent-e2e"), dead, timeout=30.0)
            )

    def test_an_unreachable_endpoint_times_out_with_a_redacted_message(
        self, tmp_path: Path
    ) -> None:
        alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with pytest.raises(AgentLifecycleError) as raised:
                asyncio.run(
                    wait_for_bridge_readiness(
                        Path(r"\\.\pipe\serverfs-never-binds-e2e"), alive, timeout=1.0
                    )
                )
            message = str(raised.value)
            assert "did not become ready" in message
            assert "marker" not in message
        finally:
            alive.kill()
            alive.wait(timeout=15)


class TestGracefulShutdown:
    """Bounded, and pipe-EOF driven."""

    def test_closing_the_pipe_is_the_shutdown_request(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
        )
        try:
            request_graceful_shutdown(process)
            assert process.stdin is not None and process.stdin.closed
            assert await_graceful_exit(process, timeout=15.0) is True
            assert process.returncode == 0
        finally:
            if process.poll() is None:
                process.kill()

    def test_bounded_wait_reports_a_timeout_rather_than_hanging(self) -> None:
        """An unbounded wait would turn an unresponsive Bridge into a hung supervisor."""
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            assert await_graceful_exit(process, timeout=1.0) is False
        finally:
            process.kill()
            process.wait(timeout=15)

    def test_shutdown_request_on_a_dead_process_is_harmless(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait(timeout=30)
        request_graceful_shutdown(process)  # must not raise

    def test_terminate_process_reaps_a_running_child(self) -> None:
        from serverfs_mcp.agent_lifecycle import terminate_process

        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        terminate_process(process)
        assert process.poll() is not None


class TestSpawnUnderContainment:
    """Material is never sent to a child that is not yet contained."""

    def test_spawn_assigns_before_returning(self, tmp_path: Path) -> None:
        job = WindowsJob()
        job.open()
        try:
            process = spawn_bridge(
                config_path=tmp_path / "bridge.json",
                child_env={"PATH": os.environ.get("PATH", "")},
                job=job,
                argv=[sys.executable, "-c", "import time; time.sleep(30)"],
            )
            assert process.poll() is None
            assert job.is_open
            # Closing the job must take the child with it: proof the assignment happened.
            job.close()
            assert process.wait(timeout=20) is not None
        finally:
            job.close()

    def test_unstartable_command_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(AgentLifecycleError, match="could not be started"):
            spawn_bridge(
                config_path=tmp_path / "bridge.json",
                child_env={},
                argv=[str(tmp_path / "definitely-not-an-executable")],
            )
