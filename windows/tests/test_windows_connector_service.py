"""No live Windows process: isolated connector policy and Job wiring tests."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from serverfs_mcp import windows_connector_control as control
from serverfs_mcp import windows_connector_service as service
from serverfs_mcp import windows_core_control as core_control


@pytest.fixture(params=("cloudflare", "openai"))
def instance(tmp_path: Path, request, monkeypatch: pytest.MonkeyPatch):
    from serverfs_mcp import native_lifecycle

    kind = request.param
    monkeypatch.setattr(native_lifecycle, "window_local_app_data", lambda: tmp_path, raising=False)
    monkeypatch.setattr(service, "_check_windows", lambda: None)
    monkeypatch.setattr(service, "_check_private", lambda path, **kw: None)
    home = service.service_home(kind)
    home.mkdir(parents=True)
    (home / "logs").mkdir()
    for name in ("connector.stdout.log", "connector.stderr.log"):
        (home / "logs" / name).write_bytes(b"")
    base = {
        "schema_version": 1,
        "kind": kind,
        "core_port": 19876,
        "core_hostname": "core.example.test",
    }
    if kind == "cloudflare":
        base.update(
            cloudflared=str(tmp_path / "bin" / "cloudflared.exe"),
            token_file=str(tmp_path / "private" / "cloudflare-token"),
        )
    else:
        base.update(
            tunnel_client=str(tmp_path / "bin" / "tunnel-client.exe"),
            tunnel_id="tunnel_0123456789abcdef0123456789abcdef",
            control_plane_key_file=str(tmp_path / "private" / "control-plane.key"),
            core_auth_header_file=str(tmp_path / "private" / "core-header"),
        )
    service.policy_path(kind).write_text(json.dumps(base), encoding="utf-8")
    monkeypatch.setattr(
        core_control,
        "_require_owned",
        lambda: ({"port": 19876, "hostname": "core.example.test"}, "<Task />"),
    )
    return kind, home, base


def test_private_per_kind_directories_are_separate(instance) -> None:
    kind, home, _ = instance
    assert service.manifest_path(kind).parent == home
    assert service.task_path(kind).parent == home
    assert service.state_path(kind).parent == home
    assert kind in service.policy_path(kind).parts
    assert "ServerFS Native Tunnel" not in str(home)


def test_load_policy_accepts_exact_schema_and_registered_core(instance) -> None:
    kind, _, expected = instance
    assert service.load_policy(kind) == expected


def test_load_policy_rejects_unexpected_keys(instance) -> None:
    kind, _, data = instance
    data["arbitrary_env"] = "OPENAI_API_KEY=secret"
    service.policy_path(kind).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(service.ConnectorServiceError, match="schema"):
        service.load_policy(kind)


def test_load_policy_rejects_foreign_core_binding(instance, monkeypatch) -> None:
    kind, _, _ = instance
    monkeypatch.setattr(
        core_control,
        "_require_owned",
        lambda: ({"port": 19877, "hostname": "core.example.test"}, "<Task />"),
    )
    with pytest.raises(service.ConnectorServiceError, match="does not match"):
        service.load_policy(kind)


def test_connector_requires_exact_fixed_policy_path(instance, monkeypatch) -> None:
    kind, home, _ = instance
    monkeypatch.setattr(service, "connector_mutex", lambda k: _null_context())
    with pytest.raises(service.ConnectorServiceError, match="fixed task scope"):
        service.run_owned_connector(kind, home / "other.json")


@contextmanager
def _null_context():
    yield


def test_runtime_owns_only_one_connector_job_and_closes_on_exit(instance, monkeypatch) -> None:
    from serverfs_mcp import windows_cloudflare_connector, windows_job, windows_openai_connector

    kind, home, data = instance
    events = []

    @contextmanager
    def lock(which: str):
        events.append("mutex.enter")
        try:
            yield
        finally:
            events.append("mutex.exit")

    class FakeJob:
        def __enter__(self):
            events.append("job.open")
            return self

        def __exit__(self, *args):
            events.append("job.close")

    def cloudflare(**kwargs):
        events.append("cloudflare.run")
        assert kwargs["job"].__class__ is FakeJob
        return 3

    def openai(spec, **kwargs):
        events.append("openai.run")
        assert spec.core_port == 19876
        assert kwargs["job"].__class__ is FakeJob
        return 3

    monkeypatch.setattr(service, "connector_mutex", lock)
    monkeypatch.setattr(service, "load_policy", lambda which: data)
    monkeypatch.setattr(service, "validate_runtime_inputs", lambda *args: None)
    monkeypatch.setattr(control, "record_runtime_owner", lambda which: events.append("stamp"))
    monkeypatch.setattr(windows_job, "WindowsJob", FakeJob)
    monkeypatch.setattr(windows_cloudflare_connector, "run_connector_generation", cloudflare)
    monkeypatch.setattr(windows_openai_connector, "run_connector_generation", openai)
    assert service.run_owned_connector(kind, service.policy_path(kind)) == 3
    assert events == [
        "mutex.enter",
        "stamp",
        "job.open",
        f"{kind}.run",
        "job.close",
        "mutex.exit",
    ]


def test_internal_cli_rejects_install_mutation_mode(instance) -> None:
    kind, home, _ = instance
    with pytest.raises(SystemExit) as ex:
        service.main(["install", "--kind", kind, "--policy", str(service.policy_path(kind))])
    assert ex.value.code == 2


def test_invalid_kind_cannot_be_passed_to_service_home(instance) -> None:
    with pytest.raises(service.ConnectorServiceError, match="invalid"):
        service.service_home("../legacy")
