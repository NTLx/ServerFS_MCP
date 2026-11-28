"""Approval-scope denial is limited to explicit structured write targets."""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_agent_bridge.approval_scope import outside_workdir_write


@pytest.fixture
def roots(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "nested").mkdir()
    outside = tmp_path / "secrets"
    outside.mkdir()
    return repo, outside


@pytest.mark.parametrize(
    "target",
    [
        "nested/file.txt",
        ".",
        "nested/../file.txt",
    ],
)
def test_file_changes_inside_root_remain_human_decided(roots, target):
    repo, _ = roots
    payload = {"category": "file_change", "file_changes": [{"path": target}]}
    assert outside_workdir_write(payload, repo, "") is False


@pytest.mark.parametrize(
    "target",
    [
        "../secrets/file.txt",
        "../../other",
        "/tmp/elsewhere",
        "",
        "invalid\x00path",
    ],
)
def test_file_changes_escape_or_unknown_target_are_blocked(roots, target):
    repo, _ = roots
    payload = {"category": "file_change", "tool_input": {"file_path": target}}
    assert outside_workdir_write(payload, repo, "") is True


def test_missing_and_malformed_file_target_are_blocked(roots):
    repo, _ = roots
    assert outside_workdir_write({"category": "file_change"}, repo, "") is True
    assert (
        outside_workdir_write(
            {"category": "file_changes", "file_changes": [{"other": "x"}]}, repo, ""
        )
        is True
    )
    assert (
        outside_workdir_write({"category": "file_changes", "file_changes": "not-a-list"}, repo, "")
        is True
    )


def test_file_change_cannot_follow_existing_symlink_outside(roots):
    repo, outside = roots
    link = repo / "nested" / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink fixtures are unavailable on this host")
    assert (
        outside_workdir_write(
            {"category": "file_change", "tool_input": {"file_path": "nested/escape/target"}},
            repo,
            "",
        )
        is True
    )


def test_relative_target_is_resolved_from_task_cwd(roots):
    repo, _ = roots
    assert (
        outside_workdir_write(
            {"category": "file_change", "tool_input": {"file_path": "../../secrets/file"}},
            repo,
            "nested",
        )
        is True
    )
    assert (
        outside_workdir_write(
            {"category": "file_change", "tool_input": {"file_path": "file"}},
            repo,
            "nested",
        )
        is False
    )


def test_permission_writes_outside_blocked_reads_remain_provider_decided(roots):
    repo, outside = roots
    permission = {
        "category": "provider_permission",
        "requested_permissions": [
            {
                "permission_id": "fileSystem",
                "details": {"read": [str(outside)], "write": ["nested"]},
            }
        ],
    }
    assert outside_workdir_write(permission, repo, "") is False
    permission["requested_permissions"][0]["details"]["writableRoots"] = [str(outside)]
    assert outside_workdir_write(permission, repo, "") is True


def test_acp_choice_only_permission_is_not_misclassified_as_file_write(roots):
    repo, _ = roots
    payload = {
        "category": "provider_permission",
        "title": "ACP confirmation",
        "response_options": [
            {"option_id": "allow", "label": "Allow once", "kind": "allow_once"},
            {"option_id": "reject", "label": "Reject", "kind": "reject_once"},
        ],
    }
    assert outside_workdir_write(payload, repo, "") is False


def test_arbitrary_commands_not_classified_by_free_text(roots):
    repo, outside = roots
    assert (
        outside_workdir_write(
            {"category": "command", "command_display": f"touch {outside / 'file'}"},
            repo,
            "",
        )
        is False
    )
