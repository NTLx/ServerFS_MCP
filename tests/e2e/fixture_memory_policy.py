"""Acceptance-only classification of local Agent memory directories."""


def fixture_entry_evidence(expected, actual, *, memory_allowed, memory_is_directory):
    extra = actual - expected
    memory_added = ".remember" in extra
    ignored = {".remember"} if memory_added and memory_allowed and memory_is_directory else set()
    return {
        "entries_unchanged": actual - ignored == expected,
        "new_entry_count": len(extra),
        "unexpected_entry_count": len(extra - ignored),
        "remember_added": memory_added,
        "remember_ignored": bool(ignored),
    }
