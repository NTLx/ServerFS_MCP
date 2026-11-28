"""Safety regression tests for the disposable native Connector fixture."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_DEPLOYMENT = Path(__file__).resolve().parents[1] / "deployment/macos"
if str(_DEPLOYMENT) not in sys.path:
    sys.path.insert(0, str(_DEPLOYMENT))

from v0250_file_only_multi_connector_acceptance import _protected_snapshot  # noqa: E402


def _inventory(monkeypatch, stdout: str, *, code: int = 0):
    def run(*args, **kwargs):
        assert args[0] == ["launchctl", "list"]
        return subprocess.CompletedProcess(args[0], code, stdout, "")

    monkeypatch.setattr(subprocess, "run", run)


def test_named_and_inactive_jobs_are_protected(monkeypatch):
    _inventory(
        monkeypatch,
        "PID Status Label\n"
        "123 0 com.ntlx.serverfs.core.blue\n"
        "- 7 com.ntlx.serverfs.cloudflare.green\n"
        "456 0 com.ntlx.serverfs.core.fixture\n"
        "789 0 unrelated\n",
    )
    assert _protected_snapshot(frozenset({"com.ntlx.serverfs.core.fixture"})) == {
        "com.ntlx.serverfs.core.blue": (123, 0),
        "com.ntlx.serverfs.cloudflare.green": (None, 7),
    }
    assert len(_protected_snapshot()) == 3


@pytest.mark.parametrize(
    ("stdout", "code"),
    [
        ("", 1),
        ("not-a-pid 0 com.ntlx.serverfs.core.blue", 0),
        ("123 com.ntlx.serverfs.core.blue", 0),
        ("1 0 com.ntlx.serverfs.core.blue", 0),
        ("123 0 com.ntlx.serverfs.core.blue\n124 0 com.ntlx.serverfs.core.blue", 0),
    ],
)
def test_unknown_or_ambiguous_inventory_refuses(monkeypatch, stdout, code):
    _inventory(monkeypatch, stdout, code=code)
    with pytest.raises(RuntimeError):
        _protected_snapshot()
