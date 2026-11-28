from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp import cli
from serverfs_mcp.config import Settings


def _args(*extra: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(
        [
            "serve",
            "--config",
            "/tmp/serverfs.toml",
            "--transport",
            "streamable-http",
            "--macos-instance",
            "named-core",
            *extra,
        ]
    )


def _darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    from serverfs_mcp import darwin_platform

    monkeypatch.setattr(
        darwin_platform,
        "darwin_platform_status",
        lambda: SimpleNamespace(supported=True),
    )


def _native_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "_load",
        lambda _path: ([], SimpleNamespace(bridge_required=True, log_level="DEBUG")),
    )
    monkeypatch.setenv("SERVERFS_HTTP_API_KEY", "test-key")


def test_parser_adds_opt_in_flag_and_keeps_it_unset_by_default() -> None:
    args = cli.build_parser().parse_args(["serve", "--config", "/tmp/serverfs.toml"])
    assert args.macos_shared_bridge is None
    assert _args("--macos-shared-bridge", "shared-bridge").macos_shared_bridge == "shared-bridge"


@pytest.mark.parametrize(
    "args,platform",
    [
        (
            cli.build_parser().parse_args(
                [
                    "serve",
                    "--config",
                    "/tmp/serverfs.toml",
                    "--macos-shared-bridge",
                    "bridge",
                ]
            ),
            "darwin",
        ),
        (_args("--macos-shared-bridge", ""), "darwin"),
        (_args("--macos-shared-bridge", "bridge"), "win32"),
    ],
)
def test_shared_flag_invalid_context_fails_before_server(
    monkeypatch: pytest.MonkeyPatch, args: argparse.Namespace, platform: str
) -> None:
    if platform == "darwin":
        _darwin(monkeypatch)
    else:
        monkeypatch.setattr(cli.sys, "platform", platform)
    _native_config(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr("serverfs_mcp.main.create_server", lambda *a: calls.append("create"))
    monkeypatch.setattr("serverfs_mcp.main.run_streamable_http", lambda *a: calls.append("run"))

    assert cli.cmd_serve(args) == 2
    assert calls == []


@pytest.mark.parametrize("failed_stage", ["manifest", "authentication"])
def test_shared_verification_failure_is_sanitized_and_precedes_registration(
    monkeypatch: pytest.MonkeyPatch, failed_stage: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _darwin(monkeypatch)
    _native_config(monkeypatch)
    from serverfs_mcp import darwin_instance_shared_manifest, darwin_shared_agent_factory

    def fail(*_args, **_kwargs):
        raise ValueError("secret-bearing private failure")

    monkeypatch.setattr(
        darwin_instance_shared_manifest,
        "resolve_shared_core_manifest",
        fail if failed_stage == "manifest" else lambda *_a: ("repo", Path("/private/bridge.sock")),
    )
    monkeypatch.setattr(
        darwin_shared_agent_factory,
        "prepare_shared_agent_adapter",
        fail if failed_stage == "authentication" else lambda *_a: None,
    )
    calls: list[str] = []
    monkeypatch.setattr("serverfs_mcp.main.create_server", lambda *a: calls.append("create"))
    monkeypatch.setattr("serverfs_mcp.main.run_streamable_http", lambda *a: calls.append("run"))

    assert cli.cmd_serve(_args("--macos-shared-bridge", "shared-bridge")) == 2
    assert calls == []
    assert "secret-bearing" not in capsys.readouterr().err


def test_shared_success_uses_authenticated_adapter_and_skips_owned_v1_and_ingress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _darwin(monkeypatch)
    _native_config(monkeypatch)
    from serverfs_mcp import (
        darwin_instance_shared_manifest,
        darwin_shared_agent_factory,
        darwin_shared_mutation_lease,
    )

    socket_path = tmp_path / "bridge.sock"
    owner_lock_dir = tmp_path / "owner-locks"
    lease = SimpleNamespace(lock_dir=owner_lock_dir, hold=lambda _lease, _root: None)
    adapter = SimpleNamespace(_workdir_alias="repo", call=lambda *_a: None)
    seen: dict[str, object] = {}

    def resolve(identity, config, port):
        seen["identity"] = identity
        seen["config"] = config
        seen["port"] = port
        return "repo", socket_path

    async def prepare(identity, config, trusted_socket):
        assert identity is seen["identity"]
        assert config == Path("/tmp/serverfs.toml")
        assert trusted_socket == socket_path
        return adapter

    def create(settings, registry, client, ingress):
        seen.update(settings=settings, client=client, ingress=ingress)
        return object()

    monkeypatch.setattr(darwin_instance_shared_manifest, "resolve_shared_core_manifest", resolve)
    monkeypatch.setattr(darwin_shared_agent_factory, "prepare_shared_agent_adapter", prepare)

    def prepare_lease(identity, trusted_socket, selected):
        assert identity is seen["identity"]
        assert trusted_socket == socket_path and selected is adapter
        return lease

    monkeypatch.setattr(
        darwin_shared_mutation_lease, "prepare_shared_mutation_lease", prepare_lease
    )
    monkeypatch.setattr(cli, "_native_agent_settings", lambda *_a, **_kw: pytest.fail("v1 wiring"))
    monkeypatch.setattr(cli, "_overlay_ingress_env", lambda *_a: pytest.fail("ingress env overlay"))
    monkeypatch.setattr(cli, "_darwin_ingress_helper", lambda *_a: pytest.fail("ingress helper"))
    monkeypatch.setattr("serverfs_mcp.main.create_server", create)
    monkeypatch.setattr(
        "serverfs_mcp.main.run_streamable_http",
        lambda *_a: seen.setdefault("ran", True),
    )

    assert cli.cmd_serve(_args("--macos-shared-bridge", "shared-bridge")) == 0
    settings = seen["settings"]
    assert isinstance(settings, Settings)
    assert settings.log_level == "DEBUG"
    assert settings.agent_bridge_enabled is True
    assert settings.agent_bridge_socket == str(socket_path)
    assert settings.agent_lock_dir == str(owner_lock_dir)
    assert settings.shared_mutation_lease is lease.hold
    assert seen["client"] is adapter
    assert seen["ingress"] is None
    assert seen["ran"] is True
