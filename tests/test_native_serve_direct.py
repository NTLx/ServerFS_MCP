"""Phase D tests for the direct native serve contract (§6.2, §4.2, §15 D3/D5; maintainer review B).

The defect: ``cmd_serve`` *required* ``SERVERFS_AGENT_BRIDGE_SOCKET`` and
``SERVERFS_AGENT_LOCK_DIR`` and refused to start without them. That made a direct
``serverfs serve`` dependent on a supervisor that, by design, is not its launcher — so the frozen
"direct serve is a legal native entry" semantics could not actually be exercised.

The endpoint is now **derived** from the frozen contract: current user SID -> sha256 -> pipe name,
and the frozen data home -> lock directory. Two properties are therefore under test.

**Derivation is correct and agrees with the Bridge.** §23/§70 forbid importing
``serverfs_agent_bridge``, and Phase B requires the two sides to derive the name independently. The
parity test drives the Bridge's own function in its own environment and compares the result, which
is the design working as intended rather than a limitation.

**Injection is an override, never a second identity.** A supervisor may inject the same values; a
mismatch is a startup refusal rather than an override, because letting internal env rewrite the
operator's identity would put a supervisor and a direct serve in different pipe and lease
universes.

Cases B and C below run with the wiring variables **deleted**, not pre-injected. An earlier version
of this file proved the direct-serve path by injecting the values first, which is the thing under
test and therefore proved nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from serverfs_mcp.cli import _native_agent_settings
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.native_config import load_native_config
from serverfs_mcp.native_endpoint import (
    ENDPOINT_ENV,
    LOCK_DIR_ENV,
    NativeEndpointError,
    derive_endpoint,
    derive_lock_dir,
    derive_pipe_name,
    resolve_native_agent_wiring,
)
from serverfs_mcp.workdirs import WorkdirRegistry

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native serve")

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PYTHON = REPO_ROOT / "agent_bridge" / ".venv" / "Scripts" / "python.exe"

AGENT_TOOLS = {
    "submit_agent_task",
    "get_agent_task",
    "list_agent_runtimes",
    "cancel_agent_task",
    "respond_agent_approval",
    "read_agent_task_events",
    "read_agent_task_result",
    "list_agent_models",
    "send_agent_message",
    "answer_agent_question",
}

V010_CONFIG = """\
[server]
log_level = "INFO"

[[workdirs]]
alias = "repo"
path = "{root}"
read_only = false
"""

AGENT_CONFIG = """\
[server]
log_level = "INFO"

[agent]
enabled = true

[agent.codex]
enabled = true

