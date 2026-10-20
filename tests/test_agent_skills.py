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
            PACKAGE_API["_validate_markdown_links"](skill_root, path, path.read_text())
    assert {p.relative_to(root) for p in root.rglob("*") if p.is_file()} == expected_files
    assert json.loads((root / "plugin.json").read_text())["version"] == _project_version()


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


@pytest.mark.parametrize("directory", [False, True])
def test_symlinks_fail_closed(plugin_source: Path, directory: bool) -> None:
    skill = plugin_source / "skills/serverfs-install"
    if directory:
        shutil.rmtree(skill)
        skill.symlink_to(REPO_ROOT / "skills/serverfs-install", target_is_directory=True)
    else:
        (skill / "references").mkdir()
        (skill / "references/external.md").symlink_to(REPO_ROOT / "README.md")
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
