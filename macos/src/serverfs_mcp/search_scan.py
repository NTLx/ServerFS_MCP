"""Pure content-scan helpers shared by the Windows and Darwin searchers.

Both platforms implement ``search_text`` as "policy-filtered walk → bounded
regular-file read → literal UTF-8 scan" (no rg on Windows, no /proc on
Darwin). These helpers reproduce the rg behaviors that the public contract
observes: binary suppression by 64 KiB chunk truncation, per-line UTF-8
decode-or-skip, verbatim CRLF interiors and casefold-based case
insensitivity. They are pure functions over bytes/str — no platform
primitives — and are extracted verbatim from the Windows backend
(dev_plan_v0.11 §17) so every platform decides matches identically.
"""

from __future__ import annotations

from .models import TextMatch

# rg suppresses a NUL-containing file from the 64 KiB chunk that contains the
# first NUL onward (measured: a needle before the NUL in one small file still
# yields nothing). Truncating at the start of the NUL-containing chunk
# reproduces that exactly.
RG_READ_CHUNK = 65536

# VCS internals are never searched, on any platform, regardless of
# hidden policy (rg on Linux excludes them via glob; the walk-based
# searchers skip the directories outright).
ALWAYS_EXCLUDED_DIRS = frozenset({".git", ".hg", ".svn"})


def rg_binary_truncate(data: bytes) -> bytes:
    nul = data.find(b"\x00")
    if nul < 0:
        return data
    return data[: (nul // RG_READ_CHUNK) * RG_READ_CHUNK]


def scan_file(data: bytes, path: str, needle: str, case_sensitive: bool):
    for index, raw in enumerate(data.split(b"\n"), start=1):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # rg's UTF-8 matcher cannot match a line it cannot decode: an
            # undecodable line contributes no matches, the file continues
            continue
        # trailing \n was consumed by the split; an interior \r stays in the
        # reported text verbatim (rg does not normalize CRLF lines)
        probe = text if case_sensitive else text.casefold()
        if needle and needle in probe:
            yield TextMatch(path=path, line=index, text=text)
