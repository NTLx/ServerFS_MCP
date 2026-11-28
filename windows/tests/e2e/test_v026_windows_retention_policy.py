"""Provider-free regression for the frozen Windows runner's cleanup policy."""

from __future__ import annotations

import itertools

from e2e.windows_lifecycle_retention import lifecycle_retention_reasons


def test_retention_policy_is_fail_closed():
    reasons = lifecycle_retention_reasons
    sha = "8fc6aa459daf9b9517de0040fbe645ceb349c925"
    count = 0
    for source, cleanup, mutation, tasks, fixture, post, proxy in itertools.product(
        (None, sha, "wrong"),
        (None, False, True),
        (None, False, True),
        (None, [], [{}]),
        (None, False, True),
        (None, False, True),
        (False, True),
    ):
        child = {
            "source_sha": source,
            "cleanup_verified": cleanup,
            "unexpected_fixture_mutation": mutation,
            "provider_tasks": tasks,
            "real_fixture_state_verified": fixture,
            "post_teardown_fixture_state_verified": post,
        }
        old_ok = (
            source == sha and cleanup is True and not mutation and (not tasks or fixture is True)
        )
        expected = old_ok and (not proxy or not tasks or post is True)
        assert (not reasons(child, sha, proxy_reject=proxy)) == expected
        count += 1
    assert count == 1458


def test_missing_post_teardown_proof_retains_proxy_fixture():
    reasons = lifecycle_retention_reasons
    sha = "candidate"
    child = {
        "source_sha": sha,
        "cleanup_verified": True,
        "provider_tasks": [{}],
        "real_fixture_state_verified": True,
    }
    assert reasons(child, sha, proxy_reject=True) == [
        "post_teardown_fixture_state_unverified",
    ]
    assert reasons(child, sha, proxy_reject=False) == []
