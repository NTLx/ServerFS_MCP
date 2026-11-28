"""Pure dispatch tests for v0.25 Windows per-component operator CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp import windows_component_cli as cli


@pytest.mark.parametrize("component", ("bridge", "core", "cloudflare", "openai"))
def test_status_dispatch_is_scoped_to_one_component(
    component: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = []
    if component == "bridge":
        from serverfs_mcp import windows_bridge_control as bridge

        monkeypatch.setattr(
            bridge, "status", lambda: observed.append("bridge") or {"component": "bridge"}
        )
    elif component == "core":
        from serverfs_mcp import windows_core_control as core

        monkeypatch.setattr(
            core, "status", lambda: observed.append("core") or {"component": "core"}
        )
    else:
        from serverfs_mcp import windows_connector_control as connector

        monkeypatch.setattr(
            connector,
            "status",
            lambda kind: observed.append(kind) or {"component": kind},
        )
    result = cli.dispatch(cli.parser().parse_args([component, "status"]))
    assert result == {"component": component}
    assert observed == [component]


def test_core_install_uses_dedicated_pythonw_and_fixed_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from serverfs_mcp import native_endpoint
    from serverfs_mcp import windows_core_control as core

    monkeypatch.setattr(native_endpoint, "current_user_sid", lambda: "S-1-5-21-1-2-3-1000")
    monkeypatch.setattr(cli.sys, "prefix", "C:/Private/ServerFS/core-owner-env")
    specs = []
    monkeypatch.setattr(
        core, "install_disabled", lambda spec: specs.append(spec) or {"enabled": False}
    )
    args = cli.parser().parse_args(
        [
            "core",
            "install",
            "--config",
            "C:/Private/serverfs.toml",
            "--auth-file",
            "C:/Private/auth.env",
            "--port",
            "49876",
            "--hostname",
            "core.example.test",
        ]
    )
    assert cli.dispatch(args) == {"enabled": False}
    assert len(specs) == 1
    assert specs[0].pythonw == Path("C:/Private/ServerFS/core-owner-env/Scripts/pythonw.exe")
    assert specs[0].port == 49876
    assert not specs[0].enabled


@pytest.mark.parametrize(
    "argv",
    [
        ["core", "install"],
        ["bridge", "install"],
        ["core", "start", "--config", "C:/unexpected"],
        ["cloudflare", "install", "--port", "49876"],
        ["openai", "restart", "--auth-file", "C:/private.env"],
    ],
)
def test_missing_or_unexpected_install_arguments_fail_before_component_action(
    argv: list[str],
) -> None:
    with pytest.raises(cli.ComponentCLIError):
        cli.dispatch(cli.parser().parse_args(argv))


def test_bridge_active_stop_is_still_provider_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    from serverfs_mcp import windows_bridge_control as bridge

    monkeypatch.setattr(
        bridge,
        "stop",
        lambda: (_ for _ in ()).throw(bridge.BridgeControlError("provider guard incomplete")),
    )
    with pytest.raises(bridge.BridgeControlError, match="guard"):
        cli.dispatch(cli.parser().parse_args(["bridge", "stop"]))
