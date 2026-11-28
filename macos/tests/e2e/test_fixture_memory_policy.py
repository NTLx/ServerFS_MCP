from e2e.fixture_memory_policy import fixture_entry_evidence


def test_local_agent_memory_is_the_only_allowed_extra():
    expected = {"marker.txt"}
    for allow in (False, True):
        for ordinary in (False, True):
            for actual in (
                expected,
                expected | {".remember"},
                expected | {".remember", "other"},
                expected | {"other"},
                {".remember"},
                set(),
            ):
                row = fixture_entry_evidence(
                    expected,
                    actual,
                    memory_allowed=allow,
                    memory_is_directory=ordinary,
                )
                ignored = ".remember" in actual and allow and ordinary
                assert row["remember_ignored"] == ignored
                assert row["entries_unchanged"] == (
                    actual == expected or (ignored and actual == expected | {".remember"})
                )
                assert row["unexpected_entry_count"] == len(actual - expected) - int(ignored)
