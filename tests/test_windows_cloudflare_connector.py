"""Connector-only generation tests: neither Core nor legacy Task Scheduler is started."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from serverfs_mcp import windows_cloudflare_connector as connector


@pytest.fixture
def inputs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    monkeypatch.setattr(connector.sys, "platform", "win32")
    executable = tmp_path / "cloudflared.exe"
    credential = tmp_path / "token.txt"
    executable.write_bytes(b"test binary")
    credential.write_bytes(b"synthetic-test-token")
    return executable, credential


def run(
    inputs: tuple[Path, Path],
    *,
    job: object,
    owner_check,
    probe,
    popen,
) -> int:
    exe, token = inputs
    return connector.run_connector_generation(
        cloudflared=exe,
        token_file=token,
        core_port=19876,
        core_hostname="core.example.test",
        job=job,
        stdout=io.BytesIO(),
        stderr=io.BytesIO(),
        verify_core_owner=owner_check,
        probe=probe,
        popen=popen,
        sleep=lambda _: None,
    )


def test_missing_owned_core_blocks_all_connector_process_creation(inputs) -> None:
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("spawn")
        raise AssertionError("must not spawn")

    with pytest.raises(connector.ConnectorOnlyError, match="ownership"):
        run(inputs, job=object(), owner_check=lambda: False, probe=forbidden, popen=forbidden)
    assert calls == []


def test_unauthenticated_401_probe_is_required_after_owner_proof(inputs) -> None:
    checks = []

    def owner() -> bool:
        checks.append("owner")
        return True

    def probe(port: int, hostname: str) -> bool:
        assert port == 19876
        assert hostname == "core.example.test"
        checks.append("probe")
        return False

    def forbidden(*args, **kwargs):
        raise AssertionError("must not spawn")

    with pytest.raises(connector.ConnectorOnlyError, match="unauthenticated"):
        run(inputs, job=object(), owner_check=owner, probe=probe, popen=forbidden)
    assert checks == ["owner", "probe"]


def test_connector_only_launches_cloudflared_after_verified_core(inputs) -> None:
    events = []

    class Job:
        def assign(self, process) -> None:
            events.append("job.assign")

        def resume(self, process) -> None:
            events.append("job.resume")

    class Process:
        polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls == 1 else 0

        def wait(self):
            events.append("connector.wait")
            return 0

    def popen(argv, **kwargs):
        events.append("connector.spawn")
        assert argv[0] == str(inputs[0])
        assert argv[1:5] == ["tunnel", "--no-autoupdate", "run", "--token-file"]
        assert argv[5] == str(inputs[1])
        assert kwargs["cwd"] == inputs[0].parent
        assert kwargs["env"].get("CONTROL_PLANE_API_KEY") is None
        return Process()

    assert (
        run(
            inputs,
            job=Job(),
            owner_check=lambda: events.append("owner") or True,
            probe=lambda *_: events.append("probe") or True,
            popen=popen,
        )
        == 2
    )
    assert events == [
        "owner",
        "probe",
        "connector.spawn",
        "job.assign",
        "job.resume",
        "connector.wait",
    ]


def test_invalid_port_and_missing_binary_refuse_before_owner_check(inputs) -> None:
    exe, token = inputs

    def reject(*args, **kwargs):
        raise AssertionError("must not check or spawn")

    with pytest.raises(connector.ConnectorOnlyError, match="port"):
        connector.run_connector_generation(
            cloudflared=exe,
            token_file=token,
            core_port=0,
            core_hostname="core.example.test",
            job=object(),
            stdout=io.BytesIO(),
            stderr=io.BytesIO(),
            verify_core_owner=reject,
        )
    with pytest.raises(connector.ConnectorOnlyError, match="inputs"):
        connector.run_connector_generation(
            cloudflared=exe.with_name("missing.exe"),
            token_file=token,
            core_port=19876,
            core_hostname="core.example.test",
            job=object(),
            stdout=io.BytesIO(),
            stderr=io.BytesIO(),
            verify_core_owner=reject,
        )


def test_cloudflare_generation_rechecks_core_owner_during_running_session(
    inputs,
) -> None:
    events = []
    times = iter((0.0, 6.0))

    class FakeJob:
        def assign(self, process) -> None:
            events.append("assigned")

        def resume(self, process) -> None:
            events.append("resumed")

    class FakeProcess:
        def poll(self):
            return None

    def owner() -> bool:
        events.append("owner")
        return events.count("owner") == 1

    with pytest.raises(connector.ConnectorOnlyError, match="owner changed"):
        connector.run_connector_generation(
            cloudflared=inputs[0],
            token_file=inputs[1],
            core_port=19876,
            core_hostname="core.example.test",
            job=FakeJob(),
            stdout=io.BytesIO(),
            stderr=io.BytesIO(),
            verify_core_owner=owner,
            probe=lambda *_: True,
            popen=lambda *args, **kwargs: FakeProcess(),
            clock=lambda: next(times),
            sleep=lambda _: None,
        )
    assert events == ["owner", "assigned", "resumed", "owner"]


def test_connector_module_does_not_launch_core_or_bridge() -> None:
    source = Path(connector.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "serverfs_mcp.cli",
        "run_cloudflare_generation(",
        "serverfs_mcp.supervisor",
        "WindowsJob(",
        "run_with_agent(",
        '"origin"',
        '"bridge"',
    ):
        assert forbidden not in source
