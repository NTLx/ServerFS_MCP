"""macOS v0.25 predeclared Connector IDs: CLI routing only, never register jobs."""

from __future__ import annotations

import json

import pytest

from serverfs_mcp import (
    darwin_instance_cli,
    darwin_instance_owned_runtime,
    darwin_instance_runtime,
)


@pytest.mark.parametrize("owned", [False, True])
def test_stage_cli_forwards_explicit_connector_id_predeclarations(
    owned: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "darwin")
    observed: list[tuple[object, dict[str, list[str]]]] = []

    def fake_stage(identity, *_args, connector_ids=None):
        observed.append((identity, connector_ids))
        return {"instance_id": identity.instance_id, "state": "staged-not-registered"}

    if owned:
        monkeypatch.setattr(darwin_instance_owned_runtime, "stage_owned_core_bridge", fake_stage)
    else:
        monkeypatch.setattr(darwin_instance_runtime, "stage_file_only_instance", fake_stage)

    command = "owned-stage" if owned else "stage"
    argv = ["--instance", "staging", command]
    argv.extend(
        [
            "--config",
            "/private/operator.toml",
            "--env-file",
            "/private/core.env",
            "--http-port",
            "18783",
            "--hostname",
            "synthetic.test",
            "--template-app",
            "/private/template.app",
        ]
    )
    if owned:
        argv.extend(["--bridge-python", "/private/bridge-python"])
    argv.extend(
        [
            "--cloudflare-connector-id",
            "blue",
            "--cloudflare-connector-id",
            "green",
            "--openai-connector-id",
            "blue",
        ]
    )

    assert darwin_instance_cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "staged-not-registered"
    assert len(observed) == 1
    identity, ids = observed[0]
    assert identity.instance_id == "staging"
    assert identity.bridge_mode == ("owned" if owned else "disabled")
    assert ids == {"cloudflare": ["blue", "green"], "openai": ["blue"]}


@pytest.mark.parametrize("owned", [False, True])
def test_unmodified_stage_cli_does_not_change_legacy_staging_call(
    owned: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "darwin")
    observed: list[dict[str, object]] = []

    def fake_stage(identity, *_args, **kwargs):
        observed.append(kwargs)
        return {"instance_id": identity.instance_id}

    module = darwin_instance_owned_runtime if owned else darwin_instance_runtime
    name = "stage_owned_core_bridge" if owned else "stage_file_only_instance"
    monkeypatch.setattr(module, name, fake_stage)
    argv = [
        "--instance",
        "staging",
        "owned-stage" if owned else "stage",
        "--config",
        "/private/operator.toml",
        "--env-file",
        "/private/core.env",
        "--http-port",
        "18783",
        "--hostname",
        "synthetic.test",
        "--template-app",
        "/private/template.app",
    ]
    if owned:
        argv.extend(["--bridge-python", "/private/bridge-python"])
    assert darwin_instance_cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["instance_id"] == "staging"
    assert observed == [{}]
