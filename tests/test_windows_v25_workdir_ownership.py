"""Windows v25 Bridge workdir collision policy, source-only tests."""

from __future__ import annotations

import pytest

from serverfs_mcp.instance_identity import InstanceIdentityError
from serverfs_mcp.windows_v25.workdir_ownership import (
    WorkdirClaim,
    WorkdirOwnershipError,
    assert_independent_bridge_roots,
)


def claim(owner: str, root: str, writable: bool = True) -> WorkdirClaim:
    return WorkdirClaim(owner, root, writable)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (r"C:\Projects\Repo", r"c:\projects\repo"),
        (r"C:\Projects\Repo", r"C:\Projects\Repo\Nested"),
        (r"C:\Projects\Repo\Nested", r"C:\Projects\Repo"),
        (r"\\server\share\Repo", r"\\SERVER\SHARE\Repo\Sub"),
    ],
)
def test_writable_overlap_between_independent_bridges_is_refused(first: str, second: str) -> None:
    with pytest.raises(WorkdirOwnershipError, match="overlap"):
        assert_independent_bridge_roots(
            (claim("production", first),),
            (claim("staging", second),),
        )


def test_similar_prefix_is_not_a_canonical_root_overlap() -> None:
    assert_independent_bridge_roots(
        (claim("production", r"C:\Projects\Repo"),),
        (claim("staging", r"C:\Projects\Repository"),),
    )


def test_distinct_drive_or_share_is_not_overlap() -> None:
    assert_independent_bridge_roots(
        (claim("production", r"C:\Projects\Repo"),),
        (claim("staging", r"D:\Projects\Repo"),),
    )


def test_two_read_only_bridge_claims_can_overlap() -> None:
    assert_independent_bridge_roots(
        (claim("production", r"C:\Projects\Repo", False),),
        (claim("staging", r"c:\projects\repo\Nested", False),),
    )


@pytest.mark.parametrize("is_first_write,is_second_write", [(True, False), (False, True)])
def test_read_only_and_writable_overlap_is_not_safe(
    is_first_write: bool, is_second_write: bool
) -> None:
    with pytest.raises(WorkdirOwnershipError, match="overlap"):
        assert_independent_bridge_roots(
            (claim("production", r"C:\Projects\Repo", is_first_write),),
            (claim("staging", r"C:\Projects\Repo", is_second_write),),
        )


def test_proposed_batch_rejects_cross_bridge_writable_overlap() -> None:
    with pytest.raises(WorkdirOwnershipError, match="overlap"):
        assert_independent_bridge_roots(
            (
                claim("new-a", r"C:\Shared\Repo"),
                claim("new-b", r"C:\Shared\Repo\Nested", False),
            ),
            (),
        )


def test_proposed_batch_allows_overlapping_read_only_claims() -> None:
    assert_independent_bridge_roots(
        (
            claim("new-a", r"C:\Shared\Repo", False),
            claim("new-b", r"C:\Shared\Repo\Nested", False),
        ),
        (),
    )


def test_same_owner_is_not_a_cross_bridge_collision() -> None:
    assert_independent_bridge_roots(
        (claim("production", r"C:\Projects\Repo"),),
        (claim("production", r"C:\Projects\Repo\Sub"),),
    )


@pytest.mark.parametrize(
    "path",
    [
        "",
        "relative",
        r"C:relative",
        r"C:\Projects\..\Other",
        r"\\?\C:\Projects\Repo",
        r"\\.\pipe\name",
    ],
)
def test_untrusted_noncanonical_or_device_path_rejected(path: str) -> None:
    with pytest.raises(WorkdirOwnershipError):
        claim("staging", path)


def test_invalid_name_and_nonboolean_writable_rejected() -> None:
    with pytest.raises(InstanceIdentityError):
        claim("../other", r"C:\Safe")
    with pytest.raises(WorkdirOwnershipError):
        claim("staging", r"C:\Safe", 1)
