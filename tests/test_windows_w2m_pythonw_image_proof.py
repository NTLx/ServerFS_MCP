"""Synthetic tests for W2M dedicated PythonW kernel identity probe.

No real pythonw, Task Scheduler, service, provider, credential or HTTP.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def _probe():
    file = (
        Path(__file__).resolve().parents[1] / "deployment/windows/v0250-w2m-pythonw-image-proof.py"
    )
    spec = importlib.util.spec_from_file_location("w2m_pythonw_test", file)
    assert spec and spec.loader
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


@pytest.mark.parametrize("kernel_at_base", [True, False])
def test_child_proves_real_kernel_image_without_leaking_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kernel_at_base: bool
) -> None:
    source = _probe()
    venv = tmp_path / "core-owner-env"
    launcher = venv / "Scripts" / "pythonw.exe"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"synthetic-venv-redirector")
    base = tmp_path / "base" / "pythonw.exe"
    base.parent.mkdir()
    base.write_bytes(b"synthetic-base-pythonw")

    monkeypatch.setattr(source, "_private_pythonw", lambda: (venv, launcher))
    monkeypatch.setattr(source.sys, "prefix", str(venv))
    monkeypatch.setattr(source.sys, "executable", str(launcher))
    monkeypatch.setattr(source.sys, "_base_executable", str(base), raising=False)

    import serverfs_mcp.windows_process as windows_process

    image = base if kernel_at_base else tmp_path / "unrelated.exe"
    monkeypatch.setattr(
        windows_process,
        "process_identity",
        lambda _pid: SimpleNamespace(image_path=image),
    )
    output = tmp_path / "boolean-only.json"
    assert source._child(output) == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert set(data) == source._FIELDS
    assert all(type(value) is bool for value in data.values())
    assert data["dedicated_venv_prefix"] is True
    assert data["sys_executable_is_private_pythonw_launcher"] is True
    assert data["base_pythonw_executable_exists"] is True
    assert data["base_pythonw_is_distinct_from_venv_launcher"] is True
    assert data["kernel_image_is_base_pythonw_executable"] is kernel_at_base
    assert data["kernel_image_is_private_pythonw_launcher"] is False
    text = output.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert "synthetic-" not in text
    assert not output.with_name(output.name + ".tmp").exists()


def test_source_cannot_start_core_or_mutate_scheduled_tasks() -> None:
    src = (
        Path(__file__).resolve().parents[1] / "deployment/windows/v0250-w2m-pythonw-image-proof.py"
    ).read_text(encoding="utf-8")
    shell = (
        Path(__file__).resolve().parents[1] / "deployment/windows/v0250-w2m-pythonw-image-proof.ps1"
    ).read_text(encoding="utf-8")
    assert "pythonw.exe" in src
    assert "with WindowsJob() as job:" in src
    assert "process = _launch(" in src
    assert "job.close()" in src
    assert "process.wait(timeout=15)" in src
    assert "CREATE_SUSPENDED" not in src  # delegated to reviewed _launch
    assert 'getattr(sys, "_base_executable", None)' in src
    assert "serverfs_mcp.windows_core_service" not in src
    assert "windows_core_control" not in src
    assert "Get-ScheduledTask" in shell
    for code in (
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Enable-ScheduledTask",
        "Disable-ScheduledTask",
        "Unregister-ScheduledTask",
    ):
        assert code not in shell


def test_main_rejects_unsupported_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _probe()
    import sys

    if sys.platform != "win32":
        pytest.skip("Windows-only main entrypoint guard")
    assert probe.main(["--other-mode"]) == 2


@pytest.mark.parametrize("timeout", [False, True])
def test_parent_contains_venv_redirector_in_fresh_job_and_reaps_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: bool
) -> None:
    source = _probe()
    repo = tmp_path / "test-checkout"
    script = repo / "deployment/windows/v0250-w2m-pythonw-image-proof.py"
    script.parent.mkdir(parents=True)
    (repo / "scratchpad").mkdir()
    diagnostics = repo / "docs/diagnostics"
    diagnostics.mkdir(parents=True)
    pythonw = tmp_path / "private-venv/Scripts/pythonw.exe"
    monkeypatch.setattr(source, "__file__", str(script))
    monkeypatch.setattr(source, "_private_pythonw", lambda: (pythonw.parent.parent, pythonw))
    import serverfs_mcp.windows_cloudflare_runtime as cloudflare_runtime
    import serverfs_mcp.windows_job as windows_job

    log = []

    class FakeJob:
        def __enter__(self):
            log.append("job-open")
            return self

        def close(self):
            log.append("job-close")

        def __exit__(self, *_args):
            self.close()

    class FakeProcess:
        def wait(self, timeout):
            log.append(f"wait-{timeout}")
            if timeout == 15 and timeout_mode:
                raise subprocess.TimeoutExpired("synthetic-pythonw", timeout)
            return 0

    timeout_mode = timeout

    def fake_launch(executable, args, *, env, cwd, job, stdout, stderr, popen):
        assert executable == pythonw
        assert args[:2] == ["-I", "-B"]
        assert args[3] == "--child"
        assert cwd == repo
        assert job is not None
        assert popen is subprocess.Popen
        assert stdout == subprocess.DEVNULL
        assert stderr == subprocess.DEVNULL
        assert env.get("LOCALAPPDATA") is None or isinstance(env["LOCALAPPDATA"], str)
        log.append("child-launched")
        if not timeout_mode:
            Path(args[-1]).write_text(
                json.dumps({name: True for name in source._FIELDS}),
                encoding="utf-8",
            )
        return FakeProcess()

    monkeypatch.setattr(windows_job, "WindowsJob", FakeJob)
    monkeypatch.setattr(cloudflare_runtime, "_launch", fake_launch)
    rc = source._parent()
    assert rc == (2 if timeout else 0)
    assert log[:2] == ["job-open", "child-launched"]
    assert "job-close" in log
    assert log.count("job-close") >= 1
    assert not list((repo / "scratchpad").iterdir())
    reports = list(diagnostics.glob("v0250-windows-w2m-pythonw-image-*.md"))
    assert len(reports) == 1
    report = reports[0].read_text(encoding="utf-8")
    assert ("Overall: INCOMPLETE" if timeout else "Overall: OBSERVED") in report
    assert str(pythonw) not in report
    assert str(repo) not in report
