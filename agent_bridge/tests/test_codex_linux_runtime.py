from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import pytest

from serverfs_agent_bridge.adapters.codex import CodexAdapter
from serverfs_agent_bridge.adapters.codex_linux import LinuxCodexAppServer
from serverfs_agent_bridge.adapters.codex_transport import UnixSocketEndpoint
from serverfs_agent_bridge.bootstrap import RuntimeProxy
from serverfs_agent_bridge.config import CodexSettings
from serverfs_agent_bridge.errors import BridgeError

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="Bridge-owned Codex app-server and Unix socket lifecycle are Linux-only",
)


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode


class FakeVersionProcess:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"codex-cli 0.162.0\n", b""


@pytest.mark.asyncio
async def test_proxy_mode_probe_is_local_and_does_not_start_owned_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = CodexSettings(
        enabled=True,
        codex_home=tmp_path / "codex-home",
        codex_bin="/usr/bin/codex-test",
        use_proxy=True,
    )
    adapter = CodexAdapter(
        settings,
        state_dir=tmp_path / "state",
        runtime_proxy=RuntimeProxy(
            url="http://127.0.0.1:19999",
            no_proxy="127.0.0.1,localhost,::1",
        ),
    )
    assert adapter._linux_runtime is not None
    captured: dict[str, object] = {}

    async def fail_if_started() -> UnixSocketEndpoint:
        raise AssertionError("runtime.list probe must not start the provider app-server")

    async def fake_exec(*argv: str, **kwargs: object) -> FakeVersionProcess:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return FakeVersionProcess()

    monkeypatch.setattr(adapter._linux_runtime, "ensure_started", fail_if_started)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient.invalid:1")

    info = await adapter.probe()
    assert info.available is True
    assert info.version == "0.162.0"
    assert captured["argv"] == ("/usr/bin/codex-test", "--version")
    env = captured["env"]
    assert isinstance(env, dict)
    assert "HTTPS_PROXY" not in env


@pytest.mark.asyncio
async def test_adapter_selects_owned_socket_only_when_proxy_mode_is_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy_settings = CodexSettings(
        enabled=True,
        codex_home=tmp_path / "codex-home",
        codex_bin="/usr/bin/codex-test",
        use_proxy=True,
    )
    adapter = CodexAdapter(
        proxy_settings,
        state_dir=tmp_path / "state",
        runtime_proxy=RuntimeProxy(
            url="http://127.0.0.1:19999",
            no_proxy="127.0.0.1,localhost,::1",
        ),
    )
    assert adapter._linux_runtime is not None
    owned = tmp_path / "state" / "owned.sock"

    async def fake_owned_endpoint() -> UnixSocketEndpoint:
        return UnixSocketEndpoint(owned)

    monkeypatch.setattr(adapter._linux_runtime, "ensure_started", fake_owned_endpoint)
    connection = await adapter._acquire_connection()
    assert isinstance(connection.endpoint, UnixSocketEndpoint)
    assert connection.endpoint.path == owned

    direct_settings = CodexSettings(
        enabled=True,
        codex_home=tmp_path / "direct-codex-home",
        codex_bin="/usr/bin/codex-test",
        use_proxy=False,
    )
    direct = CodexAdapter(direct_settings, state_dir=tmp_path / "state-direct")
    assert direct._linux_runtime is None
    direct_connection = await direct._acquire_connection()
    assert isinstance(direct_connection.endpoint, UnixSocketEndpoint)
    assert direct_connection.endpoint.path == direct_settings.control_socket


