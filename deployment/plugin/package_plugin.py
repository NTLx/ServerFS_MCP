#!/usr/bin/env python3
"""Validate and package the ServerFS portable Agent Plugin."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

EXPECTED_SKILLS = (
    "serverfs-install",
    "serverfs-configure",
    "serverfs-upgrade",
    "serverfs-diagnose",
    "serverfs-tunnel",
    "serverfs-auth",
    "serverfs-agent-bridge",
)

REQUIRED_HEADINGS = (
    "## When to use",
    "## When NOT to use",
    "## Preflight",
    "## Authoritative sources",
    "## Procedure",
    "## Platform branches",
    "## Safety / stop conditions",
    "## Verification",
    "## Success criteria",
    "## Failure handling",
)

PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
PLUGIN_NAME = "serverfs"
DISPLAY_NAME = "ServerFS"
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
MARKDOWN_DEFINITION_RE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
SUSPICIOUS_SECRET_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----)"
)


class ValidationError(RuntimeError):
    """Raised when the checked-out plugin source violates the release contract."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def project_version(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValidationError("pyproject.toml is missing [project].version")
    return version


def _parse_front_matter(path: Path, content: str) -> dict[str, str]:
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        raise ValidationError(f"{path}: SKILL.md must start with YAML front matter")

    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValidationError(f"{path}: front matter is not closed") from exc

    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValidationError(f"{path}: unsupported front matter line {line!r}")
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"').strip("'")
    return metadata


def _validate_markdown_links(skill_root: Path, skill_file: Path, content: str) -> None:
    targets = MARKDOWN_LINK_RE.findall(content) + MARKDOWN_DEFINITION_RE.findall(content)
    for raw_target in targets:
        target = raw_target.strip().split(' "', 1)[0].strip("<>")
        if not target or target.startswith("#"):
            continue
        parsed = urlparse(target)
        if parsed.scheme in {"http", "https", "mailto"}:
            continue
        if parsed.scheme:
            raise ValidationError(f"{skill_file}: unsupported link scheme in {target!r}")

        relative = unquote(parsed.path)
        if not relative:
            continue
        resolved = (skill_file.parent / relative).resolve()
        try:
            resolved.relative_to(skill_root.resolve())
        except ValueError as exc:
            raise ValidationError(
                f"{skill_file}: link escapes packaged skill: {target!r}; "
                "express checkout-only sources as paths after locating a checkout"
            ) from exc
        if Path(relative).is_absolute() or not resolved.is_file():
            raise ValidationError(f"{skill_file}: broken packaged link: {target!r}")


def validate_skills(root: Path) -> None:
    skills_root = root / "skills"
    if not (skills_root / "README.md").is_file():
        raise ValidationError("skills/README.md is missing")

    actual = sorted(entry.name for entry in skills_root.iterdir() if entry.is_dir())
    expected = sorted(EXPECTED_SKILLS)
    if actual != expected:
        raise ValidationError(f"skill set mismatch: expected={expected}, actual={actual}")

    for skill_name in EXPECTED_SKILLS:
        path = skills_root / skill_name / "SKILL.md"
        content = path.read_text(encoding="utf-8")
        metadata = _parse_front_matter(path, content)

        if metadata.get("name") != skill_name:
            raise ValidationError(
                f"{path}: front matter name must equal directory name {skill_name!r}"
            )
        if not metadata.get("description"):
            raise ValidationError(f"{path}: front matter description is required")
        if len(metadata["description"]) > 1024:
            raise ValidationError(f"{path}: description is unexpectedly long")

        for heading in REQUIRED_HEADINGS:
            if heading not in content:
                raise ValidationError(f"{path}: missing required section {heading!r}")

        if metadata.get("disable-model-invocation", "false") != "false":
            raise ValidationError(f"{path}: skill must remain model-invoked")

    for path, _ in _runtime_skill_files(root):
        content = path.read_text(encoding="utf-8")
        skill_root = skills_root / path.relative_to(skills_root).parts[0]
        _validate_markdown_links(skill_root, path, content)
        if SUSPICIOUS_SECRET_RE.search(content):
            raise ValidationError(f"{path}: content appears to contain a credential token")


