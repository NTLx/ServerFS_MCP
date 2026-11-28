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
    metadata_check = 'deployment/native/release_metadata.py --root . --tag "$RELEASE_TAG"'
    assert metadata_check in release
    prepare = release.split("  linux-gate:", 1)[0]
    assert prepare.index(metadata_check) < prepare.index("git fetch --prune origin main --tags")
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
        "(github.event_name == 'push' && github.ref == 'refs/heads/main') || "
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


def test_pages_pr_preview_is_not_a_publication_step() -> None:
    pages = text("pages.yml")
    preview = pages.split("      - name: Retain PR preview", 1)[1].split(
        "      - name: Configure GitHub Pages", 1
    )[0]
    assert "if: github.event_name == 'pull_request'" in preview
    assert "actions/upload-artifact@" in preview
    assert "name: site-preview-${{ github.sha }}" in preview
    assert "path: site/dist" in preview
    assert "if-no-files-found: error" in preview
    assert "deploy-pages" not in preview


def test_pages_preview_manifest_is_machine_readable_and_validation_only() -> None:
    pages = text("pages.yml")
    manifest = pages.split("      - name: Write PR preview provenance", 1)[1].split(
        "      - name: Retain PR preview", 1
    )[0]
    assert "if: github.event_name == 'pull_request'" in manifest
    for value in (
        "PR_HEAD_SHA: ${{ github.event.pull_request.head.sha }}",
        "PR_BASE_SHA: ${{ github.event.pull_request.base.sha }}",
        "GITHUB_RUN_ID: ${{ github.run_id }}",
        "GITHUB_RUN_ATTEMPT: ${{ github.run_attempt }}",
    ):
        assert value in manifest
    assert '["git", "rev-parse", "HEAD"]' in manifest
    assert "json.dump(manifest" in manifest and "dist/provenance.json" in manifest
    assert "${{" not in manifest.split("run: |", 1)[1]


def test_linux_ci_and_publication_use_the_same_platform_local_context() -> None:
    container = text("container.yml")
    assert container.count("working-directory: linux\n") == 3
    assert "working-directory: linux/agent_bridge\n" in container
    for workflow in ("container.yml", "container-publish.yml"):
        source = text(workflow)
        assert "context: ./linux\n" in source
        assert "file: ./linux/Dockerfile\n" in source
        assert "context: .\n" not in source
    assert "../deployment/registry/validate.py" in container
    assert "../tests/test_release_workflow.py" in container


def test_stable_container_publish_verifies_tag_points_to_candidate_sha() -> None:
    publish = text("container-publish.yml")
    assert "workflow_call:" in on_block("container-publish.yml")
    assert "git ls-remote origin" in publish
    assert 'if [ "$remote_tag" != "$GITHUB_SHA" ]' in publish
    assert "$IMAGE_NAME:$version" in publish
    assert "$IMAGE_NAME:$minor" in publish
    assert "$IMAGE_NAME:latest" in publish


def test_integration_pr_triggers_validation_without_expanding_push_publication() -> None:
    for workflow in (
        "container.yml",
        "macos-native.yml",
        "windows-native.yml",
        "windows-agent.yml",
        "pages.yml",
    ):
        triggers = on_block(workflow)
        pr = triggers.split("  pull_request:", 1)[1]
        assert "    branches:\n      - main\n      - integration/v0.26.0\n" in pr
        push = triggers.split("  push:", 1)[1].split("  pull_request:", 1)[0]
        assert "      - main\n" in push
        assert "integration/v0.26.0" not in push
    assert "integration/v0.26.0" not in on_block("wheel-release.yml")
    assert "integration/v0.26.0" not in on_block("container-publish.yml")


def test_windows_agent_ci_triggers_and_runs_named_proxy_traffic_regression() -> None:
    workflow = text("windows-agent.yml")
    path = "tests/test_windows_v026_named_proxy_live.py"
    triggers = on_block("windows-agent.yml")
    assert triggers.count(f'      - "{path}"') == 2
    assert f"            {path} `" in workflow


def test_exact_integration_sha_gate_is_validation_only() -> None:
    gate = text("integration-candidate.yml")
    assert "  push:\n    branches:\n      - integration/v0.26.0\n" in gate
    assert "workflow_dispatch:" not in gate
    assert "paths:" not in gate and "paths-ignore:" not in gate
    for workflow in (
        "container.yml",
        "windows-native.yml",
        "windows-agent.yml",
        "macos-native.yml",
        "pages.yml",
    ):
        assert gate.count(f"uses: ./.github/workflows/{workflow}") == 1
    assert "deploy_pages: false" in gate
    assert "container-publish.yml" not in gate and "wheel-release.yml" not in gate
    container = text("container.yml")
    publish = container.split("  publish:", 1)[1]
    assert "if: github.event_name == 'push' && github.ref == 'refs/heads/main'" in publish
    check = container.split("  container-check:", 1)[1].split("  publish:", 1)[0]
    assert (
        "(github.event_name == 'push' && github.ref == 'refs/heads/integration/v0.26.0')"
    ) in check
    pages = text("pages.yml")
    assert (
        pages.count(
            "(github.event_name == 'push' && github.ref == 'refs/heads/main') || "
            "(github.event_name == 'workflow_dispatch' && inputs.deploy_pages)"
        )
        == 3
    )
    assert (
        pages.count(
            "if: github.event_name == 'pull_request' || "
            "(github.event_name == 'push' && github.ref == 'refs/heads/integration/v0.26.0')"
        )
        == 2
    )
