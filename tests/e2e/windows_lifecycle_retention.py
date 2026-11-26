"""Fail-closed acceptance fixture retention policy."""


def lifecycle_retention_reasons(child, sha, *, proxy_reject=False):
    """Fail closed on missing teardown or fixture integrity evidence."""
    reasons = []
    if child.get("source_sha") != sha:
        reasons.append("source_sha_mismatch")
    if child.get("cleanup_verified") is not True:
        reasons.append("native_fixture_cleanup_unverified")
    if child.get("unexpected_fixture_mutation", False):
        reasons.append("unexpected_fixture_mutation")
    if child.get("provider_tasks"):
        if child.get("real_fixture_state_verified") is not True:
            reasons.append("provider_fixture_state_unverified")
        if proxy_reject and child.get("post_teardown_fixture_state_verified") is not True:
            reasons.append("post_teardown_fixture_state_unverified")
    return reasons
