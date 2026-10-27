from __future__ import annotations

import json
import runpy
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_TOOL = REPO_ROOT / "deployment" / "plugin" / "package_plugin.py"
PACKAGE_API = runpy.run_path(str(PACKAGE_TOOL))
EXPECTED_SKILLS = {
    "serverfs-install",
    "serverfs-configure",
    "serverfs-upgrade",
    "serverfs-diagnose",
    "serverfs-tunnel",
    "serverfs-auth",
    "serverfs-agent-bridge",
}


def _project_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PACKAGE_TOOL), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_skill_and_plugin_validation_passes() -> None:
    result = _run("validate")
    assert result.returncode == 0, result.stderr
    assert f"v{_project_version()}" in result.stdout


def test_release_tag_version_mismatch_fails_closed() -> None:
    result = _run("validate", "--tag", "v999.0.0")
    assert result.returncode != 0
    assert "does not match project/plugin version" in result.stderr


def test_plugin_archive_is_minimal_and_deterministic(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"

    first = _run("build", "--output-dir", str(first_dir))
    second = _run("build", "--output-dir", str(second_dir))
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr

    name = f"serverfs-plugin-v{_project_version()}.zip"
    first_zip = first_dir / name
    second_zip = second_dir / name
    assert first_zip.read_bytes() == second_zip.read_bytes()

    with zipfile.ZipFile(first_zip) as archive:
        members = set(archive.namelist())

    assert "serverfs/plugin.json" in members
    assert "serverfs/skills/README.md" not in members
    assert all(not member.startswith("serverfs/.codex-plugin/") for member in members)
    assert "serverfs/mcp.json" not in members

    packaged_skills = {
        parts[2]
        for member in members
        if (parts := Path(member).parts)[:2] == ("serverfs", "skills") and len(parts) >= 4
    }
    assert packaged_skills == EXPECTED_SKILLS
    for skill in EXPECTED_SKILLS:
        assert f"serverfs/skills/{skill}/SKILL.md" in members
    assert all(
        member == "serverfs/plugin.json"
        or (
            member.startswith("serverfs/skills/")
            and len(Path(member).parts) >= 4
            and Path(member).parts[2] in EXPECTED_SKILLS
        )
        for member in members
    )


@pytest.fixture
def plugin_source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    for name in ("pyproject.toml", "plugin.json"):
        shutil.copy2(REPO_ROOT / name, root / name)
    shutil.copytree(REPO_ROOT / "skills", root / "skills")
    return root


@pytest.mark.parametrize("newline", [b"\r\n", b"\r"])
def test_plugin_archive_is_canonical_across_hosts_and_newlines(
    plugin_source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, newline: bytes
) -> None:
    manifest_path = plugin_source / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["description"] += " — 中文 café e\u0301\u2028\u2029"
    manifest_path.write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())
    sources = [manifest_path, *sorted((plugin_source / "skills").rglob("*.md"))]
    for path in sources:
        content = path.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        if path.suffix == ".md":
            content += "\nUnicode: 中文 café e\u0301\u2028\u2029\n"
        path.write_bytes(content.encode("utf-8"))

    other_source = tmp_path / "other-source"
    shutil.copytree(plugin_source, other_source)
    for path in sources:
        (other_source / path.relative_to(plugin_source)).write_bytes(
            path.read_bytes().replace(b"\n", newline)
        )

    original_init = zipfile.ZipInfo.__init__
    host_system = 3

    def host_init(self: zipfile.ZipInfo, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)
        self.create_system = host_system

    monkeypatch.setattr(zipfile.ZipInfo, "__init__", host_init)
    first = PACKAGE_API["build_archive"](plugin_source, tmp_path / "first")
    host_system = 0  # Simulate the Windows ZipInfo default for the other checkout.
    second = PACKAGE_API["build_archive"](other_source, tmp_path / "second")
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as left, zipfile.ZipFile(second) as right:
        assert left.namelist() == right.namelist()
        for left_info, right_info in zip(left.infolist(), right.infolist(), strict=True):
            assert {slot: getattr(left_info, slot, None) for slot in zipfile.ZipInfo.__slots__} == {
                slot: getattr(right_info, slot, None) for slot in zipfile.ZipInfo.__slots__
            }
            assert left_info.create_system == 3
            assert left_info.external_attr == 0o100644 << 16
            assert left_info.date_time == (1980, 1, 1, 0, 0, 0)
            assert left_info.compress_type == zipfile.ZIP_DEFLATED
            content = left.read(left_info)
            source = plugin_source / Path(left_info.filename).relative_to("serverfs")
            assert content == right.read(right_info) == source.read_bytes()
            assert b"\r" not in content


