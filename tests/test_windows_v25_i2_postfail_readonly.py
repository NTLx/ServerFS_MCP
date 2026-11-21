"""I2 postfail read-only diagnostic contracts; never query host Scheduler."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path
from types import SimpleNamespace

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment" / "windows" / "v0250-i2-postfail-readonly.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i2_postfail_readonly", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_i2_metadata_probe_distinguishes_absence_private_and_unknown(tmp_path: Path) -> None:
    module = _module()
    probe = tmp_path / "artifact"
    fake = SimpleNamespace(check_private=lambda path, **kwargs: None)
    assert module.classify_private(fake, probe, directory=False, sid="fake") == "ABSENT"
    probe.write_bytes(b"nonsecret")
    assert module.classify_private(fake, probe, directory=False, sid="fake") == "PRIVATE"
    fake.check_private = lambda path, **kwargs: 1 / 0
    assert module.classify_private(fake, probe, directory=False, sid="fake") == "UNVERIFIED"


def test_i2_code_parity_is_readonly_and_fails_closed(tmp_path: Path, monkeypatch) -> None:
    from serverfs_mcp import windows_cloudflare_security

    module = _module()
    monkeypatch.setattr(windows_cloudflare_security, "_no_reparse_components", lambda p: None)
    installed = tmp_path / "installed.py"
    source = tmp_path / "source.py"
    fake = SimpleNamespace(check_private=lambda path, **kwargs: None)
    assert module.classify_installed_code(fake, installed, source, sid="fixture") == "UNVERIFIED"
    installed.write_bytes(b"same-code")
    source.write_bytes(b"same-code")
    assert module.classify_installed_code(fake, installed, source, sid="fixture") == "MATCHES"
    source.write_bytes(b"updated-code")
    assert module.classify_installed_code(fake, installed, source, sid="fixture") == "DIFFERS"
    fake.check_private = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError())
    assert module.classify_installed_code(fake, installed, source, sid="fixture") == "UNVERIFIED"


def test_original_wheel_delta_classifies_only_expected_changed_module(
    tmp_path: Path, monkeypatch
) -> None:
    from serverfs_mcp import windows_cloudflare_security

    module = _module()
    monkeypatch.setattr(windows_cloudflare_security, "_no_reparse_components", lambda p: None)
    base = tmp_path / "instance"
    repo = tmp_path / "repo"
    wheel_dir = base / "build" / "core-owner"
    wheel_dir.mkdir(parents=True)
    owner = "serverfs_mcp/windows_v25/bridge_v2_owner.py"
    other = "serverfs_mcp/windows_v25/runtime.py"
    for name, content in ((owner, b"old"), (other, b"stable")):
        file = repo / "src" / Path(name)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
    with zipfile.ZipFile(wheel_dir / "serverfs_mcp-0.25.0-py3-none-any.whl", "w") as archive:
        archive.writestr(owner, b"old")
        archive.writestr(other, b"stable")
    fake = SimpleNamespace(check_private=lambda *args, **kwargs: None)
    assert module.classify_original_wheel(fake, base, repo, sid="fake") == "MATCHES"
    (repo / "src" / Path(owner)).write_bytes(b"new")
    assert module.classify_original_wheel(fake, base, repo, sid="fake") == "BRIDGE_OWNER_ONLY"
    (repo / "src" / Path(other)).write_bytes(b"also-new")
    assert module.classify_original_wheel(fake, base, repo, sid="fake") == "MULTIPLE"


def test_i2_task_inventory_requires_exact_disabled_identity() -> None:
    module = _module()
    target = "ServerFS v25 i2-smoke Agent Bridge"
    assert module.classify_task([], target) == "ABSENT"
    row = {"name": target, "state": "Disabled", "enabled": False}
    assert module.classify_task([row], target) == "DISABLED"
    assert module.classify_task([row, row], target) == "AMBIGUOUS"
    assert module.classify_task([{**row, "state": "Running"}], target) == "UNEXPECTED"
    assert module.classify_task([{**row, "enabled": True}], target) == "UNEXPECTED"
    assert module.classify_task([{**row, "enabled": "false"}], target) == "UNVERIFIED"


def test_i2_postfail_source_never_reads_private_grants_or_mutates() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    for forbidden in (
        "read_bytes(",
        "read_text(",
        "json.load(",
        "secret_hex",
        "storage.stage(",
        "storage.prepare(",
        "Scheduler().install(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "Stop-Process",
        "Remove-Item",
        "Set-Acl",
        "git push",
    ):
        assert forbidden not in source
    assert "I2_POSTFAIL_" in source
    assert "I2_POSTFAIL_READONLY=PASS" in source
    assert "I2_POSTFAIL_PRODUCTION_TASKS=EXPECTED" in source
    assert 'storage.component_inventory("bridge")' in source
    assert "classify_private(storage, path, directory=directory, sid=sid)" in source
