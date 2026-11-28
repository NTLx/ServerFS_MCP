"""Windows v25 runtime/CLI boundaries using synthetic manifests only.

No real Task Scheduler, service, Tunnel, provider or network operation.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from serverfs_mcp.windows_v25 import cli, runtime, storage
from serverfs_mcp.windows_v25.spec import Manifest


@pytest.fixture
def core(tmp_path: Path) -> Manifest:
    return Manifest.parse(
        {
            "schema": 1,
            "component": "core",
            "sid": "S-1-5-21-111-222-333-1001",
            "pythonw": str(tmp_path / "envs/core/Scripts/pythonw.exe"),
            "settings": {
                "config": str(tmp_path / "policy/serverfs.toml"),
                "auth_file": str(tmp_path / "secret/core.env"),
                "port": 18767,
                "hostname": "core.example.net",
            },
        }
    )


def test_core_reuses_shared_business_server_but_not_old_windows_lifecycle() -> None:
    source = Path(runtime.__file__).read_text(encoding="utf-8")
    assert "cli.main([" in source
    assert re.search(r'"--transport"\s*,\s*"streamable-http"', source) is not None
    assert "os.environ.clear()" in source
    assert "LifecycleLease().acquire()" in source
    assert "with WindowsJob(job_name()) as job:" in source
    for forbidden in (
        "windows_core_control",
        "windows_connector_control",
        "windows_component_cli",
        "windows_bridge_control",
        "run_with_agent(",
        '"ServerFS Native Tunnel"',
    ):
        assert forbidden not in source


def test_core_pythonw_missing_console_gets_protected_runtime_log(
    core: Manifest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A windowless task must not crash when JSON logs use sys.stderr."""
    from serverfs_mcp import logging as jsonlog
    from serverfs_mcp import windows_private_artifacts

    home = tmp_path / "private-core"
    home.mkdir()
    seen: list[str] = []
    monkeypatch.setattr(storage, "home", lambda _component: home)
    monkeypatch.setattr(
        storage,
        "check_private",
        lambda path, *, directory, sid: seen.append(path.name),
    )
    monkeypatch.setattr(
        windows_private_artifacts,
        "protect_new_file",
        lambda path, sid: seen.append("protected:" + path.name),
    )
    monkeypatch.setattr(runtime, "_verified_owner", lambda _component: core)

    def fake_core(manifest: Manifest) -> int:
        assert manifest == core
        jsonlog.info("v25_windowless_startup_regression")
        return 0

    monkeypatch.setattr(runtime, "run_core", fake_core)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout = None
    sys.stderr = None
    log_stream = None
    try:
        assert runtime.main(["core"]) == 0
        log_stream = sys.stderr
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr
        if log_stream is not None:
            log_stream.close()
    text = (home / "runtime.log").read_text(encoding="utf-8")
    assert '"event": "v25_windowless_startup_regression"' in text
    assert "protected:runtime.log" in seen
    assert "runtime.log" in seen


def test_connector_runtime_scrubs_ambient_proxy_and_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(storage, "root", lambda: tmp_path / "ServerFS/v25")
    monkeypatch.setenv("MCP_EXTRA_HEADERS", "Authorization: other")
    monkeypatch.setenv("SERVERFS_HTTP_API_KEY", "sensitive")
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "sensitive")
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted")
    value = runtime._minimal_env(openai=True)
    assert value["HEALTH_LISTEN_ADDR"] == "127.0.0.1:0"
    for name in (
        "MCP_EXTRA_HEADERS",
        "SERVERFS_HTTP_API_KEY",
        "CONTROL_PLANE_API_KEY",
        "HTTPS_PROXY",
    ):
        assert name not in value


