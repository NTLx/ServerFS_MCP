"""Source-only checks for installed I3 Bridge durable nonce acceptance gate.

These tests never call the native main entry or touch Task Scheduler, any
private installed fixture, HMAC file or IPC listener.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp.agent_client import AgentBridgeRemoteError

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i3-installed-nonce-replay-smoke.py"
)


def module():
    spec = importlib.util.spec_from_file_location("i3_nonce_smoke_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def test_one_sealed_ping_envelope_is_stable_and_signed() -> None:
    obj = module()
    credential = SimpleNamespace(
        bridge_id="i3-shared",
        instance_id="i3-core-a",
        key_id="key-synthetic",
        secret=b"K" * 32,
        policy_revisions=(("Fixture", "a" * 64),),
    )
    encoded, request_id = obj.signed_ping(credential, issued_at=1_900_000_000)
    request = json.loads(encoded)
    assert encoded.endswith(b"\n")
    assert request["request_id"] == request_id
    assert request["issued_at"] == 1_900_000_000
    assert request["core_id"] == "i3-core-a"
    assert request["params"] == {"workdir": "Fixture"}
    assert request["policy_revision"] == "a" * 64
    sig = request.pop("signature")
    expected = hmac.new(
        credential.secret,
        json.dumps(
            request, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(sig, expected)


def test_replay_is_only_accepted_as_denial_for_specific_error() -> None:
    obj = module()

    class Client:
        def __init__(self, code):
            self.code = code

        async def _exchange(self, _message, _request_id):
            if self.code is not None:
                raise AgentBridgeRemoteError(self.code, "redacted")
            return {"status": "ok"}

    assert asyncio.run(obj.denied_replay(Client("CORE_NOT_AUTHORIZED"), b"safe\n", "req")) is None
    with pytest.raises(obj.I3ReplayError, match="replay denial code"):
        asyncio.run(obj.denied_replay(Client("INVALID_REQUEST"), b"safe\n", "req"))
    with pytest.raises(obj.I3ReplayError, match="replay was accepted"):
        asyncio.run(obj.denied_replay(Client(None), b"safe\n", "req"))


def test_installed_nonce_script_never_mutates_grants_or_foreign_tasks() -> None:
    src = _SCRIPT.read_text("utf-8")
    assert 'BRIDGE = "i3-shared"' in src
    assert 'CORE_IDS = ("i3-core-a", "i3-core-b")' in src
    assert "load_native_core_bridge_binding(manifest, workdirs)" in src
    assert "probe.three_task_states(gate)" in src
    assert "0 <= int(time.time()) - issued <= 30" in src
    assert "I3_INSTALLED_NONCE_REPLAY_SAME_GENERATION=DENIED" in src
    assert "I3_INSTALLED_NONCE_REPLAY_AFTER_RESTART=DENIED" in src
    assert "I3_INSTALLED_FRESH_TWO_CORE_GRANTS_AFTER_RESTART=PASS" in src
    assert "I3_INSTALLED_REPLAY_FIXTURE_STOP=" in src
    assert "I3_INSTALLED_REPLAY_PRODUCTION_TASKS_AND_CORE=UNCHANGED" in src
    assert "I3_INSTALLED_DURABLE_NONCE=PASS" in src
    for prohibited in (
        'probe.cli("core"',
        "Scheduler().install(",
        "storage.stage(",
        "provision_private_core_grants(",
        "replace_grants(",
        "Stop-Process",
        "Remove-Item",
        "git push",
        "print(encoded)",
        "print(binding.secret)",
    ):
        assert prohibited not in src