@pytest.mark.parametrize(
    "relative",
    [
        "plugin.json",
        "skills/serverfs-install/SKILL.md",
        "skills/serverfs-auth/references/api-key.md",
    ],
)
def test_non_utf8_runtime_text_fails_closed(
    plugin_source: Path, tmp_path: Path, relative: str
) -> None:
    path = plugin_source / relative
    path.write_bytes(path.read_bytes() + b"\xff")
    with pytest.raises(UnicodeDecodeError):
        PACKAGE_API["build_archive"](plugin_source, tmp_path / "output")


def test_packaged_references_resolve_without_checkout(tmp_path: Path) -> None:
    output = PACKAGE_API["build_archive"](REPO_ROOT, tmp_path)
    extracted = tmp_path / "standalone"
    with zipfile.ZipFile(output) as archive:
        archive.extractall(extracted)
    root = extracted / "serverfs"
    assert not (root / "README.md").exists()
    assert not (root / "deployment").exists()
    expected_references = {
        "serverfs-upgrade": {"linux", "windows", "macos"},
        "serverfs-tunnel": {"linux", "windows", "macos"},
        "serverfs-auth": {"no-built-in-auth", "api-key", "oauth", "native-stdio"},
        "serverfs-agent-bridge": {"linux", "windows", "macos"},
    }
    expected_files = {Path("plugin.json")}
    for skill in EXPECTED_SKILLS:
        skill_root = root / "skills" / skill
        assert list(skill_root.rglob("SKILL.md")) == [skill_root / "SKILL.md"]
        expected_files.add(Path("skills") / skill / "SKILL.md")
        references = expected_references.get(skill, set())
        assert {p.stem for p in skill_root.glob("references/*.md")} == references
        for name in references:
            expected_files.add(Path("skills") / skill / "references" / f"{name}.md")
        for path in skill_root.rglob("*.md"):
            PACKAGE_API["_validate_markdown_links"](
                skill_root, path, path.read_text(encoding="utf-8")
            )
    assert {p.relative_to(root) for p in root.rglob("*") if p.is_file()} == expected_files
    assert (
        json.loads((root / "plugin.json").read_text(encoding="utf-8"))["version"]
        == _project_version()
    )


@pytest.mark.parametrize(
    ("relative", "content", "message"),
    [
        ("skills/extra/notes.md", "extra", "skill set mismatch"),
        ("skills/serverfs-install/references/SKILL.md", "nested", "unexpected Plugin runtime"),
        ("skills/serverfs-install/.env", "secret=value", "unexpected Plugin runtime"),
        ("skills/serverfs-install/mcp.json", "{}", "unexpected Plugin runtime"),
        ("skills/serverfs-install/.codex-plugin/plugin.json", "{}", "unexpected Plugin runtime"),
        ("skills/serverfs-install/README.md", "source spillover", "unexpected Plugin runtime"),
        ("skills/serverfs-install/source.py", "source spillover", "unexpected Plugin runtime"),
        (
            "skills/serverfs-install/references/bad.md",
            "[repo](../../../README.md)",
            "escapes packaged skill",
        ),
        (
            "skills/serverfs-install/references/bad.md",
            "[other](../../serverfs-auth/SKILL.md)",
            "escapes packaged skill",
        ),
        (
            "skills/serverfs-install/references/bad.md",
            "[missing](missing.md)",
            "broken packaged link",
        ),
        (
            "skills/serverfs-install/references/bad.md",
            "[repo][ref]\n\n[ref]: ../../../README.md",
            "escapes packaged skill",
        ),
        ("skills/serverfs-install/references/bad.md", "sk-" + "a" * 24, "credential token"),
        (
            "skills/serverfs-install/references/bad.md",
            "-----BEGIN PRIVATE KEY-----",
            "credential token",
        ),
        ("mcp.json", "{}", "must not bundle root mcp.json"),
        (".codex-plugin/plugin.json", "{}", "parallel .codex-plugin"),
    ],
)
def test_invalid_runtime_content_fails_closed(
    plugin_source: Path, relative: str, content: str, message: str
) -> None:
    path = plugin_source / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    with pytest.raises(PACKAGE_API["ValidationError"], match=message):
        PACKAGE_API["validate"](plugin_source)