@pytest.mark.parametrize("component", ["cloudflare", "openai"])
def test_connector_command_never_spawns_core_or_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    settings = {
        "cloudflare": {
            "cloudflared": str(tmp_path / "bin/cloudflared.exe"),
            "token_file": str(tmp_path / "secrets/token"),
            "port": 18767,
            "hostname": "core.example.net",
        },
        "openai": {
            "tunnel_client": str(tmp_path / "bin/tunnel-client.exe"),
            "tunnel_id": "tunnel_" + "a" * 32,
            "control_plane_key_file": str(tmp_path / "secrets/plane.key"),
            "core_auth_header_file": str(tmp_path / "secrets/core.header"),
            "port": 18767,
            "hostname": "core.example.net",
        },
    }
    manifest = Manifest.parse(
        {
            "schema": 1,
            "component": component,
            "sid": "S-1-5-21-111-222-333-1001",
            "pythonw": str(tmp_path / "envs" / component / "Scripts/pythonw.exe"),
            "settings": settings[component],
        }
    )
    monkeypatch.setattr(runtime, "_private_input", lambda path, _roots, _sid: path)
    executable, args = runtime._connector_command(manifest, [])
    if component == "openai":
        assert executable.name == "tunnel-client.exe"
        assert "url=http://127.0.0.1:18767/mcp" in args
        header = "Authorization: file:" + str(tmp_path / "secrets/core.header")
        assert args[args.index("--mcp.extra-headers") + 1] == header
        assert args[args.index("--mcp.discovery-extra-headers") + 1] == header
        assert "--mcp.command" not in args
        assert "serverfs_mcp.supervisor" not in " ".join(args)
    else:
        assert executable.name == "cloudflared.exe"
        assert args[:3] == ["tunnel", "--no-autoupdate", "run"]
        assert "--token-file" in args
    assert "--transport" not in args
    assert "--config" not in args


def test_mutating_cli_rejects_configuration_flags() -> None:
    args = cli.parser().parse_args(["core", "start", "--config", "C:/Users/foo/serverfs.toml"])
    with pytest.raises(cli.ManifestError, match="only for configure"):
        cli.dispatch(args)


def test_bridge_start_refuses_legacy_same_sid_owner(
    core: Manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = Manifest.parse(
        {
            "schema": 1,
            "component": "bridge",
            "sid": core.sid,
            "pythonw": core.pythonw.replace("/core/", "/bridge/"),
            "settings": {
                "config": core.settings["config"],
                "bridge_python": core.pythonw.replace("pythonw.exe", "python.exe"),
            },
        }
    )
    args = cli.parser().parse_args(["bridge", "start"])
    monkeypatch.setattr(storage, "load", lambda _component: bridge)
    monkeypatch.setattr(cli, "_legacy_bridge_active", lambda: True)

    class MustNotStart:
        def start(self, _manifest):
            pytest.fail("new Bridge must never launch alongside legacy SID owner")

    monkeypatch.setattr(cli, "Scheduler", MustNotStart)
    with pytest.raises(cli.TaskError, match="W3 handover"):
        cli.dispatch(args)


def test_unknown_legacy_owner_state_refuses_handover(monkeypatch: pytest.MonkeyPatch) -> None:
    from serverfs_mcp.windows_v25 import tasks

    monkeypatch.setattr(
        tasks,
        "_run",
        lambda args: subprocess.CompletedProcess(args, 0, b"Unknown"),
    )
    with pytest.raises(cli.TaskError, match="unknown"):
        cli._legacy_bridge_active()


def test_status_never_prints_private_paths(core: Manifest, monkeypatch) -> None:
    args = cli.parser().parse_args(["core", "status"])
    monkeypatch.setattr(storage, "load", lambda _component: core)

    class Fake:
        def status(self, manifest):
            return {
                "component": manifest.component,
                "registered": False,
                "enabled": False,
                "readiness": "not_verified",
            }

    monkeypatch.setattr(cli, "Scheduler", Fake)
    result = cli.dispatch(args)
    assert "secret" not in str(result)
    assert "auth_file" not in str(result)
