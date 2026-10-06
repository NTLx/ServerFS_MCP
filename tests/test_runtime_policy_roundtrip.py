"""Round-trip: serverfs.toml -> native config -> render request -> bridge.json -> BridgeConfig.

The maintainer review found that ``render_request_from_config`` sent only runtime *names*, so
``codex_bin`` / ``claude_bin`` / ``qoder_bin`` and ``use_proxy`` were all dropped on the way into
``bridge.json``. The Bridge then loaded its own defaults, and ``use_proxy=True`` silently became
``use_proxy=False`` — which matters because that flag is what decides whether a provider child
receives the Agent proxy at all.

The two packages are deliberately independent (§23/§70) and neither virtualenv can import the
other, so this test crosses the boundary the way production does: the render request is produced in
this process, then handed to the ``agent_bridge`` renderer CLI as a subprocess, and the resulting
``bridge.json`` is read back through the real ``BridgeConfig`` loader. That is a stronger proof than
an in-process import would be, because it also proves the document survives the real transport.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from serverfs_mcp.agent_lifecycle import render_request_from_config
from serverfs_mcp.native_config import load_native_config

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native deployment shape")

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PYTHON = REPO_ROOT / "agent_bridge" / ".venv" / "Scripts" / "python.exe"


def _root(config_body: str, root: Path) -> str:
    """A config whose workdir allowlists exactly the runtimes the body configures.

    Deriving the allowlist rather than hard-coding all three keeps each case minimal and lets the
    cross-validation in native_config do its job: a workdir may not allowlist a disabled runtime.
    """
    escaped = str(root).replace("\\", "\\\\")
    configured = [name for name in ("codex", "claude", "qoder") if f"[agent.{name}]" in config_body]
    runtimes = ", ".join(f'"{name}"' for name in configured) or '"codex"'
    if not configured:
        config_body = "[agent.codex]\nenabled = true\n\n" + config_body
    return (
        '[server]\nlog_level = "INFO"\n\n[agent]\nenabled = true\n\n'
        f"{config_body}\n"
        '[[workdirs]]\nalias = "repo"\n'
        f'path = "{escaped}"\n'
        'read_only = false\nagent_mode = "workspace-write"\n'
        f"agent_runtimes = [{runtimes}]\n"
    )


def _render(tmp_path: Path, config_text: str, workdir: Path):
    """serverfs.toml -> native config -> render request -> renderer subprocess -> bridge.json."""
    config = tmp_path / "serverfs.toml"
    config.write_text(config_text, encoding="utf-8")
    workdirs, settings = load_native_config(config)
    request = render_request_from_config(workdirs, settings)

    if not BRIDGE_PYTHON.exists():
        pytest.skip(f"bridge virtualenv interpreter is missing at {BRIDGE_PYTHON}")
    completed = subprocess.run(
        [
            str(BRIDGE_PYTHON),
            "-m",
            "serverfs_agent_bridge.render_config",
            "--data-home",
            str(tmp_path / "bridge-home"),
        ],
        input=json.dumps(request).encode("utf-8"),
        capture_output=True,
        cwd=str(REPO_ROOT / "agent_bridge"),
    )
    if completed.returncode != 0:
        pytest.fail(
            f"the renderer refused the request: {completed.stderr.decode(errors='replace')}"
        )
    document = json.loads((tmp_path / "bridge-home" / "bridge.json").read_text(encoding="utf-8"))
    return request, document


def _load_bridge_config(path: Path):
    """Read bridge.json back through the real Bridge loader, in the Bridge's own environment."""
    if not BRIDGE_PYTHON.exists():
        pytest.skip(f"bridge virtualenv interpreter is missing at {BRIDGE_PYTHON}")
    script = (
        "import json,sys\n"
        "from pathlib import Path\n"
        "from serverfs_agent_bridge.config import BridgeConfig\n"
        "c = BridgeConfig.load(Path(sys.argv[1]))\n"
        "print(json.dumps({\n"
        "  'codex_bin': c.codex.codex_bin, 'codex_use_proxy': c.codex.use_proxy,\n"
        "  'claude_bin': c.claude.claude_bin, 'claude_use_proxy': c.claude.use_proxy,\n"
        "  'qoder_bin': c.qoder.qoder_bin, 'qoder_use_proxy': c.qoder.use_proxy,\n"
        "  'codex_enabled': c.codex.enabled,\n"
        "}))\n"
    )
    completed = subprocess.run(
        [str(BRIDGE_PYTHON), "-c", script, str(path)],
        capture_output=True,
        cwd=str(REPO_ROOT / "agent_bridge"),
    )
    if completed.returncode != 0:
        pytest.fail(f"BridgeConfig.load failed: {completed.stderr.decode(errors='replace')}")
    return json.loads(completed.stdout.decode("utf-8"))


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


