"""search_text implementation backed by ripgrep (rg).

rg runs with an argument array (never a shell string; user input never
concatenates into a command string) rooted at a pre-validated directory FD
(via /proc/self/fd), streaming ``--json`` output. ServerFS:

- applies hidden/deny policy to every result path (defense in depth)
- enforces the GLOBAL result limit with true early-stop: once limit+1
  policy-valid matches have been seen, rg is terminated
- guards the whole run with a wall-clock deadline; on timeout rg is
  terminated, then killed after a grace period — no orphan processes
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time

from . import logging as jsonlog
from .fdio import proc_fd_path
from .models import TextMatch
from .paths import RESERVED_RG_EXCLUDES, ResolvedPath, is_hidden_component

_RG_BENIGN_EXIT = {0, 1}
_GRACE_SECONDS = 2.0
_READ_CHUNK = 65536
_REAP_SECONDS = 5.0


class SearchTimeout(Exception):
    """rg exceeded the wall-clock deadline; the process was reaped."""


def _rg_args(
    query: str,
    glob: str | None,
    case_sensitive: bool,
    max_file_bytes: int,
) -> list[str]:
    args = [
        "rg",
        "--fixed-strings",
        "--json",
        "--no-messages",
        "--hidden",
        "--max-filesize",
        str(max_file_bytes),
    ]
    if not case_sensitive:
        args.append("--ignore-case")
    if glob:
        args.extend(["--glob", glob])
    # VCS internals and the reserved names are excluded here so rg never even
    # reads them (temp artifacts of an in-flight mutation, and the
    # disabled-slot sentinel, must not be searchable); hidden/deny policy is
    # enforced on every result path as well (defense in depth), and rg never
    # follows symlinks.
    for excluded in ("!.git", "!.hg", "!.svn", *RESERVED_RG_EXCLUDES):
        args.extend(["--glob", excluded])
    args.append("--")  # everything after is operands: query then path
    args.append(query)
    args.append(".")
    return args


def _parse_match_event(
    line: bytes,
    base_parts: tuple[str, ...],
    resolved: ResolvedPath,
) -> TextMatch | None:
    """Parse one rg --json line; return a policy-passing match or None."""
    try:
        event = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(event, dict) or event.get("type") != "match":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    path_obj = data.get("path")
    if not isinstance(path_obj, dict):
        return None
    ptext = path_obj.get("text")
    if not isinstance(ptext, str):
        return None  # binary / non-UTF-8 path: outside our UTF-8 contract
    line_no = data.get("line_number")
    lines_obj = data.get("lines")
    text = lines_obj.get("text") if isinstance(lines_obj, dict) else None
    if not isinstance(line_no, int) or not isinstance(text, str):
        return None

    parts = tuple(ptext.split("/"))
    if parts and parts[0] == ".":
        parts = parts[1:]
    if not parts:
        return None
    # rg runs with cwd at the search root FD, so its paths are already
    # relative to that root; the workdir-relative result is simply
    # base_parts + rg-relative parts (a repeated directory name such as
    # foo/foo/test.txt must NOT be collapsed).
    if not resolved.allow_hidden and any(is_hidden_component(seg) for seg in parts):
        return None
    full = (*base_parts, *parts)
    if resolved.deny_policy.is_denied(full):
        return None
    return TextMatch(path="/".join(full), line=line_no, text=text.rstrip("\n"))


def _reap(proc: subprocess.Popen, graceful: bool) -> None:
    """Terminate/kill/reap rg; never leave an orphan."""
    if proc.poll() is not None:
        return
    if graceful:
        try:
            proc.wait(timeout=_REAP_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
    proc.terminate()
    try:
        proc.wait(timeout=_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_search(
    root_fd: int,
    resolved: ResolvedPath,
    *,
    query: str,
    glob: str | None,
    case_sensitive: bool,
    limit: int,
    timeout_seconds: float,
    max_file_bytes: int,
) -> tuple[list[TextMatch], bool]:
    """Stream rg --json from the search root FD; global early-stop at limit.

    Returns (matches, truncated). Reading ``limit + 1`` policy-valid
    matches proves truncation; rg is then terminated instead of scanning
    the rest of the tree.
    """
    base_parts = resolved.rel_parts
    args = _rg_args(query, glob, case_sensitive, max_file_bytes)
    proc = subprocess.Popen(
        args,
        cwd=proc_fd_path(root_fd),
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(root_fd,),
    )
    matches: list[TextMatch] = []
    truncated = False
    timed_out = False
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    sel.register(proc.stderr, selectors.EVENT_READ)
    streams = {proc.stdout: stdout_buf, proc.stderr: stderr_buf}
    deadline = time.monotonic() + timeout_seconds

    try:
        open_count = len(streams)
        early_stop = False
        while open_count > 0 and not early_stop:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in sel.select(timeout=remaining):
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), _READ_CHUNK)
                except (OSError, ValueError):
                    chunk = b""
                if not chunk:
                    sel.unregister(stream)
                    stream.close()
                    open_count -= 1
                    continue
                buf = streams[stream]
                buf.extend(chunk)
                if stream is proc.stdout:
                    while True:
                        nl = stdout_buf.find(b"\n")
                        if nl < 0:
                            break
                        line = bytes(stdout_buf[:nl])
                        del stdout_buf[: nl + 1]
                        match = _parse_match_event(line, base_parts, resolved)
                        if match is not None:
                            matches.append(match)
                            if len(matches) > limit:
                                truncated = True
                                early_stop = True
                                break
                    if early_stop:
                        break
    finally:
        _reap(proc, graceful=not (timed_out or truncated))
        for stream in streams:
            if not stream.closed:
                try:
                    sel.unregister(stream)
                except (KeyError, ValueError):
                    pass
                stream.close()
        sel.close()

    if timed_out:
        raise SearchTimeout()
    if truncated:
        # rg was deliberately terminated at the global limit; a negative or
        # non-zero exit code here is expected, not a failure
        return matches[:limit], truncated
    if proc.returncode not in _RG_BENIGN_EXIT:
        detail = bytes(stderr_buf).decode("utf-8", errors="replace").strip()
        jsonlog.debug("rg_failed", returncode=proc.returncode, stderr=detail[:300])
        raise RuntimeError("SEARCH_FAILED: content search failed.")
    return matches[:limit], truncated
