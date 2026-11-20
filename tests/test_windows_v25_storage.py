"""Synthetic private-directory preparation and CLI contract for Windows v25.

Never modifies a real Windows profile, scheduled task or service.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp import windows_private_artifacts
from serverfs_mcp.windows_v25 import cli, storage
from serverfs_mcp.windows_v25.spec import ManifestError


def test_prepare_creates_only_private_user_scoped_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appdata = tmp_path / "LocalAppData"
    appdata.mkdir()
    root = appdata / "ServerFS" / "v25"
    protected: list[Path] = []
    checked: list[Path] = []
    sid = "S-1-5-21-111-222-333-1001"

    monkeypatch.setattr(storage, "_windows", lambda: None)
    monkeypatch.setattr(storage, "_sid", lambda: sid)
    monkeypatch.setattr(storage, "root", lambda: root)

    def protect(path: Path, owner: str) -> None:
        assert owner == sid
        assert path.is_dir()
        protected.append(path)

    def check(path: Path, *, directory: bool, sid: str) -> None:
        assert sid == "S-1-5-21-111-222-333-1001"
        assert directory and path.is_dir()
        checked.append(path)

    monkeypatch.setattr(windows_private_artifacts, "protect_new_file", protect)
    monkeypatch.setattr(storage, "check_private", check)

    expected = [
        appdata / "ServerFS",
        root,
        root / "services",
        root / "services" / "core",
        root / "envs",
        root / "envs" / "core",
    ]
    storage.prepare("core")
    assert protected == expected
    assert all(path.is_dir() for path in expected)

    storage.prepare("core")
    assert protected == expected  # No re-ACL of existing directories.
    assert checked


def test_prepare_refuses_existing_unverified_service_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appdata = tmp_path / "LocalAppData"
    root = appdata / "ServerFS" / "v25"
    (root / "services" / "core").mkdir(parents=True)

    monkeypatch.setattr(storage, "_windows", lambda: None)
    monkeypatch.setattr(storage, "_sid", lambda: "S-1-5-21-111-222-333-1001")
    monkeypatch.setattr(storage, "root", lambda: root)

    def refuse(*_args, **_kwargs) -> None:
        raise storage.StorageError("unsafe directory")

    monkeypatch.setattr(storage, "check_private", refuse)
    with pytest.raises(storage.StorageError, match="unsafe directory"):
        storage.prepare("core")
    assert not (root / "envs").exists()


def test_prepare_cli_does_not_load_manifest_or_accept_runtime_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    monkeypatch.setattr(storage, "prepare", called.append)
    monkeypatch.setattr(
        storage, "load", lambda _component: pytest.fail("prepare must not load manifest")
    )
    result = cli.dispatch(cli.parser().parse_args(["core", "prepare"]))
    assert result == {"component": "core", "prepared": True}
    assert called == ["core"]
    args = cli.parser().parse_args(["core", "prepare", "--port", "18767"])
    with pytest.raises(ManifestError, match="only for configure"):
        cli.dispatch(args)