@pytest.mark.asyncio
async def test_proxy_mode_owns_standalone_unix_app_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = CodexSettings(
        enabled=True,
        codex_home=tmp_path / "codex-home",
        codex_bin="/usr/bin/codex-test",
        use_proxy=True,
    )
    runtime = LinuxCodexAppServer(
        settings,
        state_dir=tmp_path / "state",
        runtime_proxy=RuntimeProxy(
            url="http://127.0.0.1:19999",
            no_proxy="127.0.0.1,localhost,::1",
        ),
    )
    captured: dict[str, object] = {}
    fake = FakeProcess()

    async def fake_exec(*argv: str, **kwargs: object) -> FakeProcess:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        socket_path = runtime._socket_path  # deterministic lifecycle seam under test
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.close()
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient.invalid:1")
    monkeypatch.setenv("SERVERFS_PROXY_PASSWORD", "must-not-reach-child")

    endpoint = await runtime.ensure_started()
    assert endpoint.path == runtime._socket_path
    assert captured["argv"] == (
        "/usr/bin/codex-test",
        "app-server",
        "--listen",
        f"unix://{runtime._socket_path}",
    )
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:19999"
    assert "127.0.0.1" in env["NO_PROXY"]
    assert env["CODEX_HOME"] == str(tmp_path / "codex-home")
    assert "SERVERFS_PROXY_PASSWORD" not in env

    # Reuse one child for the Bridge lifetime; no second spawn for another connection.
    endpoint2 = await runtime.ensure_started()
    assert endpoint2 == endpoint

    await runtime.close()
    assert fake.terminated is True
    assert fake.killed is False
    assert not runtime._socket_path.exists()


def _runtime_for_socket_policy(tmp_path: Path) -> LinuxCodexAppServer:
    return LinuxCodexAppServer(
        CodexSettings(
            enabled=True,
            codex_home=tmp_path / "codex-home",
            codex_bin="/usr/bin/codex-test",
            use_proxy=True,
        ),
        state_dir=tmp_path / "state",
        runtime_proxy=RuntimeProxy(
            url="http://127.0.0.1:19999",
            no_proxy="127.0.0.1,localhost,::1",
        ),
    )


def test_proxy_mode_accepts_private_same_user_socket_symlink(tmp_path: Path) -> None:
    runtime = _runtime_for_socket_policy(tmp_path)
    runtime._prepare_runtime_dir()
    target_dir = tmp_path / "codex-private"
    target_dir.mkdir(mode=0o700)
    target = target_dir / "real.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(target))
        target.chmod(0o600)
        runtime._socket_path.symlink_to(target)
        assert runtime._socket_is_ready() is True
    finally:
        listener.close()


def test_proxy_mode_rejects_unsafe_socket_symlink_targets(tmp_path: Path) -> None:
    runtime = _runtime_for_socket_policy(tmp_path)
    runtime._prepare_runtime_dir()

    regular_dir = tmp_path / "regular-private"
    regular_dir.mkdir(mode=0o700)
    regular = regular_dir / "not-a-socket"
    regular.write_text("x", encoding="utf-8")
    regular.chmod(0o600)
    runtime._socket_path.symlink_to(regular)
    assert runtime._socket_is_ready() is False
    runtime._socket_path.unlink()

    writable_dir = tmp_path / "writable-parent"
    writable_dir.mkdir(mode=0o700)
    target = writable_dir / "real.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(target))
        target.chmod(0o600)
        writable_dir.chmod(0o777)
        runtime._socket_path.symlink_to(target)
        assert runtime._socket_is_ready() is False
    finally:
        listener.close()


def test_proxy_mode_removes_stale_listener_symlink_without_following_target(tmp_path: Path) -> None:
    runtime = _runtime_for_socket_policy(tmp_path)
    runtime._runtime_dir.mkdir(mode=0o700, parents=True)
    target = tmp_path / "must-survive"
    target.write_text("sentinel", encoding="utf-8")
    runtime._socket_path.symlink_to(target)

    runtime._prepare_runtime_dir()

    assert not runtime._socket_path.exists()
    assert target.read_text(encoding="utf-8") == "sentinel"


@pytest.mark.asyncio
async def test_proxy_mode_refuses_missing_runtime_proxy(tmp_path: Path) -> None:
    settings = CodexSettings(
        enabled=True,
        codex_home=tmp_path / "codex-home",
        codex_bin="/usr/bin/codex-test",
        use_proxy=True,
    )
    runtime = LinuxCodexAppServer(settings, state_dir=tmp_path / "state", runtime_proxy=None)

    with pytest.raises(BridgeError, match="no endpoint is available"):
        await runtime.ensure_started()
