"""Regression tests for the v0.23.3 stable-release orchestration contract."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def on_block(name: str) -> str:
    return text(name).split("permissions:", 1)[0]


def test_stable_release_is_dispatch_first_not_tag_first() -> None:
    release = on_block("wheel-release.yml")
    assert "workflow_dispatch:" in release
    assert "release_tag:" in release
    assert "  push:" not in release


def test_release_calls_every_exact_sha_gate_before_tag_creation() -> None:
    release = text("wheel-release.yml")
    assert "'registry': json.loads((root / 'server.json').read_text())['version']" in release
    for workflow in (
        "container.yml",
        "windows-native.yml",
        "windows-agent.yml",
        "macos-native.yml",
        "pages.yml",
    ):
        assert f"uses: ./.github/workflows/{workflow}" in release

    create_tag = release.split("  create-tag:", 1)[1].split("  wheel:", 1)[0]
    for gate in (
        "prepare",
        "linux-gate",
        "windows-native-gate",
        "windows-agent-gate",
        "macos-gate",
        "pages-gate",
    ):
        assert f"- {gate}" in create_tag
    assert "origin/main" in create_tag
    assert "git push --porcelain --no-verify origin" in create_tag
    assert "--force" not in create_tag


def test_release_artifacts_are_downstream_of_validated_tag() -> None:
    release = text("wheel-release.yml")
    wheel = release.split("  wheel:", 1)[1].split("  container-release:", 1)[0]
    container = release.split("  container-release:", 1)[1].split("  verify-publication:", 1)[0]
    verify = release.split("  verify-publication:", 1)[1]
    assert "needs: create-tag" in wheel
    assert "needs: wheel" in container
    assert "release_tag: ${{ inputs.release_tag }}" in container
    assert "- wheel" in verify
    assert "- container-release" in verify
    assert "Unexpected GitHub Release asset set" in verify
    assert "serverfs_mcp-$version-py3-none-any.whl" in verify
    assert "serverfs_agent_bridge-$version-py3-none-any.whl" in verify
    assert "serverfs_windows_native-$version-cp312-abi3-win_amd64.whl" in verify
    assert "serverfs-plugin-v$version.zip" in verify
    assert "Stable GHCR tags do not resolve to the same OCI index" in verify


def test_release_gates_are_reusable_without_tag_push_side_effects() -> None:
    for workflow in (
        "container.yml",
        "windows-native.yml",
        "windows-agent.yml",
        "macos-native.yml",
        "pages.yml",
    ):
        assert "workflow_call:" in on_block(workflow)

    assert '- "v*.*.*"' not in on_block("container.yml")
    assert '- "v*.*.*"' not in on_block("macos-native.yml")

    pages = text("pages.yml")
    assert "deploy_pages:" in on_block("pages.yml")
    side_effect_condition = (
        "github.event_name == 'push' || "
        "(github.event_name == 'workflow_dispatch' && inputs.deploy_pages)"
    )
    assert pages.count(side_effect_condition) == 3

    release = text("wheel-release.yml")
    linux_gate = release.split("  linux-gate:", 1)[1].split("  windows-native-gate:", 1)[0]
    assert "packages: write" in linux_gate

    pages_gate = release.split("  pages-gate:", 1)[1].split("  create-tag:", 1)[0]
    assert "pages: write" in pages_gate
    assert "id-token: write" in pages_gate
    assert "deploy_pages: false" in pages_gate


def test_stable_container_publish_verifies_tag_points_to_candidate_sha() -> None:
    publish = text("container-publish.yml")
    assert "workflow_call:" in on_block("container-publish.yml")
    assert "git ls-remote origin" in publish
    assert 'if [ "$remote_tag" != "$GITHUB_SHA" ]' in publish
    assert "$IMAGE_NAME:$version" in publish
    assert "$IMAGE_NAME:$minor" in publish
    assert "$IMAGE_NAME:latest" in publish