def validate_manifest(root: Path, *, tag: str | None = None) -> dict[str, object]:
    manifest_path = root / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = project_version(root)

    if manifest.get("$schema") != PLUGIN_SCHEMA:
        raise ValidationError("plugin.json does not use the portable Agent Plugins 1.0.0 schema")
    if manifest.get("name") != PLUGIN_NAME:
        raise ValidationError(f"plugin.json name must be {PLUGIN_NAME!r}")
    if manifest.get("version") != version:
        raise ValidationError(
            f"plugin.json version {manifest.get('version')!r} does not match project {version!r}"
        )
    if not manifest.get("description"):
        raise ValidationError("plugin.json description is required")

    extensions = manifest.get("extensions")
    if not isinstance(extensions, dict):
        raise ValidationError("plugin.json extensions.com.openai.interface is required")
    openai = extensions.get("com.openai")
    if not isinstance(openai, dict):
        raise ValidationError("plugin.json extensions.com.openai is required")
    interface = openai.get("interface")
    if not isinstance(interface, dict) or interface.get("displayName") != DISPLAY_NAME:
        raise ValidationError(f"plugin displayName must be {DISPLAY_NAME!r}")

    if "skills" in manifest or "mcpServers" in manifest:
        raise ValidationError(
            "portable plugin.json must use fixed skills/ and mcp.json discovery, not Codex fields"
        )
    if (root / "mcp.json").exists():
        raise ValidationError("v0.18 skills-only Plugin must not bundle root mcp.json")
    if (root / ".codex-plugin" / "plugin.json").exists():
        raise ValidationError("v0.18 must not maintain a parallel .codex-plugin/plugin.json")

    if tag is not None:
        tag_version = tag[1:] if tag.startswith("v") else tag
        if tag_version != version:
            raise ValidationError(f"tag {tag!r} does not match project/plugin version {version!r}")

    return manifest


def validate(root: Path, *, tag: str | None = None) -> str:
    validate_skills(root)
    validate_manifest(root, tag=tag)
    return project_version(root)


def _runtime_skill_files(root: Path) -> list[tuple[Path, PurePosixPath]]:
    files: list[tuple[Path, PurePosixPath]] = []
    for entry in (root / "skills").iterdir():
        if entry.is_symlink() or (entry.name not in EXPECTED_SKILLS and entry.name != "README.md"):
            raise ValidationError(f"unexpected skills entry: {entry}")
    for skill_name in EXPECTED_SKILLS:
        skill_root = root / "skills" / skill_name
        if skill_root.is_symlink():
            raise ValidationError(f"Plugin skill must not be a symlink: {skill_root}")
        for path in sorted(skill_root.rglob("*")):
            if path.is_symlink():
                raise ValidationError(f"Plugin runtime skill must not contain symlink: {path}")
            if path.is_file():
                local = path.relative_to(skill_root)
                if local != Path("SKILL.md") and not (
                    local.parts[0] == "references"
                    and path.suffix == ".md"
                    and path.name != "SKILL.md"
                ):
                    raise ValidationError(f"unexpected Plugin runtime file: {path}")
                relative = path.relative_to(root)
                files.append((path, PurePosixPath("serverfs") / PurePosixPath(relative.as_posix())))
    return files


def expected_archive_members(root: Path) -> list[str]:
    members = ["serverfs/plugin.json"]
    members.extend(str(archive_path) for _, archive_path in _runtime_skill_files(root))
    return sorted(members)


def _write_deterministic_file(archive: zipfile.ZipFile, source: Path, target: str) -> None:
    content = source.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    info = zipfile.ZipInfo(target, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3  # Unix, matching the fixed POSIX permissions on every host.
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, content.encode("utf-8"))


def build_archive(root: Path, output_dir: Path, *, tag: str | None = None) -> Path:
    version = validate(root, tag=tag)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"serverfs-plugin-v{version}.zip"

    with zipfile.ZipFile(output_path, "w") as archive:
        _write_deterministic_file(archive, root / "plugin.json", "serverfs/plugin.json")
        for source, target in _runtime_skill_files(root):
            _write_deterministic_file(archive, source, str(target))

    with zipfile.ZipFile(output_path, "r") as archive:
        actual = sorted(archive.namelist())
    expected = expected_archive_members(root)
    if actual != expected:
        output_path.unlink(missing_ok=True)
        raise ValidationError(f"archive layout mismatch: expected={expected}, actual={actual}")

    return output_path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate skills and manifest")
    validate_parser.add_argument("--tag", help="optional release tag, for example v0.18.0")

    build_parser = subparsers.add_parser("build", help="validate and build the Plugin ZIP")
    build_parser.add_argument("--output-dir", type=Path, required=True)
    build_parser.add_argument("--tag", help="optional release tag, for example v0.18.0")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    root = repo_root()
    try:
        if args.command == "validate":
            version = validate(root, tag=args.tag)
            print(f"ServerFS Agent Skills/Plugin validation passed for v{version}")
            return 0
        if args.command == "build":
            output = build_archive(root, args.output_dir, tag=args.tag)
            print(output)
            return 0
    except (OSError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        return 1

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
