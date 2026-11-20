"""Synthetic Windows v25 Core owner -> independent Bridge client wiring.

No process, native Core listener, real Bridge IPC, Task Scheduler, endpoint or
credential access. The real CLI parser and main dispatch are exercised;
only the final serve handler is replaced by a recording fake.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from serverfs_mcp.windows_v25 import runtime
from test_windows_v25_core_bridge_binding import _fixtures


def _run(tmp_path: Path, monkeypatch, *, with_bridge: bool):
    from serverfs_mcp import cli, native_config
    from serverfs_mcp.windows_v25 import core_bridge_binding, isolation, readiness, storage

    core, peer, doc = _fixtures(tmp_path)
    state = []
    workdirs = [SimpleNamespace(alias="Repo")]
    wiring = object()

    monkeypatch.setattr(
        runtime,
        "_core_configuration",
        lambda _: (tmp_path / "policy" / "serverfs.toml", {"SERVERFS_HTTP_API_KEY": "synthetic"}),
    )
    monkeypatch.setattr(
        native_config,
        "load_native_config",
        lambda _: (workdirs, SimpleNamespace(bridge_required=with_bridge, log_level="INFO")),
    )

    class FakeBinding:
        def build_wiring(self, *, log_level):
            state.append(("build-wiring", log_level))
            return wiring

    def load_binding(_core, actual_workdirs):
        assert _core == core and actual_workdirs == workdirs
        state.append("load-binding")
        return FakeBinding()

    monkeypatch.setattr(core_bridge_binding, "load_native_core_bridge_binding", load_binding)
    monkeypatch.setattr(isolation, "assert_core_isolation", lambda *_args: None)
    monkeypatch.setattr(storage, "core_inventory", lambda: ())
    monkeypatch.setattr(storage, "root", lambda iid=None: tmp_path / "instances" / str(iid))
    monkeypatch.setattr(readiness, "publish", lambda _core: state.append("publish"))
    monkeypatch.setattr(runtime.os, "environ", {"PATH": "synthetic"})
    captured = []

    def fake_serve(args, *, agent_wiring_override=None):
        captured.append((args, agent_wiring_override))
        return 0

    monkeypatch.setattr(cli, "cmd_serve", fake_serve)
    return runtime.run_core(core), state, captured, wiring


def test_nondefault_agent_core_injects_v2_client_without_env_credentials(tmp_path, monkeypatch):
    result, state, cli_calls, wiring = _run(tmp_path, monkeypatch, with_bridge=True)
    assert result == 0
    assert state == ["load-binding", ("build-wiring", "INFO"), "publish"]
    assert len(cli_calls) == 1
    args, supplied_wiring = cli_calls[0]
    assert args.command == "serve"
    assert args.transport == "streamable-http"
    assert args.config == tmp_path / "policy" / "serverfs.toml"
    assert supplied_wiring is wiring
    assert "synthetic" not in repr(supplied_wiring)


def test_nondefault_file_only_core_reuses_unmodified_v1_cli_signature(tmp_path, monkeypatch):
    result, state, cli_calls, wiring = _run(tmp_path, monkeypatch, with_bridge=False)
    assert result == 0
    assert state == ["publish"]
    assert len(cli_calls) == 1
    args, supplied_wiring = cli_calls[0]
    assert args.command == "serve"
    assert args.transport == "streamable-http"
    assert args.config == tmp_path / "policy" / "serverfs.toml"
    assert supplied_wiring is None
