"""I7 Windows no-echo local-only Cloudflare credential import contracts.

No actual credential, TTY session, installed private file or Cloudflare API.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SOURCE = Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i7-local-token-handoff.py"
_SPEC = importlib.util.spec_from_file_location("i7_local_token_handoff", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
handoff = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(handoff)


def _fake_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from serverfs_mcp import windows_private_artifacts
    from serverfs_mcp.windows_v25 import storage

    first = tmp_path / "edge-a"
    second = tmp_path / "edge-b"
    first.mkdir()
    second.mkdir()
    locations = {
        handoff.CONNECTORS[0]: first,
        handoff.CONNECTORS[1]: second,
    }
    monkeypatch.setattr(handoff, "native_user", lambda: None)
    monkeypatch.setattr(handoff, "interactive_console", lambda: True)
    monkeypatch.setattr(
        storage, "home", lambda component, instance, connector: locations[connector]
    )
    monkeypatch.setattr(storage, "_sid", lambda: "fake-sid")
    monkeypatch.setattr(storage, "check_private", lambda *args, **kwargs: None)
    monkeypatch.setattr(windows_private_artifacts, "protect_new_file", lambda *args, **kwargs: None)

    def digest(path, *, sid):
        content = path.read_bytes()
        return hashlib.sha256(content).digest()

    expected_tunnels = {
        "i7-edge-a": "aad24669-98ae-4eea-a54c-d89b872981a5",
        "i7-edge-b": "85ea7057-ea6c-4d2e-b71a-63074fa31441",
    }

    def record(path, *, sid):
        tunnel_id = expected_tunnels["i7-edge-a" if path.parent == first else "i7-edge-b"]
        return digest(path, sid=sid), "a" * 32, tunnel_id

    fake_register = SimpleNamespace(
        INSTANCE=handoff.INSTANCE,
        IDS=handoff.CONNECTORS,
        EXPECTED_TUNNELS=expected_tunnels,
        base=lambda: None,
        private_token_digest=digest,
        private_token_record=record,
    )
    monkeypatch.setattr(handoff, "loader", lambda: fake_register)
    return first, second


def test_operator_token_is_private_created_once_and_not_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = _fake_homes(tmp_path, monkeypatch)
    a_secret = "eyJ" + "A" * 135
    b_secret = "eyJ" + "B" * 135
    handoff.import_private_token("i7-edge-a", read_secret=lambda prompt: a_secret)
    handoff.import_private_token("i7-edge-b", read_secret=lambda prompt: b_secret)
    assert (first / "tunnel.token").read_bytes() == a_secret.encode("ascii")
    assert (second / "tunnel.token").read_bytes() == b_secret.encode("ascii")
    captured = capsys.readouterr()
    assert a_secret not in captured.out + captured.err
    assert b_secret not in captured.out + captured.err
    assert captured.out.count("I7_LOCAL_PRIVATE_TOKEN_IMPORTED=PASS") == 2
    assert "I7_CONNECTOR_SERVICE_NOT_STARTED=PASS" in captured.out
    with pytest.raises(handoff.I7HandoffRefusal, match="already occupies slot"):
        handoff.import_private_token("i7-edge-a", read_secret=lambda prompt: a_secret)


def test_interactive_console_mandatory_before_read_or_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = _fake_homes(tmp_path, monkeypatch)
    monkeypatch.setattr(handoff, "interactive_console", lambda: False)
    read_calls = []
    with pytest.raises(handoff.I7HandoffRefusal, match="interactive Windows console"):
        handoff.import_private_token(
            "i7-edge-a", read_secret=lambda prompt: read_calls.append(prompt)
        )
    assert not read_calls
    assert not any(first.iterdir())


def test_duplicate_token_refused_prior_to_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _fake_homes(tmp_path, monkeypatch)
    fake = "eyJ" + "A" * 135
    (first / "tunnel.token").write_text(fake, encoding="ascii")
    with pytest.raises(handoff.I7HandoffRefusal, match="reused by peer"):
        handoff.import_private_token("i7-edge-b", read_secret=lambda prompt: fake)
    assert not any(second.iterdir())


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        " eyJ" + "A" * 135,
        "eyJ" + "A" * 135 + "\n",
        "eyJ" + "A" * 30,
        "eyJ" + "A" * 5000,
        "eyJ" + "A" * 130 + "*",
        "eyJ" + "A" * 130 + "é",
    ],
)
def test_invalid_token_refused_before_private_file(tmp_path, monkeypatch, invalid) -> None:
    first, _ = _fake_homes(tmp_path, monkeypatch)
    with pytest.raises(handoff.I7HandoffRefusal, match="format invalid"):
        handoff.import_private_token("i7-edge-a", read_secret=lambda prompt: invalid)
    assert not any(first.iterdir())


def test_peer_unexpected_artifact_blocks_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _fake_homes(tmp_path, monkeypatch)
    (second / "unexpected.json").write_text("not a credential", encoding="utf-8")
    with pytest.raises(handoff.I7HandoffRefusal, match="unexpected data"):
        handoff.import_private_token("i7-edge-a", read_secret=lambda prompt: "eyJ" + "A" * 135)
    assert not any(first.iterdir())


@pytest.mark.parametrize("failure", ["acl", "postwrite"])
def test_failed_secret_acl_or_validation_removes_only_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from serverfs_mcp import windows_private_artifacts
    from serverfs_mcp.windows_v25 import storage

    first, second = _fake_homes(tmp_path, monkeypatch)
    peer = second / "tunnel.token"
    peer.write_bytes(b"eyJ" + b"B" * 135)

    def fail_acl(*args, **kwargs):
        raise RuntimeError("synthetic private DACL failure")

    def fail_postwrite(path, *, directory, sid):
        if not directory:
            raise RuntimeError("synthetic source validation failure")

    if failure == "acl":
        monkeypatch.setattr(windows_private_artifacts, "protect_new_file", fail_acl)
    else:
        monkeypatch.setattr(storage, "check_private", fail_postwrite)
    with pytest.raises(RuntimeError, match="synthetic"):
        handoff.import_private_token("i7-edge-a", read_secret=lambda prompt: "eyJ" + "A" * 135)
    assert not any(first.iterdir())
    assert peer.read_bytes() == b"eyJ" + b"B" * 135


def test_wrong_named_tunnel_after_private_write_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _fake_homes(tmp_path, monkeypatch)
    register = handoff.loader()
    monkeypatch.setattr(
        register,
        "private_token_record",
        lambda path, *, sid: (
            hashlib.sha256(path.read_bytes()).digest(),
            "a" * 32,
            register.EXPECTED_TUNNELS["i7-edge-b"],
        ),
    )
    with pytest.raises(handoff.I7HandoffRefusal, match="wrong named Cloudflare Tunnel"):
        handoff.import_private_token("i7-edge-a", read_secret=lambda prompt: "eyJ" + "A" * 135)
    assert not any(first.iterdir())
    assert not any(second.iterdir())


def test_wrong_existing_peer_tunnel_refused_without_hidden_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _fake_homes(tmp_path, monkeypatch)
    peer = second / "tunnel.token"
    peer.write_bytes(b"eyJ" + b"B" * 135)
    register = handoff.loader()
    monkeypatch.setattr(
        register,
        "private_token_record",
        lambda path, *, sid: (
            hashlib.sha256(path.read_bytes()).digest(),
            "a" * 32,
            register.EXPECTED_TUNNELS["i7-edge-a"],
        ),
    )
    input_calls = []
    with pytest.raises(handoff.I7HandoffRefusal, match="peer credential"):
        handoff.import_private_token(
            "i7-edge-a", read_secret=lambda prompt: input_calls.append(prompt)
        )
    assert not input_calls
    assert not any(first.iterdir())
    assert peer.read_bytes() == b"eyJ" + b"B" * 135


def test_only_local_tty_secret_input_and_no_cli_secret_argument() -> None:
    body = _SOURCE.read_text(encoding="utf-8")
    for expected in (
        "getpass.getpass",
        "interactive_console()",
        "protect_new_file(token, sid)",
        "storage.check_private(token, directory=False, sid=sid)",
        'token.open("xb")',
        "os.fsync(stream.fileno())",
        "I7_TOKEN_NOT_LOGGED_OR_EXPORTED=PASS",
    ):
        assert expected in body
    for forbidden in (
        "cloudflare.request(",
        "requests.post(",
        "Scheduler().start(",
        "subprocess.Popen(",
        "git push",
        "--token",
    ):
        assert forbidden not in body
