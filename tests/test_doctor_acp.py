"""Static ACP profile checks in ``serverfs doctor``."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp import agent_doctor
from serverfs_mcp.agent_doctor import _probe_runtimes
from serverfs_mcp.native_config import NativeAcpProfile, NativeAgentSettings


class _Report:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, str]] = []

    def note(self, label: str, detail: str) -> None:
        self.lines.append((label, "note", detail))

    def status(self, label: str, level: str, detail: str = "") -> None:
        self.lines.append((label, level, detail))

    def text(self) -> str:
        return "\n".join(" ".join(line) for line in self.lines)


def _settings(*profiles: NativeAcpProfile):
    return SimpleNamespace(agent=NativeAgentSettings(acp_profiles=profiles))


def test_no_acp_profiles_preserves_existing_runtime_report() -> None:
    report = _Report()
    _probe_runtimes(report, _settings())
    assert report.lines == [
        ("agent codex", "note", "disabled"),
        ("agent claude", "note", "disabled"),
        ("agent qoder", "note", "disabled"),
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="Linux/POSIX executable-bit diagnostic")
def test_valid_profile_is_reported_without_launching_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "acp-secret-command"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)

    def forbidden_launch(*_args, **_kwargs):
        raise AssertionError("doctor launched an ACP command")

    monkeypatch.setattr(agent_doctor.subprocess, "run", forbidden_launch)
    report = _Report()
    _probe_runtimes(
        report,
        _settings(NativeAcpProfile("demo", str(executable), ("secret-arg",), use_proxy=True)),
    )

    assert ("agent acp-demo", "OK", "configured, executable locally present, use_proxy=true") in (
        report.lines
    )
    assert str(executable) not in report.text()
    assert "secret-arg" not in report.text()


@pytest.mark.skipif(sys.platform == "win32", reason="Linux/POSIX executable-bit diagnostic")
@pytest.mark.parametrize("kind", ["missing", "not-executable", "directory"])
def test_unavailable_profile_command_is_warned_without_path_leakage(
    tmp_path: Path, kind: str
) -> None:
    command = tmp_path / "private-acp-command"
    if kind == "not-executable":
        command.write_text("data", encoding="utf-8")
        command.chmod(0o600)
    elif kind == "directory":
        command.mkdir()

    report = _Report()
    _probe_runtimes(report, _settings(NativeAcpProfile("private", str(command))))

    assert (
        "agent acp-private",
        "WARN",
        "configured, executable unavailable, use_proxy=false",
    ) in report.lines
    assert str(tmp_path) not in report.text()
    assert "private-acp-command" not in report.text()
