"""Windows Cloudflare generation tests — no services or network are started."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from serverfs_mcp import windows_cloudflare_runtime as runtime


class FakeJob:
    def __init__(self, fail_assign: bool = False):
        self.assigned = []
        self.resumed = []
        self.fail_assign = fail_assign

    def assign(self, process):
        if self.fail_assign:
            raise OSError("assign denied")
        self.assigned.append(process)

    def resume(self, process):
        self.resumed.append(process)


class FakeProcess:
    def __init__(self, role, *, origin_exits=False):
        self.role = role
        self.origin_exits = origin_exits
        self.killed = False

    def poll(self):
        if self.role == "origin":
            return 1 if self.origin_exits else None
        return 1

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 1


@pytest.fixture
def scenario(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runtime, "_is_windows", lambda: True)
    python = tmp_path / "pythonw.exe"
    config = tmp_path / "serverfs.toml"
    cloudflared = tmp_path / "cloudflared.exe"
    token = tmp_path / "connector.token"
    for path in (python, config, cloudflared, token):
        path.write_bytes(b"fixture")
    return dict(
        python_executable=python,
        config_path=config,
        cloudflared_executable=cloudflared,
        token_file=token,
        http_port=18765,
        origin_environment={
            "SERVERFS_HTTP_API_KEY": "fixture-key",
            "SERVERFS_HTTP_ALLOWED_HOSTS": "example.invalid",
        },
        origin_stdout=io.BytesIO(),
        origin_stderr=io.BytesIO(),
        connector_stdout=io.BytesIO(),
        connector_stderr=io.BytesIO(),
    )


def test_launches_http_then_cloudflare_in_one_job_without_raw_token(scenario):
    launches = []
    started = []
    job = FakeJob()

    def popen(args, **kwargs):
        role = "origin" if "serverfs_mcp.cli" in args else "cloudflared"
        launches.append((args, kwargs))
        return FakeProcess(role)

    outcome = runtime.run_cloudflare_generation(
        **scenario,
        job=job,
        popen=popen,
        on_child_started=lambda role, process: started.append(role),
        probe=lambda port, hostname: port == 18765 and hostname == "example.invalid",
        sleep=lambda _: None,
    )
    assert outcome == 2
    assert len(launches) == 2
    assert len(job.assigned) == len(job.resumed) == 2
    assert started == ["origin", "connector"]
    origin_args, origin_kwargs = launches[0]
    connector_args, connector_kwargs = launches[1]
    assert origin_args[0] == str(scenario["python_executable"])
    assert "--transport" in origin_args and "streamable-http" in origin_args
    assert connector_args[:4] == [
        str(scenario["cloudflared_executable"]),
        "tunnel",
        "--no-autoupdate",
        "run",
    ]
    assert connector_args[-2:] == ["--token-file", str(scenario["token_file"])]
    assert all("fixture-key" not in " ".join(args) for args, _ in launches)
    assert origin_kwargs["env"]["SERVERFS_HTTP_API_KEY"] == "fixture-key"
    assert "SERVERFS_HTTP_API_KEY" not in connector_kwargs["env"]
    assert origin_kwargs["creationflags"] & runtime.CREATE_SUSPENDED


def test_does_not_launch_connector_until_origin_authenticated(scenario):
    calls = []

    def popen(args, **kwargs):
        calls.append(args)
        return FakeProcess("origin")

    ticks = iter([0.0, 0.0, 15.1])
    with pytest.raises(runtime.WindowsCloudflareRuntimeError, match="readiness"):
        runtime.run_cloudflare_generation(
            **scenario,
            job=FakeJob(),
            popen=popen,
            probe=lambda _port, _hostname: False,
            clock=lambda: next(ticks),
            sleep=lambda _: None,
        )
    assert len(calls) == 1


def test_fails_closed_on_origin_premature_exit(scenario):
    calls = []

    def popen(args, **kwargs):
        calls.append(args)
        return FakeProcess("origin", origin_exits=True)

    with pytest.raises(runtime.WindowsCloudflareRuntimeError, match="origin exited"):
        runtime.run_cloudflare_generation(
            **scenario,
            job=FakeJob(),
            popen=popen,
            probe=lambda _port, _hostname: True,
            clock=lambda: 0.0,
            sleep=lambda _: None,
        )
    assert len(calls) == 1


def test_requires_explicit_http_auth(scenario):
    scenario["origin_environment"] = {"SERVERFS_HTTP_ALLOWED_HOSTS": "example.invalid"}
    with pytest.raises(runtime.WindowsCloudflareRuntimeError, match="API Key or OAuth"):
        runtime.run_cloudflare_generation(
            **scenario,
            job=FakeJob(),
            popen=lambda *a, **k: pytest.fail("spawned"),
        )


def test_refuses_bad_port(scenario):
    scenario["http_port"] = 0
    with pytest.raises(runtime.WindowsCloudflareRuntimeError, match="loopback port"):
        runtime.run_cloudflare_generation(
            **scenario,
            job=FakeJob(),
            popen=lambda *a, **k: pytest.fail("spawned"),
        )


def test_uncontained_process_is_terminated(scenario):
    process = FakeProcess("origin")
    process.poll = lambda: None
    with pytest.raises(runtime.WindowsCloudflareRuntimeError, match="generation could not start"):
        runtime.run_cloudflare_generation(
            **scenario,
            job=FakeJob(fail_assign=True),
            popen=lambda *a, **k: process,
            probe=lambda _port, _hostname: True,
        )
    assert process.killed


def test_readiness_probe_uses_public_hostname_on_loopback(monkeypatch):
    from urllib.error import HTTPError

    observed = []

    class ProbeOpener:
        def open(self, request, timeout):
            observed.append((request.full_url, request.get_header("Host"), timeout))
            raise HTTPError(request.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda handler: ProbeOpener())
    assert runtime._probe_unauthenticated_origin(18765, "example.invalid")
    assert observed == [("http://127.0.0.1:18765/mcp", "example.invalid", 1)]


def test_readiness_probe_rejects_unauthenticated_success(monkeypatch):
    class Response:
        def close(self):
            pass

    class ProbeOpener:
        def open(self, request, timeout):
            return Response()

    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda handler: ProbeOpener())
    assert not runtime._probe_unauthenticated_origin(18765, "example.invalid")


def test_refuses_unsupported_host(scenario, monkeypatch):
    monkeypatch.setattr(runtime, "_is_windows", lambda: False)
    with pytest.raises(runtime.WindowsCloudflareRuntimeError, match="requires Windows"):
        runtime.run_cloudflare_generation(
            **scenario,
            job=FakeJob(),
            popen=lambda *a, **k: pytest.fail("spawned"),
        )
