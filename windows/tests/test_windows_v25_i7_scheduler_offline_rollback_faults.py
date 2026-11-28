"""Deterministic I7 dual-owner offline Wheel rollback fault injection.

Exercise the actual one-use rollout control flow with a simulated uv runner.
The test never creates/mutates a Scheduler task, private Windows venv or
Cloudflare resource. It proves both attempted owners are restored in reverse
order even if an install command fails after partially updating its package.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i7-scheduler-offline-rollout.py"
)
spec = importlib.util.spec_from_file_location("i7_fault_inject_rollout", SCRIPT)
assert spec is not None and spec.loader is not None
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)

MODULE = "windows_v25/tasks.py"


class FakeWheelGate:
    REPO = Path("checked-repo")

    def __init__(self, old: dict[str, Path]) -> None:
        self.old = old
        self.baseline_checks = 0

    def helper(self, _name: str, _label: str):
        return self.helper_object

    def installed_baseline(self):
        self.baseline_checks += 1

    def archived_modules(self, wheel: Path, names: set[str]) -> dict[str, bytes]:
        assert names == {MODULE}
        for owner, path in self.old.items():
            if wheel == path:
                return {MODULE: b"old-" + owner.encode()}
        raise AssertionError("not a retained old Wheel")


class FakeInstaller:
    REPO = FakeWheelGate.REPO

    def __init__(self, *, installed: dict[str, bytes], fail_on: str | None) -> None:
        self.installed = installed
        self.fail_on = fail_on
        self.actions: list[tuple[str, str]] = []
        self.protection_count = 0

    def _new_dir(self, path: Path, _sid: str) -> None:
        path.mkdir()

    def _protect_tree(self, _path: Path, _sid: str) -> None:
        self.protection_count += 1

    def run(self, argv, *, env=None, timeout=0):
        assert argv[:3] == ["uv", "pip", "install"]
        assert "--offline" in argv and "--no-deps" in argv and "--reinstall" in argv
        assert env["UV_LINK_MODE"] == "copy"
        assert timeout in (90, 150)
        py = Path(argv[argv.index("--python") + 1])
        component = py.parents[1].name
        assert component in ("core", "cloudflare")
        if "--dry-run" in argv:
            self.actions.append(("dry-run", component))
            return b""
        wheel = Path(argv[-1])
        is_old = wheel.name.startswith("old-")
        self.actions.append(("restore" if is_old else "install", component))
        if is_old:
            self.installed[component] = b"old-" + component.encode()
        elif self.fail_on == component:
            self.installed[component] = b"partially-installed"
            raise RuntimeError("simulated installer failure, no secret")
        else:
            self.installed[component] = b"new"
        return b""


@pytest.fixture
def setup(tmp_path: Path, monkeypatch):
    (tmp_path / "build").mkdir()
    old = {component: tmp_path / ("old-" + component + ".whl") for component in rollout.COMPONENTS}
    candidate = tmp_path / "candidate.whl"
    current = {comp: b"old-" + comp.encode() for comp in rollout.COMPONENTS}
    modules = {MODULE: b"new"}
    wheel_gate = FakeWheelGate(old)
    stub = type("OwnerGate", (), {"postflight": lambda self: None})()
    monkeypatch.setattr(
        rollout,
        "stage_inputs",
        lambda: (wheel_gate, tmp_path, "test-sid", modules, old, candidate, stub),
    )
    monkeypatch.setattr(
        rollout,
        "package_files",
        lambda env, names, sid: {MODULE: current[env.name]},
    )
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--stage"])
    return wheel_gate, current, old, candidate, stub


@pytest.mark.parametrize("fail_on", ("core", "cloudflare"))
def test_failed_or_partial_install_restores_exact_attempted_owners_in_reverse_order(
    setup, monkeypatch, capsys, fail_on: str
) -> None:
    wheel_gate, current, old, _candidate, _stub = setup
    helper = FakeInstaller(installed=current, fail_on=fail_on)
    wheel_gate.helper_object = helper
    with pytest.raises(rollout.I7OfflineRolloutError):
        rollout.run()
    assert all(current[comp] == b"old-" + comp.encode() for comp in rollout.COMPONENTS)
    assert [(action, component) for action, component in helper.actions if action == "restore"] == (
        [("restore", "core")]
        if fail_on == "core"
        else [("restore", "cloudflare"), ("restore", "core")]
    )
    assert setup[0].baseline_checks == 1
    assert setup[0] is wheel_gate
    assert setup[1] is current
    assert setup[2] is old
    assert "I7_TWO_OWNER_OLD_WHEEL_ROLLBACK=VERIFIED" in capsys.readouterr().out


def test_success_stays_on_new_wheel_without_rollback(setup, monkeypatch, capsys) -> None:
    wheel_gate, current, _old, _candidate, _stub = setup
    helper = FakeInstaller(installed=current, fail_on=None)
    wheel_gate.helper_object = helper
    postflight_called = []
    monkeypatch.setattr(rollout, "installed_postflight", lambda: postflight_called.append(True))
    rollout.run()
    assert current == {"core": b"new", "cloudflare": b"new"}
    assert postflight_called == [True]
    assert not any(action == "restore" for action, _ in helper.actions)
    assert "I7_SCHEDULER_ROLLOUT_TWO_OWNERS_OFFLINE=PASS" in capsys.readouterr().out
