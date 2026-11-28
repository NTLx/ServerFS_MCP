r"""Pathname glob matching for the Windows search filter (dev_plan §17).

The Linux searcher delegates ``--glob`` to ripgrep; Windows has no rg, so
this module implements the same contract. It is deliberately conservative
about what the ``search_text`` tool exposes: ``*``, ``?``, ``[...]`` (with
``[!...]`` negation) and ``**`` directory wildcards. It is NOT a general
ripgrep reimplementation — no brace alternation, no extened classes; those
grammars are outside the advertised tool contract, and a pattern using
them simply matches nothing rather than matching something surprising.

Semantics pinned to ripgrep's documented --glob behaviour (and verified by
a black-box parity test against real rg, tests/test_search_glob_parity.py):

- a pattern without ``/`` matches the FILE NAME (any depth): ``*.py`` hits
  ``a.py`` and ``pkg/deep/b.py``;
- a pattern with ``/`` matches the path relative to the SEARCH ROOT,
  components must line up: ``docs/*.md`` hits ``docs/a.md`` but never
  ``docs/sub/a.md``;
- a ``**/`` prefix matches zero or more leading directories:
  ``**/*.txt`` hits root-level and deep files; ``src/**`` matches
  everything under ``src/``;
- ``*`` and ``?`` never cross ``/``;
- matching is case-sensitive on both platforms (rg's Linux default; the
  Windows backend compares here, not in the kernel, so the agent-visible
  contract is identical).
"""

from __future__ import annotations

import re

__all__ = ["glob_matches"]


def _translate(glob: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    n = len(glob)
    while i < n:
        ch = glob[i]
        if ch == "*":
            if glob.startswith("**", i):
                if glob.startswith("**/", i):
                    # zero or more leading directories
                    out.append("(?:[^/]*/)*")
                    i += 3
                    continue
                # trailing or interior ** : span separators
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if ch == "?":
            out.append("[^/]")
            i += 1
            continue
        if ch == "[":
            close = glob.find("]", i + 1)
            if close < 0:
                out.append(re.escape(ch))  # fnmatch/rg: unterminated is literal
                i += 1
                continue
            body = glob[i + 1 : close]
            neg = ""
            if body.startswith(("!", "^")):
                neg = "^"
                body = body[1:]
            body = body.replace("\\", "\\\\")
            out.append(f"[{neg}{body}]")
            i = close + 1
            continue
        out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out) + r"\Z")


_CACHE: dict[str, re.Pattern[str]] = {}


def _pattern(glob: str) -> re.Pattern[str]:
    compiled = _CACHE.get(glob)
    if compiled is None:
        compiled = _translate(glob)
        _CACHE[glob] = compiled
    return compiled


def glob_matches(name: str, rel_path: str, glob: str) -> bool:
    """True when one search result passes the --glob style pattern.

    ``name`` is the file name, ``rel_path`` the POSIX-style path relative
    to the search root (which the caller must supply — never the workdir
    path, mirroring rg's cwd-relative matching).
    """
    if "/" in glob:
        return _pattern(glob).match(rel_path) is not None
    return _pattern(glob).match(name) is not None
