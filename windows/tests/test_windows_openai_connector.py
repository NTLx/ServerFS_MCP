"""Pure OpenAI HTTP-upstream connector tests; no tunnel-client process starts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from serverfs_mcp import windows_openai_connector as connector


@pytest.fixture
def spec(tmp_path: Path) -> connector.OpenAIHTTPConnector:
    return connector.OpenAIHTTPConnector(
        tunnel_client=tmp_path / "tunnel-client.exe",
        tunnel_id="tunnel_0123456789abcdef0123456789abcdef",
        control_plane_key_file=tmp_path / "private" / "control-plane.key",
        core_auth_header_file=tmp_path / "private" / "core-auth-header.txt",
        core_port=19876,
        core_hostname="core.example.test",
    )


def test_direct_http_upstream_replaces_stdio_supervisor(spec) -> None:
    command = connector.command(spec)
    assert command == [
        str(spec.tunnel_client),
        "run",
        "--control-plane.tunnel-id",
        spec.tunnel_id,
        "--control-plane.api-key",
        f"file:{spec.control_plane_key_file}",
        "--mcp.server-url",
        "url=http://127.0.0.1:19876/mcp",
        "--mcp.extra-headers",
        f"Authorization: file:{spec.core_auth_header_file}",
    ]
    assert "--mcp.command" not in command
    assert "serverfs_mcp.supervisor" not in " ".join(command)
    assert "serverfs_mcp.cli" not in " ".join(command)
    assert "Authorization: Bearer" not in " ".join(command)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tunnel_id", "not-a-tunnel"),
        ("core_port", 0),
        ("core_port", True),
        ("core_port", 65536),
        ("core_hostname", "*.example.com"),
        ("tunnel_client", Path("relative.exe")),
        ("core_auth_header_file", Path("auth.txt")),
    ],
)
def test_invalid_contract_fails_without_launch(spec, field: str, value: object) -> None:
    with pytest.raises(connector.OpenAIConnectorError):
        connector.command(replace(spec, **{field: value}))


def test_environment_never_inherits_control_plane_or_core_secrets() -> None:
    polluted = {
        "PATH": "safe",
        "SystemRoot": "C:/Windows",
        "LOCALAPPDATA": "C:/User/AppData",
        "MCP_EXTRA_HEADERS": "Authorization: Bearer stolen",
        "MCP_DISCOVERY_EXTRA_HEADERS": "danger",
        "CONTROL_PLANE_API_KEY": "secret",
        "OPENAI_API_KEY": "secret",
        "TUNNEL_CLIENT_TOKEN": "secret",
        "SERVERFS_HTTP_API_KEY": "core-secret",
        "HTTP_PROXY": "http://credentialed-proxy",
        "PYTHONPATH": "D:/untrusted",
    }
    actual = connector.environment(polluted)
    assert actual == {
        "PATH": "safe",
        "SystemRoot": "C:/Windows",
        "LOCALAPPDATA": "C:/User/AppData",
        "HEALTH_LISTEN_ADDR": "127.0.0.1:0",
    }
    assert "secret" not in str(actual)


def test_openai_generation_launches_only_tunnel_client_after_core_proof(
    spec, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import sys

    monkeypatch.setattr(sys, "platform", "win32")
    for path in (
        spec.tunnel_client,
        spec.control_plane_key_file,
        spec.core_auth_header_file,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic-test-only")

    events = []

    class Job:
        def assign(self, proc) -> None:
            events.append("job.assign")

        def resume(self, proc) -> None:
            events.append("job.resume")

    class Process:
        polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls == 1 else 0

        def wait(self):
            events.append("child.wait")
            return 0

    def popen(argv, **kwargs):
        events.append("connector.spawn")
        assert argv == connector.command(spec)
        assert kwargs["env"]["HEALTH_LISTEN_ADDR"] == "127.0.0.1:0"
        return Process()

    result = connector.run_connector_generation(
        spec,
        job=Job(),
        stdout=io.BytesIO(),
        stderr=io.BytesIO(),
        verify_core_owner=lambda: events.append("core.owner") or True,
        probe=lambda port, host: events.append(f"probe:{host}") or True,
        popen=popen,
        clock=lambda: 0.0,
        sleep=lambda _: None,
    )
    assert result == 2
    assert events == [
        "core.owner",
        "probe:127.0.0.1:19876",
        "connector.spawn",
        "job.assign",
        "job.resume",
        "child.wait",
    ]


def test_openai_generation_refuses_core_owner_loss_after_launch(
    spec, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import sys

    monkeypatch.setattr(sys, "platform", "win32")
    for path in (
        spec.tunnel_client,
        spec.control_plane_key_file,
        spec.core_auth_header_file,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic-only")
    attempts = []

    class Process:
        def poll(self):
            return None

    class Job:
        def assign(self, process):
            pass

        def resume(self, process):
            pass

    def owner() -> bool:
        attempts.append("checked")
        return len(attempts) == 1

    times = iter((0.0, 6.0))
    with pytest.raises(connector.OpenAIConnectorError, match="owner changed"):
        connector.run_connector_generation(
            spec,
            job=Job(),
            stdout=io.BytesIO(),
            stderr=io.BytesIO(),
            verify_core_owner=owner,
            probe=lambda *args: True,
            popen=lambda *args, **kwargs: Process(),
            clock=lambda: next(times),
            sleep=lambda _: None,
            check_interval_seconds=5.0,
        )
    assert len(attempts) == 2


def test_openai_connector_has_no_task_scheduler_or_process_creation() -> None:
    source = Path(connector.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "subprocess.Popen(",
        "WindowsJob(",
        "run_native_tunnel(",
        "serverfs_mcp.supervisor",
        "schtasks.exe",
    ):
        assert forbidden not in source
