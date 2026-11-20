"""I7: protect Cloudflare Token handoff; no real credentials or network."""

from __future__ import annotations

import ast
import base64
import importlib.util
import json
from pathlib import Path

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i7-connectors-disabled-register.py"
)
_SPEC = importlib.util.spec_from_file_location("i7_disabled_register_source", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
register = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(register)


def test_fixed_ids_require_real_separate_provisioned_tokens() -> None:
    assert register.INSTANCE == "i7-public"
    assert register.IDS == ("i7-edge-a", "i7-edge-b")
    assert register.TOKEN_NAME == "tunnel.token"
    assert set(register.EXPECTED_TUNNELS) == set(register.IDS)
    assert len(set(register.EXPECTED_TUNNELS.values())) == 2
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    assert tree is not None
    source = _SOURCE.read_text(encoding="utf-8")
    for mandatory in (
        "prepare._installed_core_and_binary()",
        "prepare.candidates_absent()",
        "storage.check_private(",
        "private_token_digest(",
        "digest not in digests",
        "token_gate(allow_missing=False)",
        "_preflight(manifest)",
        "_assert_instance_isolation(manifest)",
        "storage.stage(manifest)",
        "scheduler.install(manifest)",
        "I7_TWO_REAL_CONNECTORS_REGISTERED_DISABLED=PASS",
    ):
        assert mandatory in source
    for forbidden in (
        "Scheduler().start(",
        "scheduler.start(",
        "subprocess.Popen(",
        "cloudflare.request(",
        "cloudflared tunnel",
        "git push",
        "get_tunnel_token",
        "token_secret =",
    ):
        assert forbidden not in source


def _homes(monkeypatch: pytest.MonkeyPatch, base: Path) -> Path:
    from serverfs_mcp.windows_v25 import storage

    homes = base / "connectors"
    for name in register.IDS:
        (homes / name).mkdir(parents=True)
    monkeypatch.setattr(register, "host", lambda: None)
    monkeypatch.setattr(storage, "_sid", lambda: "fixture")
    monkeypatch.setattr(storage, "check_private", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        storage,
        "home",
        lambda component, instance, connector: homes / connector,
    )
    return homes


def test_absent_handoff_does_not_register_or_qualify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _homes(monkeypatch, tmp_path)
    monkeypatch.setattr(register, "base", lambda: (None, tmp_path, tmp_path))
    assert register.audit() is False
    assert register.token_gate(allow_missing=True) is False
    with pytest.raises(register.I7RegistrationRefusal, match="two private Tunnel Tokens"):
        register.token_gate(allow_missing=False)
    with pytest.raises(register.I7RegistrationRefusal, match="two private Tunnel Tokens"):
        register.stage()


def _fake_cloudflared_token(
    tunnel: str = register.EXPECTED_TUNNELS["i7-edge-a"],
    *,
    account: str = "a" * 32,
    secret: bytes = b"A" * 32,
) -> bytes:
    """Mimic upstream cloudflared Base64(JSON {a,s,t}) without real credentials."""
    payload = {
        "a": account,
        "s": base64.b64encode(secret).decode("ascii"),
        "t": tunnel,
    }
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def test_token_file_minimum_sanity_and_two_unique_digest_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    homes = _homes(monkeypatch, tmp_path)
    a = homes / register.IDS[0] / register.TOKEN_NAME
    b = homes / register.IDS[1] / register.TOKEN_NAME
    a.write_bytes(_fake_cloudflared_token())
    b.write_bytes(_fake_cloudflared_token(register.EXPECTED_TUNNELS["i7-edge-b"]))
    assert register.token_gate(allow_missing=False) is True
    b.write_bytes(a.read_bytes())
    with pytest.raises(register.I7RegistrationRefusal, match="duplicated"):
        register.token_gate(allow_missing=False)
    b.write_bytes(b"NO_VALID_CLOUDFLARE_TUNNEL_TOKEN")
    with pytest.raises(register.I7RegistrationRefusal, match="size is invalid"):
        register.token_gate(allow_missing=False)
    b.write_bytes(_fake_cloudflared_token() + b"\n")
    with pytest.raises(register.I7RegistrationRefusal, match="minimum sanity check"):
        register.token_gate(allow_missing=False)


def test_token_rotation_same_tunnel_uuid_is_not_second_connector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    homes = _homes(monkeypatch, tmp_path)
    first = homes / register.IDS[0] / register.TOKEN_NAME
    second = homes / register.IDS[1] / register.TOKEN_NAME
    first.write_bytes(_fake_cloudflared_token(secret=b"A" * 32))
    second.write_bytes(_fake_cloudflared_token(secret=b"B" * 32))
    assert first.read_bytes() != second.read_bytes()
    with pytest.raises(register.I7RegistrationRefusal, match="Tunnel UUID is reused"):
        register.token_gate(allow_missing=False)


def test_tokens_from_different_accounts_do_not_share_one_i7_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    homes = _homes(monkeypatch, tmp_path)
    (homes / register.IDS[0] / register.TOKEN_NAME).write_bytes(_fake_cloudflared_token())
    (homes / register.IDS[1] / register.TOKEN_NAME).write_bytes(
        _fake_cloudflared_token(
            register.EXPECTED_TUNNELS["i7-edge-b"],
            account="b" * 32,
        )
    )
    with pytest.raises(register.I7RegistrationRefusal, match="accounts differ"):
        register.token_gate(allow_missing=False)


def test_swapped_tunnel_credentials_are_rejected_even_if_both_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    homes = _homes(monkeypatch, tmp_path)
    (homes / register.IDS[0] / register.TOKEN_NAME).write_bytes(
        _fake_cloudflared_token(register.EXPECTED_TUNNELS["i7-edge-b"])
    )
    (homes / register.IDS[1] / register.TOKEN_NAME).write_bytes(
        _fake_cloudflared_token(register.EXPECTED_TUNNELS["i7-edge-a"])
    )
    with pytest.raises(register.I7RegistrationRefusal, match="wrong named Cloudflare Tunnel"):
        register.token_gate(allow_missing=False)


def test_unrecognized_third_tunnel_uuid_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    homes = _homes(monkeypatch, tmp_path)
    (homes / register.IDS[0] / register.TOKEN_NAME).write_bytes(
        _fake_cloudflared_token("00000000-0000-4000-8000-000000000003")
    )
    with pytest.raises(register.I7RegistrationRefusal, match="wrong named Cloudflare Tunnel"):
        register.token_gate(allow_missing=True)


@pytest.mark.parametrize(
    "data",
    [
        b"eyJ" + b"A" * 130,
        base64.b64encode(b'{"a":"a", "t":4, "s":"AA"}'),
        _fake_cloudflared_token(tunnel="not-a-uuid"),
        _fake_cloudflared_token(account="not-a-valid-account"),
        _fake_cloudflared_token(secret=b"tiny"),
        base64.b64encode(b'{"a":"a", "s":"ABC", "t":"123"}'),
    ],
)
def test_malformed_cloudflared_token_envelope_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: bytes
) -> None:
    homes = _homes(monkeypatch, tmp_path)
    token = homes / register.IDS[0] / register.TOKEN_NAME
    token.write_bytes(data)
    with pytest.raises(register.I7RegistrationRefusal):
        register.token_gate(allow_missing=True)


def test_unrecognized_file_in_private_connector_home_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    homes = _homes(monkeypatch, tmp_path)
    (homes / register.IDS[0] / "unapproved.txt").write_text("not token")
    with pytest.raises(register.I7RegistrationRefusal, match="unrecognized"):
        register.token_gate(allow_missing=True)


def test_postflight_explicitly_checks_both_tasks_disabled_and_offline() -> None:
    code = _SOURCE.read_text("utf-8")
    for predicate in (
        'status.get("state") == "Disabled"',
        'status.get("enabled") is False',
        'status.get("running") is False',
        'status.get("readiness") == "offline"',
        "_current_identity(manifest) is None",
    ):
        assert predicate in code
