"""I5 single-owner offline Wheel installation failure injection (no OS changes)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i5-owner-seven-module-offline-rollout.py"
)
spec = importlib.util.spec_from_file_location("i5_offline_rollback_fault", SCRIPT)
assert spec is not None and spec.loader is not None
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)

SOURCE = {"windows_v25/tasks.py": b"new"}


class FakeGate:
    REPO = Path("expected-repository")

    def __init__(self, current: dict[str, bytes]) -> None:
        self.current = current
        self.check_count = 0

    def native_preflight(self) -> None:
        self.check_count += 1
        assert self.current == {"windows_v25/tasks.py": b"old"}


class FakeBuilder:
    REPO = FakeGate.REPO

    def __init__(self, current: dict[str, bytes], failure: str | None) -> None:
        self.current = current
        self.failure = failure
        self.calls: list[str] = []
        self.protected = 0

    def _new_dir(self, folder: Path, sid: str) -> None:
        assert sid == "fixture-sid"
        folder.mkdir()

    def _protect_tree(self, _folder: Path, _sid: str) -> None:
        self.protected += 1

    def run(self, args, *, env=None, timeout=0):
        assert args[:3] == ["uv", "pip", "install"]
        assert "--offline" in args and "--reinstall" in args and "--no-deps" in args
        assert env["UV_LINK_MODE"] == "copy"
        if "--dry-run" in args:
            self.calls.append("dry-run")
            return b""
        if str(args[-1]).endswith("old-owner.whl"):
            self.calls.append("restore")
            self.current["windows_v25/tasks.py"] = b"old"
        else:
            self.calls.append("install")
            self.current["windows_v25/tasks.py"] = (
                b"partial" if self.failure == "install" else b"new"
            )
            if self.failure == "install":
                raise RuntimeError("simulated partial uv failure")
        return b""


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    (tmp_path / "build").mkdir()
    installed = {"windows_v25/tasks.py": b"old"}
    old = tmp_path / "old-owner.whl"
    new = tmp_path / "candidate.whl"
    venv = tmp_path / "envs" / "bridge"
    py = venv / "Scripts" / "python.exe"
    gate = FakeGate(installed)
    monkeypatch.setattr(
        rollout,
        "preflight",
        lambda: (
            gate,
            object(),
            tmp_path,
            venv,
            "fixture-sid",
            SOURCE,
            dict(installed),
            old,
            new,
            py,
        ),
    )
    monkeypatch.setattr(rollout, "current_modules", lambda _env, _source, _sid: dict(installed))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--stage"])
    return gate, installed


@pytest.mark.parametrize("failure", ("install", "postflight"))
def test_partial_failure_restores_verified_old_owner(
    env, monkeypatch, capsys, failure: str
) -> None:
    gate, installed = env
    builder = FakeBuilder(installed, "install" if failure == "install" else None)
    monkeypatch.setattr(rollout, "helper", lambda _file, _label: builder)
    if failure == "postflight":
        monkeypatch.setattr(
            rollout,
            "postflight",
            lambda: (_ for _ in ()).throw(RuntimeError("simulated import failure")),
        )
    with pytest.raises(rollout.I5OwnerRolloutError):
        rollout.main()
    assert installed == {"windows_v25/tasks.py": b"old"}
    assert builder.calls == (["dry-run", "install", "restore"])
    assert gate.check_count == 2
    assert "I5_OWNER_OLD_WHEEL_ROLLBACK=VERIFIED" in capsys.readouterr().out


def test_success_never_uses_retained_wheel(env, monkeypatch, capsys) -> None:
    gate, installed = env
    builder = FakeBuilder(installed, None)
    monkeypatch.setattr(rollout, "helper", lambda _file, _label: builder)
    hits = []
    monkeypatch.setattr(rollout, "postflight", lambda: hits.append(True))
    rollout.main()
    assert hits == [True]
    assert installed == {"windows_v25/tasks.py": b"new"}
    assert builder.calls == ["dry-run", "install"]
    assert gate.check_count == 1
    assert "I5_OWNER_ONLY_OFFLINE_ROLLOUT=PASS" in capsys.readouterr().out