def test_missing_skill_entry_fails(plugin_source: Path) -> None:
    (plugin_source / "skills/serverfs-install/SKILL.md").unlink()
    with pytest.raises(OSError):
        PACKAGE_API["validate"](plugin_source)


def _symlink_or_skip(target: Path, link: Path, *, target_is_directory: bool = False) -> None:
    """Create the symlink the case needs, or skip where the host refuses to create one.

    A non-elevated Windows session can be denied link creation outright (WinError 1314,
    ``ERROR_PRIVILEGE_NOT_HELD``). That is a capability of the test host, not a change in the
    packaged content, so the case is reported as skipped instead of requiring elevation. Where the
    link can be created, the fail-closed assertion below runs unchanged.
    """
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:  # ERROR_PRIVILEGE_NOT_HELD
            pytest.skip("this Windows session lacks the privilege to create symlinks")
        raise


@pytest.mark.parametrize("directory", [False, True])
def test_symlinks_fail_closed(plugin_source: Path, directory: bool) -> None:
    skill = plugin_source / "skills/serverfs-install"
    if directory:
        shutil.rmtree(skill)
        _symlink_or_skip(REPO_ROOT / "skills/serverfs-install", skill, target_is_directory=True)
    else:
        (skill / "references").mkdir()
        _symlink_or_skip(REPO_ROOT / "README.md", skill / "references/external.md")
    with pytest.raises(PACKAGE_API["ValidationError"], match="symlink"):
        PACKAGE_API["validate"](plugin_source)


def test_manifest_version_parity_fails_closed(plugin_source: Path) -> None:
    path = plugin_source / "plugin.json"
    manifest = json.loads(path.read_text())
    manifest["version"] = "999.0.0"
    path.write_text(json.dumps(manifest))
    with pytest.raises(PACKAGE_API["ValidationError"], match="does not match project"):
        PACKAGE_API["validate"](plugin_source)


def test_skills_remain_model_invoked(plugin_source: Path) -> None:
    path = plugin_source / "skills/serverfs-install/SKILL.md"
    path.write_text(path.read_text().replace("name:", "disable-model-invocation: true\nname:", 1))
    with pytest.raises(PACKAGE_API["ValidationError"], match="model-invoked"):
        PACKAGE_API["validate"](plugin_source)


def test_v023_repo_ops_skill_contract_is_explicit_and_non_agent() -> None:
    def text(relative: str) -> str:
        return (REPO_ROOT / relative).read_text(encoding="utf-8")

    configure = text("skills/serverfs-configure/SKILL.md")
    bridge = text("skills/serverfs-agent-bridge/SKILL.md")
    install = text("skills/serverfs-install/SKILL.md")
    upgrade = text("skills/serverfs-upgrade/SKILL.md")
    diagnose = text("skills/serverfs-diagnose/SKILL.md")
    tunnel_linux = text("skills/serverfs-tunnel/references/linux.md")
    auth_oauth = text("skills/serverfs-auth/references/oauth.md")
    bridge_windows = text("skills/serverfs-agent-bridge/references/windows.md")
    bridge_macos = text("skills/serverfs-agent-bridge/references/macos.md")

    assert "Repo Ops policy itself remains a normal per-workdir configuration concern" in configure
    assert "never substitute Agent submission" in configure
    assert "Repo Ops is explicitly **not Agent delegation**" in bridge
    assert "Repo Ops-only Host Bridge deployments" in bridge
    assert "do not enable an Agent runtime merely because Repo Ops" in install
    assert "Repo Ops-only deployments do not need an Agent runtime readiness check" in upgrade
    assert "do not submit an Agent task as a diagnostic fallback" in diagnose
    assert "Repo Ops may still depend on that Host Bridge transport" in tunnel_linux
    assert "Agent delegation and/or v0.23 Repo Ops" in auth_oauth
    assert (
        "v0.23.2 supports validated native Windows 11 x64 Deterministic Repo Ops" in bridge_windows
    )
    assert "independently gated from Agent delegation" in bridge_windows
    assert (
        "v0.23.1+ supports validated native Apple Silicon macOS Deterministic Repo Ops"
        in bridge_macos
    )
    assert "independently gated from Agent delegation" in bridge_macos