class TestRuntimePolicySurvivesEveryHop:
    """Binary name and routing policy both arrive intact."""

    def test_custom_binaries_survive(self, tmp_path: Path, workdir: Path):
        text = _root(
            '[agent.codex]\nenabled = true\ncodex_bin = "codex-next"\n\n'
            '[agent.claude]\nenabled = true\nclaude_bin = "claude-next"\n\n'
            '[agent.qoder]\nenabled = true\nqoder_bin = "qoder-next"\n',
            workdir,
        )
        _request, document = _render(tmp_path, text, workdir)
        assert document["codex"]["codex_bin"] == "codex-next"
        assert document["claude"]["claude_bin"] == "claude-next"
        assert document["qoder"]["qoder_bin"] == "qoder-next"

        loaded = _load_bridge_config(tmp_path / "bridge-home" / "bridge.json")
        assert loaded["codex_bin"] == "codex-next"
        assert loaded["claude_bin"] == "claude-next"
        assert loaded["qoder_bin"] == "qoder-next"

    def test_use_proxy_true_survives(self, tmp_path: Path, workdir: Path):
        text = _root(
            "[agent.codex]\nenabled = true\nuse_proxy = true\n\n"
            "[agent.claude]\nenabled = true\nuse_proxy = false\n\n"
            "[agent.qoder]\nenabled = true\nuse_proxy = false\n",
            workdir,
        )
        _request, document = _render(tmp_path, text, workdir)
        assert document["codex"]["use_proxy"] is True
        assert document["claude"]["use_proxy"] is False

        loaded = _load_bridge_config(tmp_path / "bridge-home" / "bridge.json")
        assert loaded["codex_use_proxy"] is True, "use_proxy=True was lost in the round trip"
        assert loaded["claude_use_proxy"] is False
        assert loaded["qoder_use_proxy"] is False

    def test_use_proxy_false_survives(self, tmp_path: Path, workdir: Path):
        text = _root("[agent.codex]\nenabled = true\nuse_proxy = false\n", workdir)
        _request, document = _render(tmp_path, text, workdir)
        assert document["codex"]["use_proxy"] is False
        loaded = _load_bridge_config(tmp_path / "bridge-home" / "bridge.json")
        assert loaded["codex_use_proxy"] is False

    def test_default_binary_survives_when_unset(self, tmp_path: Path, workdir: Path):
        text = _root("[agent.codex]\nenabled = true\n", workdir)
        _request, document = _render(tmp_path, text, workdir)
        assert document["codex"]["codex_bin"] == "codex"
        assert document["codex"]["use_proxy"] is True, "codex defaults to use_proxy=true (§7.1)"
        loaded = _load_bridge_config(tmp_path / "bridge-home" / "bridge.json")
        assert loaded["codex_bin"] == "codex"
        assert loaded["codex_use_proxy"] is True


class TestEndpointStillNeverPersisted:
    """The policy travels; the value it points at does not."""

    def test_no_endpoint_in_the_document(self, tmp_path: Path, workdir: Path):
        text = _root(
            "[agent.codex]\nenabled = true\nuse_proxy = true\n\n"
            '[agent.proxy]\nenabled = true\nsource = "env"\n',
            workdir,
        )
        _request, document = _render(tmp_path, text, workdir)
        blob = json.dumps(document)
        assert "127.0.0.1" not in blob
        assert "proxy_url" not in blob
        assert document["codex"]["use_proxy"] is True


def _load_via_bridge(text: str, tmp_path: Path):
    """Load an arbitrary bridge.json in the Bridge's own environment, returning stdout or stderr."""
    if not BRIDGE_PYTHON.exists():
        pytest.skip(f"bridge virtualenv interpreter is missing at {BRIDGE_PYTHON}")
    path = tmp_path / "legacy.json"
    path.write_text(text, encoding="utf-8")
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from serverfs_agent_bridge.config import BridgeConfig\n"
        "c = BridgeConfig.load(Path(sys.argv[1]))\n"
        "print(c.codex.use_proxy, c.claude.use_proxy, c.qoder.use_proxy)\n"
    )
    return subprocess.run(
        [str(BRIDGE_PYTHON), "-c", script, str(path)],
        capture_output=True,
        cwd=str(REPO_ROOT / "agent_bridge"),
    )


class TestLinuxBehaviourUnchanged:
    """A document without the new fields loads exactly as it did before."""

    def test_absent_use_proxy_defaults_false(self, tmp_path: Path):
        completed = _load_via_bridge(json.dumps({"codex": {"enabled": True}}), tmp_path)
        assert completed.returncode == 0, completed.stderr.decode(errors="replace")
        assert completed.stdout.decode().split() == ["False", "False", "False"]

    def test_absent_runtime_block_is_unaffected(self, tmp_path: Path):
        completed = _load_via_bridge(json.dumps({}), tmp_path)
        assert completed.returncode == 0, completed.stderr.decode(errors="replace")
        assert completed.stdout.decode().split() == ["False", "False", "False"]

    def test_non_boolean_use_proxy_is_refused(self, tmp_path: Path):
        """A string must not be coerced into a boolean: that would silently mean something else."""
        completed = _load_via_bridge(json.dumps({"codex": {"use_proxy": "yes"}}), tmp_path)
        assert completed.returncode != 0
        assert "use_proxy" in completed.stderr.decode(errors="replace")
