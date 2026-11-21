"""Win32 Core TCP owner proof: synthetic table snapshots; no live socket mutation."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from serverfs_mcp import windows_core_control as owner
from serverfs_mcp import windows_core_listener as listener


def _row(ip: str, port: int, pid: int, *, state: int = 2) -> bytes:
    import socket

    addr = int.from_bytes(socket.inet_aton(ip), "little")
    native_port = int.from_bytes(port.to_bytes(2, "big") + b"\x00\x00", "little")
    return struct.pack("<6I", state, addr, native_port, 0, 0, pid)


def test_parses_native_ipv4_owner_pid_table() -> None:
    payload = struct.pack("<I", 3) + (
        _row("127.0.0.1", 19876, 4242)
        + _row("0.0.0.0", 8080, 33)
        + _row("127.0.0.1", 1234, 9, state=5)
    )
    assert listener._parse_ipv4_listeners(payload) == (
        listener.ListenerRow("127.0.0.1", 19876, 4242),
        listener.ListenerRow("0.0.0.0", 8080, 33),
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x01",
        struct.pack("<I", 1),
        struct.pack("<I", 99) + _row("127.0.0.1", 19876, 4242),
    ],
)
def test_truncated_or_corrupt_table_refuses(payload: bytes) -> None:
    with pytest.raises(listener.ListenerProofError):
        listener._parse_ipv4_listeners(payload)


@pytest.fixture
def owned_core(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from serverfs_mcp import windows_process

    prefix = tmp_path / "core-owner-env"
    exe = prefix / "Scripts" / "pythonw.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"synthetic-pythonw")
    console = exe.with_name("python.exe")
    console.write_bytes(b"synthetic-console")
    monkeypatch.setattr(listener.sys, "prefix", str(prefix))
    monkeypatch.setattr(listener.sys, "executable", str(console))
    manifest = {"port": 19876, "pythonw": str(exe), "hostname": "core.example.test"}
    state = {
        "component": "mcp-core",
        "stage": "owner-started",
        "core_pid": 4242,
        "core_creation_time": 99999,
        "core_image_path": str(exe),
    }
    monkeypatch.setattr(owner, "_require_owned", lambda: (manifest, "<Task />"))
    monkeypatch.setattr(owner, "_read_state", lambda: state)
    checks = []
    monkeypatch.setattr(windows_process, "same_process", lambda x: checks.append(x) or True)
    return manifest, state, checks


def test_verified_loopback_listener_matches_exact_process_generation(owned_core) -> None:
    _, _, checks = owned_core
    proof = listener.prove_core_listener(
        expected_port=19876,
        observed_rows=(listener.ListenerRow("127.0.0.1", 19876, 4242),),
    )
    assert proof.pid == 4242
    assert proof.creation_time == 99999
    assert proof.address == "127.0.0.1"
    assert len(checks) == 2
    assert all(x.creation_time == 99999 for x in checks)


@pytest.mark.parametrize(
    "rows",
    [
        (),
        (listener.ListenerRow("127.0.0.1", 19876, 100),),
        (listener.ListenerRow("0.0.0.0", 19876, 4242),),
        (listener.ListenerRow("192.168.1.2", 19876, 4242),),
        (
            listener.ListenerRow("127.0.0.1", 19876, 4242),
            listener.ListenerRow("0.0.0.0", 19876, 100),
        ),
        (
            listener.ListenerRow("127.0.0.1", 19876, 4242),
            listener.ListenerRow("127.0.0.1", 19876, 4242),
        ),
    ],
)
def test_foreign_wildcard_missing_or_duplicate_listener_refused(
    owned_core, rows: tuple[listener.ListenerRow, ...]
) -> None:
    with pytest.raises(listener.ListenerProofError, match="listener"):
        listener.prove_core_listener(expected_port=19876, observed_rows=rows)


def test_peer_connector_venv_proves_core_base_pythonw_listener(
    owned_core, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, state, checks = owned_core
    core_pythonw = Path(manifest["pythonw"])
    core_prefix = core_pythonw.parent.parent
    base = tmp_path / "CPython"
    base.mkdir()
    basew = base / "pythonw.exe"
    basew.write_bytes(b"base-windows-pythonw")
    (core_prefix / "pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
    state["core_image_path"] = str(basew)
    peer = tmp_path / "cloudflare-connector-env"
    console = peer / "Scripts" / "python.exe"
    console.parent.mkdir(parents=True)
    console.write_bytes(b"peer-console")
    monkeypatch.setattr(listener.sys, "prefix", str(peer))
    monkeypatch.setattr(listener.sys, "executable", str(console))
    proof = listener.prove_core_listener(
        expected_port=19876,
        observed_rows=(listener.ListenerRow("127.0.0.1", 19876, 4242),),
    )
    assert proof.image_path == basew
    assert len(checks) == 2


def test_listener_wrong_public_host_refuses(owned_core) -> None:
    _, state, _ = owned_core
    with pytest.raises(listener.ListenerProofError, match="public Host"):
        listener.prove_core_listener(
            expected_port=19876,
            expected_hostname="foreign.example.test",
            observed_rows=(listener.ListenerRow("127.0.0.1", 19876, 4242),),
        )


def test_listener_wrong_port_or_manifest_mismatch_refuses(owned_core) -> None:
    with pytest.raises(listener.ListenerProofError, match="different port"):
        listener.prove_core_listener(expected_port=9876, observed_rows=())


@pytest.mark.parametrize(
    "key,value",
    [
        ("stage", "disabled-never-started"),
        ("component", "agent-bridge"),
        ("core_pid", False),
        ("core_creation_time", 0),
        ("core_image_path", "C:/wrong/pythonw.exe"),
    ],
)
def test_tampered_generation_state_refused(owned_core, key: str, value: object) -> None:
    _, state, _ = owned_core
    state[key] = value
    with pytest.raises(listener.ListenerProofError, match="generation"):
        listener.prove_core_listener(
            expected_port=19876,
            observed_rows=(listener.ListenerRow("127.0.0.1", 19876, 4242),),
        )


def test_pid_creation_time_change_after_socket_snapshot_refuses(
    owned_core, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp import windows_process

    calls = []
    monkeypatch.setattr(
        windows_process,
        "same_process",
        lambda identity: calls.append(identity) or len(calls) == 1,
    )
    with pytest.raises(listener.ListenerProofError, match="changed"):
        listener.prove_core_listener(
            expected_port=19876,
            observed_rows=(listener.ListenerRow("127.0.0.1", 19876, 4242),),
        )
    assert len(calls) == 2


def test_unavailable_task_ownership_refused_before_os_socket_lookup(monkeypatch) -> None:
    from serverfs_mcp import windows_core_control as control

    def missing():
        raise control.CoreControlError("unregistered")

    monkeypatch.setattr(control, "_require_owned", missing)
    monkeypatch.setattr(
        listener,
        "_windows_ipv4_tcp_listeners",
        lambda: pytest.fail("must not query socket owner for invalid Core"),
    )
    with pytest.raises(listener.ListenerProofError, match="registered"):
        listener.prove_core_listener(expected_port=19876)


def test_invalid_port_refuses_before_task_ownership_check(owned_core) -> None:
    for value in (0, 65536, True):
        with pytest.raises(listener.ListenerProofError, match="port"):
            listener.prove_core_listener(expected_port=value)
