"""Regression tests for the v0.23.3 stable-release orchestration contract."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def on_block(name: str) -> str:
    return text(name).split("permissions:", 1)[0]


def test_stable_site_does_not_link_to_retired_root_paths_on_main() -> None:
    prefix = "https://github.com/NTLx/ServerFS_MCP/blob/main/"
    retired_paths = (
        "deployment/linux/README.md",
        "deployment/macos/README.md",
        ".env.example",
        "serverfs.toml.example",
    )
    documents = sorted((ROOT / "site/src/content/docs").rglob("*.md"))
    assert documents
    for document in documents:
        content = document.read_text(encoding="utf-8")
        for path in retired_paths:
            assert prefix + path not in content, (document, path)


def test_platform_specific_acceptance_drivers_have_one_owner() -> None:
    owners = {
        "macos_shared_claude_owner.py": "macos",
        "macos_shared_qoder_diagnostics.py": "macos",
        "macos_shared_qoder_owner.py": "macos",
        "run_v012_linux_provider_acceptance.py": "linux",
        "v026_windows_candidate_batch.py": "windows",
    }
    for filename, owner in owners.items():
        for platform in ("linux", "macos", "windows"):
            path = ROOT / platform / "tests" / "e2e" / filename
            assert path.is_file() == (platform == owner), (platform, filename, owner)


def test_common_distribution_gate_is_independent_of_product_runtime() -> None:
    workflow = text("container.yml")
    assert on_block("container.yml").count('      - "deployment/native/check_distribution.py"') == 2
    assert "python -I -B ../deployment/native/check_distribution.py" in workflow
    gate = (ROOT / "deployment/native/check_distribution.py").read_text(encoding="utf-8")
    assert '"--noconftest"' in gate
    assert "sys.modules[package] = None" in gate


def test_windows_doctor_fixtures_stay_outside_platform_source() -> None:
    workflow = text("windows-agent.yml")
    for variable, name in (
        ("disabledConfig", "doctor-smoke.toml"),
        ("enabledConfig", "doctor-enabled.toml"),
    ):
        assert f'${variable} = Join-Path $env:RUNNER_TEMP "{name}"' in workflow
        assert f"-LiteralPath ${variable}" in workflow
        assert f"doctor --config ${variable}" in workflow
        assert f"utf8NoBOM {name}" not in workflow
        assert f"doctor --config {name}" not in workflow


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
    assert "working-directory: windows\n" in wheel
    assert "--platform windows --wheel $native --wheel $product --wheel $bridge" in wheel
    assert "needs: wheel" in container
    assert "release_tag: ${{ inputs.release_tag }}" in container
    assert "- wheel" in verify
    assert "- container-release" in verify
    assert "Unexpected GitHub Release asset set" in verify
    assert "serverfs_mcp-$version-py3-none-win_amd64.whl" in verify
    assert "serverfs_agent_bridge-$version-py3-none-win_amd64.whl" in verify
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
    assert "../deployment/native/check_distribution.py" in container
    gate = (ROOT / "deployment/native/check_distribution.py").read_text(encoding="utf-8")
    assert '"tests/test_release_workflow.py"' in gate


def test_stable_container_publish_verifies_tag_points_to_candidate_sha() -> None:
    publish = text("container-publish.yml")
    assert "workflow_call:" in on_block("container-publish.yml")
    assert "git ls-remote origin" in publish
    assert 'if [ "$remote_tag" != "$GITHUB_SHA" ]' in publish
    assert "$IMAGE_NAME:$version" in publish
    assert "$IMAGE_NAME:$minor" in publish
    assert "$IMAGE_NAME:latest" in publish


def test_integration_pr_triggers_validation_without_expanding_push_publication() -> None:
    for workflow, platform in (
        ("container.yml", "linux"),
        ("macos-native.yml", "macos"),
        ("windows-native.yml", "windows"),
        ("windows-agent.yml", "windows"),
    ):
        triggers = on_block(workflow)
        pr = triggers.split("  pull_request:", 1)[1]
        push = triggers.split("  push:", 1)[1].split("  pull_request:", 1)[0]
        for block in (pr, push):
            assert "      - main\n" in block
            assert "      - feat/v0.27.0-platform-split\n" in block
            assert f'      - "{platform}/**"\n' in block
            assert '      - "contracts/**"\n' in block
        assert "      - integration/v0.27.0\n" in pr
        assert "integration/v0.27.0" not in push
        assert f"      - feat/v0.27.0-{platform}\n" in push
        for other in {"linux", "macos", "windows"} - {platform}:
            assert f'      - "{other}/**"\n' not in triggers
    assert "integration/v0.27.0" not in on_block("wheel-release.yml")
    assert "integration/v0.27.0" not in on_block("container-publish.yml")


def test_windows_agent_ci_triggers_and_runs_named_proxy_traffic_regression() -> None:
    workflow = text("windows-agent.yml")
    triggers = on_block("windows-agent.yml")
    assert triggers.count('      - "windows/**"') == 2
    assert "working-directory: windows\n" in workflow
    assert "working-directory: windows/agent_bridge\n" in workflow
    assert "            tests/test_windows_v026_named_proxy_live.py `" in workflow


def test_native_ci_uses_platform_local_paths() -> None:
    macos = text("macos-native.yml")
    assert "working-directory: macos\n" in macos
    assert "working-directory: macos/agent_bridge\n" in macos
    assert macos.count("--platform macos --wheel") == 2
    native = text("windows-native.yml")
    assert "working-directory: windows\n" in native
    assert "working-directory: native/windows\n" not in native
    assert "working-directory: windows/native/windows\n" in native
    assert "path: windows/native/windows/target/wheels/*.whl" in native
    agent = text("windows-agent.yml")
    assert "python ../deployment/native/wheel_release.py version-gate" in agent
    assert "--platform windows --wheel $native --wheel $product --wheel $bridge" in agent
    assert "windows/dist/serverfs_mcp-*.whl" in agent
    assert "windows/dist/serverfs_agent_bridge-*.whl" in agent
    assert "tests/test_agent_deployment.py" not in agent


def test_platform_wheels_are_retained_with_candidate_sha() -> None:
    for workflow, platform in (("container.yml", "linux"), ("macos-native.yml", "macos")):
        source = text(workflow)
        for component in ("core", "bridge"):
            assert f"name: {platform}-{component}-wheel-" + "${{ github.sha }}" in source
            assert (
                "path: ${{ runner.temp }}/" + f"serverfs-{platform}-{component}-wheels/*.whl"
            ) in source
        assert source.count("if-no-files-found: error") >= 2
    assert "name: windows-agent-wheels-${{ github.sha }}" in text("windows-agent.yml")


def test_platform_ci_runs_frozen_contract_comparison() -> None:
    for workflow in ("container.yml", "macos-native.yml", "windows-agent.yml"):
        source = text(workflow)
        triggers = on_block(workflow)
        assert triggers.count('      - "deployment/native/check_mcp_contract.py"') == 2
        assert triggers.count('      - "docs/diagnostics/v0270-capture-v026-contract.py"') == 2
        assert "-I -B ../deployment/native/check_mcp_contract.py .." in source


def test_platform_ci_builds_outside_the_repository() -> None:
    for workflow in ("container.yml", "macos-native.yml", "windows-agent.yml"):
        source = text(workflow)
        assert on_block(workflow).count('      - "deployment/native/platform_isolation.py"') == 2
        assert source.count("../deployment/native/platform_isolation.py . --online") == 1
        assert source.count("Verify standalone platform source and installed wheels") == 1


def test_macos_platform_assertions_do_not_depend_on_unchecked_out_subtree() -> None:
    workflow = text("macos-native.yml")
    jobs = workflow.split("      - name: Assert the platform again inside the job")[1:]
    assert len(jobs) == 2
    for job in jobs:
        before_checkout = job.split("      - name: Checkout", 1)[0]
        assert "working-directory: ${{ github.workspace }}" in before_checkout


def test_integration_requires_historical_linux_systemd_rollback() -> None:
    gate = text("integration-candidate.yml")
    job = gate.split("  linux-systemd-rollback:\n", 1)[1].split(
        "  windows-native-gate:", 1
    )[0]
    assert "runs-on: ubuntu-latest" in job
    assert "fetch-depth: 0" in job and "persist-credentials: false" in job
    assert "systemctl --user list-unit-files --no-legend --no-pager" in job
    assert "systemctl --user list-units --all --plain --no-legend --no-pager" in job
    assert "awk '$1 ~ /^serverfs-agent-bridge/ {print $1}'" in job
    assert "systemctl --user list-unit-files 'serverfs-agent-bridge*'" not in job
    assert 'if [ -n "$conflicts" ]; then' in job
    assert "check_linux_upgrade.py --systemd --online" in job
    assert "check_linux_upgrade.py --persistent-systemd --online" in job
    assert "continue-on-error:" not in job
    driver = (ROOT / "deployment/native/check_linux_upgrade.py").read_text(encoding="utf-8")
    assert "test_actual_release_systemd_upgrade_rollback" in driver
    assert "test_actual_release_persistent_systemd_upgrade_rollback" in driver
    assert '"failure", "error", "skipped"' in driver
    assert "SERVERFS_LINUX_BASELINE_BRIDGE_SOURCE" in driver


def test_integration_requires_historical_windows_scheduler_rollback() -> None:
    gate = text("integration-candidate.yml")
    job = gate.split("  windows-scheduler-rollback:\n", 1)[1].split("  macos-gate:", 1)[0]
    assert "runs-on: windows-latest" in job
    assert "fetch-depth: 0" in job and "persist-credentials: false" in job
    assert 'SERVERFS_WINDOWS_SCHEDULER_ROLLBACK: "1"' in job
    assert "tests/test_windows_v27_scheduler_core_rollback.py" in job
    assert 'if ($sid.EndsWith("-500"))' in job
    assert "tests/test_windows_v27_scheduler_hosted_identity.py" in job
    assert "actual rollback NOT EXECUTED" in job
    assert "Separate ordinary-user native evidence required for release." in job
    assert "$cases[0].GetAttribute(\"name\") -ne $case" in job
    assert 'SelectSingleNode("failure|error|skipped")' in job
    assert "continue-on-error:" not in job
    fixture = (
        ROOT / "windows/tests/test_windows_v27_scheduler_core_rollback.py"
    ).read_text(encoding="utf-8")
    assert 'os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"' in fixture
    assert "for index, release in enumerate((baseline, candidate, baseline)):" in fixture
    assert 'scheduler.uninstall(manifest)["registered"] is False' in fixture
    assert "GetInstances(0)" in fixture and "InstanceGuid" in fixture
    assert "LastRunTime" not in fixture
    assert "SERVERFS_WINDOWS_SCHEDULER_ROLLBACK_LOCAL" in fixture
    assert "_protected_tasks(manifest.task_name()) == prior_tasks" in fixture
    hosted = (
        ROOT / "windows/tests/test_windows_v27_scheduler_hosted_identity.py"
    ).read_text(encoding="utf-8")
    assert 'sid.endswith("-500")' in hosted
    assert "pytest.raises(storage.StorageError" in hosted
    assert "REAL_ROLLBACK_NOT_EXECUTED" in hosted


def test_exact_integration_sha_gate_is_validation_only() -> None:
    gate = text("integration-candidate.yml")
    assert "  push:\n    branches:\n      - integration/v0.27.0\n" in gate
    assert "workflow_dispatch:" in gate
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
    assert "\n    if:" not in check
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
            "(github.event_name == 'push' && github.ref == 'refs/heads/integration/v0.27.0')"
        )
        == 2
    )