[[workdirs]]
alias = "repo"
path = "{root}"
read_only = false
agent_mode = "workspace-write"
agent_runtimes = ["codex"]
"""


def _write(tmp_path: Path, template: str, root: Path) -> Path:
    config = tmp_path / "serverfs.toml"
    config.write_text(template.format(root=str(root).replace("\\", "\\\\")), encoding="utf-8")
    return config


def _settings_for(config_path: Path):
    _workdirs, native_settings = load_native_config(config_path)
    return native_settings


def _tool_names(settings: Settings, config_path: Path) -> set[str]:
    registry = WorkdirRegistry(load_native_config(config_path)[0])
    client = None
    if settings.agent_bridge_enabled:
        from serverfs_mcp.agent_client import AgentBridgeClient

        client = AgentBridgeClient(Path(settings.agent_bridge_socket), timeout_seconds=2.0)
    server = create_server(settings, registry, client)

    async def _names() -> set[str]:
        return {tool.name for tool in await server.list_tools()}

    import asyncio

    return asyncio.run(_names())


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.fixture()
def no_wiring(monkeypatch, tmp_path: Path):
    """Direct serve with the supervisor's wiring genuinely absent, and a known data home."""
    monkeypatch.delenv(ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(LOCK_DIR_ENV, raising=False)
    monkeypatch.setenv("SERVERFS_DATA_HOME", str(tmp_path / "data-home"))
    return tmp_path


class TestDerivation:
    """The endpoint comes from the frozen contract, not from the environment."""

    def test_endpoint_is_derived_without_any_injected_value(self, no_wiring):
        endpoint = derive_endpoint()
        assert endpoint.startswith("\\\\.\\pipe\\serverfs-agent-bridge-v1-")
        # 16 hex characters of SID-derived suffix, nothing else.
        suffix = endpoint.rsplit("-", 1)[-1]
        assert len(suffix) == 16
        assert all(char in "0123456789abcdef" for char in suffix)

    def test_lock_dir_is_derived_from_the_data_home(self, no_wiring):
        assert derive_lock_dir() == no_wiring / "data-home" / "agent-bridge" / "locks"

    def test_endpoint_is_stable_across_calls(self, no_wiring):
        assert derive_endpoint() == derive_endpoint()

    def test_lock_dir_follows_localappdata_when_no_override(self, monkeypatch, tmp_path: Path):
        monkeypatch.delenv("SERVERFS_DATA_HOME", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
        assert (
            derive_lock_dir()
            == tmp_path / "AppData" / "Local" / "ServerFS" / "agent-bridge" / "locks"
        )

    def test_derivation_fails_closed_without_any_data_home(self, monkeypatch, tmp_path: Path):
        monkeypatch.delenv("SERVERFS_DATA_HOME", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        with pytest.raises(NativeEndpointError, match="cannot be determined"):
            derive_lock_dir()

    def test_a_non_canonical_sid_is_refused(self):
        with pytest.raises(NativeEndpointError, match="canonical user SID"):
            derive_pipe_name("not-a-sid")

    def test_a_lowercase_sid_is_refused_exactly_as_the_bridge_refuses_it(self):
        """Parity on the rejection too, not only on the accepted spelling.

        The Bridge validates ``startswith("S-")`` before uppercasing, so a lowercase SID is refused
        there and must be refused here for the same input. Asserting the opposite would pin a
        divergence between the two implementations.
        """
        with pytest.raises(NativeEndpointError, match="canonical user SID"):
            derive_pipe_name("s-1-5-21-a-b-c-1001")

    def test_sub_authority_digits_are_hashed_as_given(self):
        """Only the prefix is validated; the rest is hashed verbatim, as the Bridge does."""
        assert derive_pipe_name("S-1-5-21-A-B-C-1001") == derive_pipe_name("S-1-5-21-A-B-C-1001")
        assert derive_pipe_name("S-1-5-21-A-B-C-1001") != derive_pipe_name("S-1-5-21-A-B-C-1002")


class TestParityWithTheBridge:
    """Independent derivation, pinned by comparing the two implementations."""

    def test_both_packages_derive_the_same_pipe_name(self):
        """Neither imports the other; Phase B requires independent derivation to agree."""
        if not BRIDGE_PYTHON.exists():
            pytest.skip(f"bridge virtualenv interpreter is missing at {BRIDGE_PYTHON}")
        script = (
            "import json,sys\n"
            "from pathlib import Path\n"
            "from serverfs_agent_bridge.local_ipc import derive_pipe_name\n"
            "from serverfs_agent_bridge.windows_security import current_user_sid\n"
            "print(json.dumps({'sid': current_user_sid(),\n"
            "                  'pipe': derive_pipe_name(current_user_sid())}))\n"
        )
        completed = subprocess.run(
            [str(BRIDGE_PYTHON), "-c", script],
            capture_output=True,
            cwd=str(REPO_ROOT / "agent_bridge"),
        )
        assert completed.returncode == 0, completed.stderr.decode(errors="replace")
        theirs = json.loads(completed.stdout.decode("utf-8"))
        # Our derivation, from our own SID helper, must equal theirs.
        assert derive_pipe_name(theirs["sid"]) == theirs["pipe"]
        assert derive_endpoint() == theirs["pipe"]

    def test_both_packages_derive_the_same_lock_dir(self, no_wiring):
        if not BRIDGE_PYTHON.exists():
            pytest.skip(f"bridge virtualenv interpreter is missing at {BRIDGE_PYTHON}")
        script = (
            "import json,os,sys\n"
            "from serverfs_agent_bridge.data_home import bridge_data_home\n"
            "print(json.dumps({'locks': str(bridge_data_home() / 'locks')}))\n"
        )
        completed = subprocess.run(
            [str(BRIDGE_PYTHON), "-c", script],
            capture_output=True,
            cwd=str(REPO_ROOT / "agent_bridge"),
            env={**os.environ, "SERVERFS_DATA_HOME": str(no_wiring / "data-home")},
        )
        assert completed.returncode == 0, completed.stderr.decode(errors="replace")
        theirs = json.loads(completed.stdout.decode("utf-8"))["locks"]
        assert str(derive_lock_dir()).lower() == theirs.lower()


class TestInjectionIsAnOverrideNotAnIdentity:
    """Injected wiring may confirm the derivation; it may not replace it."""

    def test_matching_injected_values_are_accepted(self, no_wiring, monkeypatch):
        endpoint, lock_dir = derive_endpoint(), derive_lock_dir()
        monkeypatch.setenv(ENDPOINT_ENV, endpoint)
        monkeypatch.setenv(LOCK_DIR_ENV, str(lock_dir))
        assert resolve_native_agent_wiring() == (endpoint, lock_dir)

    def test_absent_injection_is_accepted(self, no_wiring):
        endpoint, lock_dir = derive_endpoint(), derive_lock_dir()
        assert resolve_native_agent_wiring() == (endpoint, lock_dir)

    def test_a_different_injected_endpoint_is_refused(self, no_wiring, monkeypatch):
        monkeypatch.setenv(ENDPOINT_ENV, r"\\.\pipe\serverfs-agent-bridge-v1-0000000000000000")
        with pytest.raises(NativeEndpointError, match="disagrees"):
            resolve_native_agent_wiring()

    def test_a_different_injected_lock_dir_is_refused(self, no_wiring, monkeypatch):
        monkeypatch.setenv(LOCK_DIR_ENV, str(no_wiring / "somewhere-else"))
        with pytest.raises(NativeEndpointError, match="disagrees"):
            resolve_native_agent_wiring()

    def test_a_lock_dir_matching_ignores_separator_and_case(self, no_wiring, monkeypatch):
        lock_dir = derive_lock_dir()
        monkeypatch.setenv(LOCK_DIR_ENV, str(lock_dir).upper().replace("/", "\\"))
        assert resolve_native_agent_wiring()[1] == lock_dir

    def test_refusal_names_the_class_not_the_values(self, no_wiring, monkeypatch):
        monkeypatch.setenv(ENDPOINT_ENV, r"\\.\pipe\secret-0000000000000000")
        with pytest.raises(NativeEndpointError) as raised:
            resolve_native_agent_wiring()
        message = str(raised.value)
        assert "secret" not in message
        assert "0000000000000000" not in message


class TestDirectServeContract:
    """Cases A–C from the review, with the wiring genuinely absent."""

    def test_a_agent_absent_gives_the_filesystem_only_surface(
        self, tmp_path: Path, workdir: Path, no_wiring
    ):
        config = _write(tmp_path, V010_CONFIG, workdir)
        assert _native_agent_settings(_settings_for(config)) is None
        names = _tool_names(Settings(log_level="INFO"), config)
        assert len(names) == 11, sorted(names)
        assert not names & AGENT_TOOLS

    def test_b_agent_enabled_without_wiring_exposes_exactly_ten_agent_tools(
        self, tmp_path: Path, workdir: Path, no_wiring
    ):
        """Case B: no SERVERFS_AGENT_BRIDGE_SOCKET and no SERVERFS_AGENT_LOCK_DIR."""
        assert ENDPOINT_ENV not in os.environ
        assert LOCK_DIR_ENV not in os.environ
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        settings, client = _native_agent_settings(_settings_for(config))
        assert settings.agent_bridge_enabled is True
        assert client is not None
        names = _tool_names(settings, config)
        assert names & AGENT_TOOLS == AGENT_TOOLS
        assert len(AGENT_TOOLS) == 10
        assert len(names) == 21, sorted(names)

    def test_c_agent_call_without_a_live_bridge_fails_closed(
        self, tmp_path: Path, workdir: Path, no_wiring
    ):
        """Case C: the tools are present and an actual call fails with the frozen code."""
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        settings, client = _native_agent_settings(_settings_for(config))
        server = create_server(settings, WorkdirRegistry(load_native_config(config)[0]), client)

        from helpers import call_error, error_code

        message = call_error(server, "list_agent_runtimes", {"workdir": "repo"})
        assert error_code(message) == "AGENT_BRIDGE_UNAVAILABLE", message
        # No endpoint, SID or Win32 detail reaches the agent-visible text.
        assert "pipe" not in message.lower()
        assert "S-1-5-21" not in message

    def test_d_matching_injection_is_accepted(
        self, tmp_path: Path, workdir: Path, no_wiring, monkeypatch
    ):
        endpoint, lock_dir = derive_endpoint(), derive_lock_dir()
        monkeypatch.setenv(ENDPOINT_ENV, endpoint)
        monkeypatch.setenv(LOCK_DIR_ENV, str(lock_dir))
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        settings, _client = _native_agent_settings(_settings_for(config))
        assert settings.agent_bridge_socket == endpoint
        assert settings.agent_lock_dir == str(lock_dir)

    def test_e_mismatched_endpoint_refuses_startup(
        self, tmp_path: Path, workdir: Path, no_wiring, monkeypatch
    ):
        monkeypatch.setenv(ENDPOINT_ENV, r"\\.\pipe\serverfs-agent-bridge-v1-0000000000000000")
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        with pytest.raises(SystemExit):
            _native_agent_settings(_settings_for(config))

    def test_f_mismatched_lock_dir_refuses_startup(
        self, tmp_path: Path, workdir: Path, no_wiring, monkeypatch
    ):
        monkeypatch.setenv(LOCK_DIR_ENV, str(no_wiring / "elsewhere"))
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        with pytest.raises(SystemExit):
            _native_agent_settings(_settings_for(config))

    def test_ambient_env_cannot_enable_delegation(
        self, tmp_path: Path, workdir: Path, no_wiring, monkeypatch
    ):
        monkeypatch.setenv("SERVERFS_AGENT_BRIDGE_ENABLED", "1")
        config = _write(tmp_path, V010_CONFIG, workdir)
        assert _settings_for(config).agent_enabled is False
        assert _native_agent_settings(_settings_for(config)) is None

    def test_log_level_still_comes_from_the_toml(self, tmp_path: Path, workdir: Path, no_wiring):
        template = AGENT_CONFIG.replace('log_level = "INFO"', 'log_level = "DEBUG"')
        config = _write(tmp_path, template, workdir)
        settings, _client = _native_agent_settings(_settings_for(config))
        assert settings.log_level == "DEBUG"
