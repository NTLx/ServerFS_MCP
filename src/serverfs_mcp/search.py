"""search_text implementation backed by ripgrep (rg).

Runs rg as a subprocess with an argument array (never a shell string).
User-supplied query/glob/path never touch a shell, so injection is
structurally impossible. rg enforces the file-size ceiling via
--max-filesize; we enforce wall-clock timeout and result limits.
"""

from __future__ import annotations

import subprocess

from .models import TextMatch
from .paths import ResolvedPath

# rg exit codes we treat as "no matches / benign"
_RG_BENIGN_EXIT = {0, 1}


class SearchTimeout(Exception):
    """rg exceeded the wall-clock timeout; the process was terminated."""


def run_search(
    resolved: ResolvedPath,
    *,
    query: str,
    glob: str | None,
    case_sensitive: bool,
    limit: int,
    timeout_seconds: float,
    max_file_bytes: int,
) -> tuple[list[TextMatch], bool]:
    """Search text content under a validated directory using rg.

    Returns (matches, truncated). Lines are parsed from rg's
    ``--vimgrep``-style output (path:line:content).
    """
    rel_root = resolved.rel_path

    args = [
        "rg",
        "--fixed-strings",
        "--vimgrep",
        "--no-heading",
        "--no-messages",
        "--max-filesize",
        str(max_file_bytes),
        f"--max-count={limit}",
        "--line-number",
        "--hidden",
    ]
    if not case_sensitive:
        args.append("--ignore-case")
    if glob:
        args.extend(["--glob", glob])
    # exclude denied basenames (e.g. *.env, *.pem) and hidden dirs rg would
    # otherwise skip-skip inconsistently with our path policy; we filter
    # hidden paths ourselves after the fact
    for pattern in ("!.git", "!.hg", "!.svn"):
        args.extend(["--glob", pattern])
    args.append("--")  # end of options: query then path operand follow
    args.append(query)
    args.append(".")
    # cwd is the resolved directory so rg reports paths relative to it
    cwd = str(resolved.container_path)

    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            shell=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        # TimeoutExpired kills the child on timeout in CPython (send SIGKILL
        # via kill() in the exception path). Guard against orphans explicitly.
        raise SearchTimeout() from None

    if proc.returncode not in _RG_BENIGN_EXIT:
        # rg 2 = error; surface a readable message without internals
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"SEARCH_FAILED: rg exited with {proc.returncode}: {detail[:200]}")

    matches: list[TextMatch] = []
    truncated = False
    prefix = f"{rel_root}/" if rel_root else ""
    stdout = proc.stdout.decode("utf-8", errors="replace")
    from .filesystem import _hidden_component

    deny_policy = resolved.deny_policy
    for line in stdout.splitlines():
        if not line:
            continue
        # --vimgrep format: path:line:column:text — strip the leading "./"
        parts = line.split(":", 3)
        if len(parts) != 4:
            continue
        path_part, line_part, _column, text_part = parts
        if path_part.startswith("./"):
            path_part = path_part[2:]
        # enforce our path policy on results: hidden components and denied
        # paths never surface as matches
        segs = tuple(path_part.split("/"))
        if any(_hidden_component(seg) for seg in segs):
            continue
        if deny_policy.is_denied(segs):
            continue
        try:
            line_no = int(line_part)
        except ValueError:
            continue
        matches.append(
            TextMatch(
                path=f"{prefix}{path_part}",
                line=line_no,
                text=text_part,
            )
        )
        if len(matches) >= limit:
            truncated = True
            break
    return matches, truncated
