"""Shared fixtures and platform classification for the Agent Bridge suite.

Collection classification (v0.11 Phase B): the files listed below drive the Windows mechanisms
behind the §3 seams — a Named Pipe instance, an impersonated client SID, an NTFS owner-and-DACL
state object. They import `serverfs_agent_bridge.windows_pipe` and `windows_security` at module
scope, and those modules load `kernel32`/`advapi32`, so on another platform they cannot even be
imported. They are not skipped to hide a failure: each one is the positive contract of one
platform's implementation, the Linux twins of these live in `test_protocol.py` and
`test_store.py`, and the platform-neutral rules both platforms must share are in
`test_local_ipc_contract.py`, which the Linux gate also runs.
"""

from __future__ import annotations

import sys

WINDOWS_ONLY_TEST_FILES = [
    "test_windows_pipe_ipc.py",
    "test_windows_peer_identity.py",
    "test_windows_pipe_e2e.py",
    "test_windows_private_state.py",
]

if not sys.platform.startswith("win"):
    collect_ignore = WINDOWS_ONLY_TEST_FILES
