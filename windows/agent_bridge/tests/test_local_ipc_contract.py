"""The platform-neutral half of the local-IPC seam contract (§6, §9, §11, §17).

These are the rules that must hold identically on both platforms and on both CI gates: the pipe
name is derived from the SID alone, the authorization compares a *measured* identity, and the
frozen size bounds are shared. Nothing here imports a Win32 or a POSIX module, so the Linux gate
runs it too.
"""

from __future__ import annotations

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.local_ipc import (
    PIPE_NAME_PREFIX,
    WindowsPeer,
    authorize_windows_peer,
    derive_pipe_name,
)
from serverfs_agent_bridge.protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PIPE_IDLE_TIMEOUT_SECONDS,
    PIPE_POOL_SIZE,
    PROTOCOL_VERSION,
)

SID_A = "S-1-5-21-1-1-1-1001"
SID_B = "S-1-5-21-1-1-1-2002"


def test_pipe_name_is_derived_from_the_sid_alone() -> None:
    first = derive_pipe_name(SID_A)
    assert first == derive_pipe_name(SID_A)
    assert first.startswith(PIPE_NAME_PREFIX)
    suffix = first.removeprefix(PIPE_NAME_PREFIX)
    assert len(suffix) == 16
    assert all(character in "0123456789abcdef" for character in suffix)


def test_only_the_canonical_sid_form_is_accepted() -> None:
    """A lower-cased or otherwise re-spelled identity is refused, not silently normalized."""
    with pytest.raises(BridgeError) as lowered:
        derive_pipe_name(SID_A.lower())
    assert lowered.value.code == "BRIDGE_IDENTITY_UNAVAILABLE"


def test_two_users_get_two_names() -> None:
    assert derive_pipe_name(SID_A) != derive_pipe_name(SID_B)


def test_a_non_canonical_identity_is_refused() -> None:
    for rejected in ("not-a-sid", "Administrator", "lx"):
        with pytest.raises(BridgeError) as raised:
            derive_pipe_name(rejected)
        assert raised.value.code == "BRIDGE_IDENTITY_UNAVAILABLE"


def test_authorization_compares_the_measured_sid() -> None:
    peer = WindowsPeer(SID_A, pid=4242, session_id=1, impersonation_level=2, token_type=1)
    authorize_windows_peer(peer, allowed_sid=SID_A)
    with pytest.raises(BridgeError) as foreign:
        authorize_windows_peer(peer, allowed_sid=SID_B)
    assert foreign.value.code == "PEER_NOT_AUTHORIZED"


def test_an_unmeasured_peer_is_never_authorized() -> None:
    """§9: no identity, no dispatch — a missing measurement is not an empty allow list."""
    with pytest.raises(BridgeError) as unmeasured:
        authorize_windows_peer(None, allowed_sid=SID_A)
    assert unmeasured.value.code == "PEER_NOT_AUTHORIZED"
    assert "never measured" in unmeasured.value.message


def test_the_peer_identity_never_prints_its_sid() -> None:
    """§11: a SID must not reach logs or tool output by way of a repr()."""
    printed = repr(WindowsPeer(SID_A, pid=4242, session_id=1, impersonation_level=2, token_type=1))
    assert SID_A not in printed


def test_the_frozen_ipc_bounds_are_shared() -> None:
    assert PROTOCOL_VERSION == 1
    assert MAX_REQUEST_BYTES == MAX_RESPONSE_BYTES == 1_048_576
    assert PIPE_POOL_SIZE >= 2
    assert PIPE_IDLE_TIMEOUT_SECONDS > 0
