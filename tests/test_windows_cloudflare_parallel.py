"""Stage 1 source-boundary tests for the parallel Windows Cloudflare instance.

Nothing here starts a process, reads a credential, or touches Task Scheduler.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp import windows_cloudflare_parallel as parallel
from serverfs_mcp import windows_process, windows_service
from serverfs_mcp.windows_cloudflare_security import WindowsPrivateFileError
from serverfs_mcp.windows_process import ProcessIdentity


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    # Existing Windows Cloudflare tests neutralize reparse inspection so temp
    # directories stay deterministic; the rejection branch is tested separately.
    monkeypatch.setattr(parallel, "_no_reparse_components", lambda _path: None)
    root = tmp_path / "shared-repo"
    root.mkdir()
    config = tmp_path / "serverfs-parallel.toml"
    return {"tmp": tmp_path, "root": root, "config": config}


def _write_config(config: Path, root: Path, *, extra_workdir: str = "", agent: str = "") -> Path:
    payload = (
        "[server]\nlog_level = 'INFO'\n\n"
        f"{agent}"
        "[[workdirs]]\n"
        "alias = 'repo'\n"
        f"path = '{root.as_posix()}'\n"
        "read_only = true\n"
        f"{extra_workdir}"
    )
    config.write_text(payload, encoding="utf-8")
    return config


def test_identity_is_distinct_from_the_default_service(
    deployment: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(parallel, "_local_app_data", lambda: deployment["tmp"] / "LocalAppData")
    monkeypatch.setattr(
        windows_service, "service_home", lambda: deployment["tmp"] / "default-service-home"
    )

    assert parallel.TASK_NAME == "ServerFS Cloudflare WorkPC"
    assert parallel.TASK_NAME != windows_service.TASK_NAME
    assert parallel.HTTP_ORIGIN_PORT == 18765
    parallel.validate_distinct_ownership()

    document = parallel.describe_instance()
    assert document["owns_default_task"] is False
    assert document["task_name"] == parallel.TASK_NAME
    assert document["instance_root"].endswith(
        str(Path("ServerFS") / "instances" / "workpc-cloudflare")
    )


def test_default_service_namespace_is_never_shared(
    deployment: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = deployment["tmp"] / "local"
    monkeypatch.setattr(parallel, "_local_app_data", lambda: home)
    monkeypatch.setattr(
        windows_service,
        "service_home",
        lambda: home / "ServerFS" / "instances" / parallel.INSTANCE_ID,
    )

    with pytest.raises(parallel.WindowsCloudflareParallelError, match="collides"):
        parallel.validate_distinct_ownership()


def test_lock_name_is_namespaced_apart_from_the_openai_mutex() -> None:
    sid = "S-1-5-21-111-222-333-1001"
    name = parallel.instance_lock_name(sid)

    assert name.startswith("Local\\serverfs-cloudflare-parallel-v1-")
    assert "serverfs-native-tunnel-v1" not in name
    assert parallel.instance_lock_name(sid) == name
    assert parallel.instance_lock_name("S-1-5-21-111-222-333-1002") != name
    with pytest.raises(parallel.WindowsCloudflareParallelError, match="SID"):
        parallel.instance_lock_name("not-a-sid")


def test_accepts_read_only_config_using_the_same_root_as_openai(
    deployment: dict[str, Path],
) -> None:
    config = _write_config(deployment["config"], deployment["root"])

    result = parallel.validate_deployment_config(config)

    assert result.config_path == config.resolve()
    assert result.workdir_aliases == ("repo",)
    assert result.workdir_roots == (deployment["root"].resolve(),)
    assert result.http_origin_port == 18765


def test_accepts_writable_workdir(deployment: dict[str, Path]) -> None:
    config = deployment["config"]
    config.write_text(
        "[[workdirs]]\n"
        "alias = 'repo'\n"
        f"path = '{deployment['root'].as_posix()}'\n"
        "read_only = false\n"
        "agent_mode = 'disabled'\n",
        encoding="utf-8",
    )

    result = parallel.validate_deployment_config(config)
    assert result.workdir_aliases == ("repo",)
    assert result.workdir_roots == (deployment["root"].resolve(),)


def test_accepts_writable_agent_bridge_repo_ops_and_shared_roots(
    deployment: dict[str, Path],
) -> None:
    second = deployment["tmp"] / "second-repo"
    second.mkdir()
    config = deployment["config"]
    config.write_text(
        "[agent]\n"
        "enabled = true\n"
        "[agent.codex]\n"
        "enabled = true\n"
        "[[workdirs]]\n"
        "alias = 'repo'\n"
        f"path = '{deployment['root'].as_posix()}'\n"
        "read_only = false\n"
        "agent_mode = 'workspace-write'\n"
        "agent_runtimes = ['codex']\n"
        "repo_ops_enabled = true\n"
        "[[workdirs]]\n"
        "alias = 'other'\n"
        f"path = '{second.as_posix()}'\n"
        "read_only = false\n"
        "agent_mode = 'workspace-write'\n"
        "agent_runtimes = ['codex']\n"
        "repo_ops_enabled = true\n",
        encoding="utf-8",
    )

    result = parallel.validate_deployment_config(config)
    assert result.workdir_aliases == ("repo", "other")
    assert result.workdir_roots == (deployment["root"].resolve(), second.resolve())


def test_invalid_native_agent_policy_is_still_rejected(
    deployment: dict[str, Path],
) -> None:
    config = deployment["config"]
    config.write_text(
        "[[workdirs]]\n"
        "alias = 'repo'\n"
        f"path = '{deployment['root'].as_posix()}'\n"
        "read_only = false\n"
        "agent_mode = 'superuser'\n",
        encoding="utf-8",
    )
    with pytest.raises(parallel.WindowsCloudflareParallelError, match="config is invalid"):
        parallel.validate_deployment_config(config)


def test_rejects_reparse_point_workdir_root(
    deployment: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(deployment["config"], deployment["root"])
    root = deployment["root"]

    def reject_reparse(path: Path) -> None:
        if Path(path) == root:
            raise WindowsPrivateFileError("credential path contains a reparse point")

    monkeypatch.setattr(parallel, "_no_reparse_components", reject_reparse)

    with pytest.raises(parallel.WindowsCloudflareParallelError, match="root cannot be validated"):
        parallel.validate_deployment_config(config)


def test_rejects_reparse_point_config(
    deployment: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(deployment["config"], deployment["root"])

    def reject(_path: Path) -> None:
        raise WindowsPrivateFileError("credential path contains a reparse point")

    monkeypatch.setattr(parallel, "_no_reparse_components", reject)

    with pytest.raises(parallel.WindowsCloudflareParallelError, match="config path cannot"):
        parallel.validate_deployment_config(config)


def test_rejects_relative_or_missing_config(
    deployment: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(parallel.WindowsCloudflareParallelError, match="absolute path"):
        parallel.validate_deployment_config(Path("relative.toml"))

    with pytest.raises(parallel.WindowsCloudflareParallelError, match="config path cannot"):
        parallel.validate_deployment_config(deployment["tmp"] / "absent.toml")

    text_file = deployment["tmp"] / "serverfs.txt"
    text_file.write_text("not toml", encoding="utf-8")
    with pytest.raises(parallel.WindowsCloudflareParallelError, match=r"\.toml"):
        parallel.validate_deployment_config(text_file)


def test_rejects_invalid_policy_before_any_os_mutation(
    deployment: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Writable workdirs are a supported parallel mode; only a genuinely invalid
    # native policy must fail closed before any scheduled-task mutation.
    config = deployment["config"]
    config.write_text(
        "[[workdirs]]\n"
        "alias = 'repo'\n"
        f"path = '{deployment['root'].as_posix()}'\n"
        "read_only = false\n"
        "agent_mode = 'superuser'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(parallel, "_ensure_windows", lambda: None)
    monkeypatch.setattr(parallel, "_validate_instance_runtime_location", lambda: None)
    monkeypatch.setattr(
        parallel,
        "_run_schtasks",
        lambda *args, **kwargs: pytest.fail("mutated Task Scheduler on invalid config"),
    )

    with pytest.raises(parallel.WindowsCloudflareParallelError, match="config is invalid"):
        parallel.install_instance(
            config,
            deployment["tmp"] / "cloudflared.exe",
            deployment["tmp"] / "token",
            deployment["tmp"] / "origin.env",
        )


# -- Stage 2 lifecycle (Task Scheduler fully mocked) -----------------------

SID = "S-1-5-21-111-222-333-1001"
SECRET = "parallel-secret-sentinel"
_REAL_VALIDATE_RUNTIME_LOCATION = parallel._validate_instance_runtime_location


def _actions(wired) -> list[str]:
    """SchTasks verbs in order; a missing task legitimately retries /Query."""
    return [call[0] for call in wired.scheduler.calls]


class _Sequence:
    """Callable returning successive results; the last one repeats."""

    def __init__(self, *results) -> None:
        self._results = list(results)
        self.calls = 0

    def __call__(self):
        index = min(self.calls, len(self._results) - 1)
        self.calls += 1
        return self._results[index]()


class FakeScheduler:
    """Records schtasks calls and emulates one task name only."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.registered_xml: str | None = None
        self.fail_create = False

    def __call__(self, *args: str, check: bool = True):
        self.calls.append(tuple(args))
        if args[0] == "/Query":
            if self.registered_xml is None:
                return subprocess.CompletedProcess(args, -2147024894, stdout=b"", stderr=b"")
            return subprocess.CompletedProcess(
                args, 0, stdout=self.registered_xml.encode("utf-16"), stderr=b""
            )
        if args[0] == "/Create":
            if self.fail_create:
                return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"")
            self.registered_xml = Path(args[4]).read_text(encoding="utf-16")
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if args[0] == "/Delete":
            self.registered_xml = None
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    def task_names(self) -> set[str]:
        return {call[2] for call in self.calls if len(call) > 2 and call[1] == "/TN"}


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """One fully mocked parallel instance rooted in a temporary LocalAppData."""
    monkeypatch.setattr(parallel, "_no_reparse_components", lambda _path: None)
    local = tmp_path / "LocalAppData"
    root = tmp_path / "shared-repo"
    root.mkdir()
    config = tmp_path / "serverfs-parallel.toml"
    config.write_text(
        "[server]\nlog_level = 'INFO'\n\n"
        "[[workdirs]]\n"
        "alias = 'repo'\n"
        f"path = '{root.as_posix()}'\n"
        "read_only = true\n",
        encoding="utf-8",
    )
    connector = local / "ServerFS" / "instances" / parallel.INSTANCE_ID / "connector-env"
    (connector / "Scripts").mkdir(parents=True)
    runner = connector / "Scripts" / "pythonw.exe"
    runner.write_bytes(b"")
    cloudflared = tmp_path / "cloudflared.exe"
    token = tmp_path / "connector.token"
    env_file = tmp_path / "origin.env"
    for path in (cloudflared, token, env_file):
        path.write_text("fixture", encoding="utf-8")
    spec = SimpleNamespace(
        config_path=config.resolve(),
        cloudflared=cloudflared.resolve(),
        token_file=token.resolve(),
        env_file=env_file.resolve(),
        port=parallel.HTTP_ORIGIN_PORT,
        hostname=parallel.DEFAULT_HOSTNAME,
        origin_environment={
            "SERVERFS_HTTP_API_KEY": SECRET,
            "SERVERFS_HTTP_ALLOWED_HOSTS": parallel.DEFAULT_HOSTNAME,
        },
    )
    scheduler = FakeScheduler()
    # One query attempt keeps call sequences deterministic; retry semantics get
    # their own test below.
    monkeypatch.setattr(parallel, "TASK_QUERY_ATTEMPTS", 1)
    monkeypatch.setattr(parallel, "TASK_QUERY_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(parallel, "_local_app_data", lambda: local)
    monkeypatch.setattr(parallel, "_ensure_windows", lambda: None)
    monkeypatch.setattr(parallel, "_current_user_sid", lambda: SID)
    monkeypatch.setattr(parallel, "_validate_instance_runtime_location", lambda: None)
    monkeypatch.setattr(parallel, "_validate_native_runtime", lambda: None)
    monkeypatch.setattr(parallel, "_runner_executable", lambda: runner.resolve())
    monkeypatch.setattr(parallel, "validate_install", lambda *a, **k: spec)
    monkeypatch.setattr(parallel, "_run_schtasks", scheduler)
    monkeypatch.setattr(
        windows_service,
        "service_home",
        lambda: tmp_path / "default-service-home",
    )
    monkeypatch.setattr(
        windows_service,
        "_run_schtasks",
        lambda *a, **k: pytest.fail("parallel lifecycle invoked the default service"),
    )
    monkeypatch.setattr(
        windows_service,
        "_query_task_xml",
        lambda **k: pytest.fail("parallel lifecycle queried the default task"),
    )
    return SimpleNamespace(
        tmp=tmp_path,
        local=local,
        config=config,
        root=root,
        runner=runner,
        cloudflared=cloudflared,
        token=token,
        env_file=env_file,
        spec=spec,
        scheduler=scheduler,
    )


def test_install_registers_own_disabled_task_without_starting(wired) -> None:
    snapshot = parallel.install_instance(
        wired.config, wired.cloudflared, wired.token, wired.env_file
    )

    assert snapshot["task_valid"] is True
    assert snapshot["enabled"] is False
    assert snapshot["provider"] == "cloudflare"
    assert snapshot["parallel_mode"] is True
    assert snapshot["agent_bridge_owner"] == "shared-existing"
    assert snapshot["repo_ops_policy"] == "native-config"
    assert smart_names(wired) == {parallel.TASK_NAME}
    assert _actions(wired) == ["/Query", "/Create", "/Query"]
    assert "/Run" not in {call[0] for call in wired.scheduler.calls}
    manifest = json.loads(parallel.instance_manifest_path().read_text(encoding="utf-8"))
    assert manifest["task_name"] == parallel.TASK_NAME
    assert manifest["hostname"] == parallel.DEFAULT_HOSTNAME
    assert manifest["http_port"] == parallel.HTTP_ORIGIN_PORT
    assert manifest["token_file"] == str(wired.token.resolve())
    assert manifest["working_directory"] == str(parallel.instance_root().resolve())
    raw = json.dumps(manifest)
    assert SECRET not in raw
    # Path only, never the token value.
    assert manifest["token_file"] == str(wired.token.resolve())
    task_text = parallel.instance_task_xml_path().read_text(encoding="utf-16")
    assert SECRET not in task_text
    assert "HighestAvailable" not in task_text
    assert parallel.TASK_URI in task_text


def smart_names(wired) -> set[str]:
    return wired.scheduler.task_names()


def test_install_never_reads_or_writes_default_service_files(
    wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        windows_service,
        "manifest_path",
        lambda: pytest.fail("touched the default service manifest path"),
    )

    parallel.install_instance(wired.config, wired.cloudflared, wired.token, wired.env_file)

    assert not (wired.tmp / "default-service-home").exists()


def test_install_refuses_a_foreign_python_environment(
    wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp import windows_cloudflare_parallel as module

    monkeypatch.setattr(
        parallel, "_validate_instance_runtime_location", _REAL_VALIDATE_RUNTIME_LOCATION
    )
    monkeypatch.setattr(module.sys, "prefix", str(wired.tmp / "some-other-venv"))
    monkeypatch.setattr(
        parallel,
        "_run_schtasks",
        lambda *a, **k: pytest.fail("registered from a foreign interpreter"),
    )

    with pytest.raises(parallel.WindowsCloudflareParallelError, match="own connector-env"):
        parallel.install_instance(wired.config, wired.cloudflared, wired.token, wired.env_file)


def test_reinstall_refuses_a_drifted_existing_task(wired) -> None:
    parallel.install_instance(wired.config, wired.cloudflared, wired.token, wired.env_file)
    wired.scheduler.registered_xml = wired.scheduler.registered_xml.replace(
        "LeastPrivilege", "HighestAvailable"
    )
    wired.scheduler.calls.clear()

    with pytest.raises(parallel.WindowsCloudflareParallelError, match="contract is invalid"):
        parallel.install_instance(wired.config, wired.cloudflared, wired.token, wired.env_file)

    assert [call[0] for call in wired.scheduler.calls] == ["/Query", "/Query"]


def test_reinstall_refuses_while_its_own_runner_is_alive(wired, monkeypatch) -> None:
    parallel.install_instance(wired.config, wired.cloudflared, wired.token, wired.env_file)
    real_status = parallel.status_instance
    monkeypatch.setattr(
        parallel,
        "status_instance",
        lambda: {**real_status(), "runner_alive": True},
    )
    wired.scheduler.calls.clear()

    with pytest.raises(parallel.WindowsCloudflareParallelError, match="stop the existing"):
        parallel.install_instance(wired.config, wired.cloudflared, wired.token, wired.env_file)

    assert "/Create" not in {call[0] for call in wired.scheduler.calls}


def test_install_rolls_back_fresh_registration_and_files(wired, monkeypatch) -> None:
    monkeypatch.setattr(
        parallel, "status_instance", lambda: {"task_valid": False, "enabled": False}
    )

    with pytest.raises(parallel.WindowsCloudflareParallelError, match="contract verification"):
        parallel.install_instance(wired.config, wired.cloudflared, wired.token, wired.env_file)

    assert [(call[0], call[2]) for call in wired.scheduler.calls][-1] == (
        "/Delete",
        parallel.TASK_NAME,
    )
    assert not parallel.instance_manifest_path().exists()
    assert not parallel.instance_task_xml_path().exists()


def test_install_restores_previous_registration_and_files_on_failed_update(
    wired, monkeypatch
) -> None:
    parallel.install_instance(wired.config, wired.cloudflared, wired.token, wired.env_file)
    previous_manifest = parallel.instance_manifest_path().read_bytes()
    previous_task_file = parallel.instance_task_xml_path().read_bytes()
    previous_registered = wired.scheduler.registered_xml
    wired.scheduler.calls.clear()
    real_status = parallel.status_instance
    # The pre-install guard must see the healthy existing instance; the
    # post-registration verification is the one that fails.
    monkeypatch.setattr(
        parallel,
        "status_instance",
        _Sequence(
            lambda: {**real_status(), "task_valid": True},
            lambda: {"task_valid": False, "enabled": False},
        ),
    )

    with pytest.raises(parallel.WindowsCloudflareParallelError, match="contract verification"):
        parallel.install_instance(wired.config, wired.cloudflared, wired.token, wired.env_file)

    creates = [call for call in wired.scheduler.calls if call[0] == "/Create"]
    assert len(creates) == 2
    assert ".rollback-task.xml" in creates[-1][4]
    assert parallel.instance_manifest_path().read_bytes() == previous_manifest
    assert parallel.instance_task_xml_path().read_bytes() == previous_task_file
    assert wired.scheduler.registered_xml == previous_registered
    assert not (parallel.instance_root() / ".rollback-task.xml").exists()


def test_start_enables_then_runs_only_its_own_task(wired) -> None:
    parallel.install_instance(wired.config, wired.cloudflared, wired.token, wired.env_file)
    wired.scheduler.calls.clear()

    parallel.start_instance()

    writes = [call for call in wired.scheduler.calls if call[0] != "/Query"]
    assert writes[0] == ("/Change", "/TN", parallel.TASK_NAME, "/ENABLE")
    assert writes[1] == ("/Run", "/TN", parallel.TASK_NAME)
    assert smart_names(wired) == {parallel.TASK_NAME}


def test_start_refuses_a_tampered_task_before_enabling(wired) -> None:
    parallel.install_instance(wired.config, wired.cloudflared, wired.token, wired.env_file)
    wired.scheduler.registered_xml = wired.scheduler.registered_xml.replace(
        "<Arguments>", "<Arguments>--injected "
    )
    wired.scheduler.calls.clear()

    with pytest.raises(parallel.WindowsCloudflareParallelError, match="task arguments"):
        parallel.start_instance()

    assert {call[0] for call in wired.scheduler.calls} == {"/Query"}


def test_stop_disables_first_then_ends_and_never_targets_the_default_task(
    wired, monkeypatch
) -> None:
    parallel.install_instance(wired.config, wired.cloudflared, wired.token, wired.env_file)
    wired.scheduler.calls.clear()
    monkeypatch.setattr(parallel, "_read_state", lambda: {})
    monkeypatch.setattr(parallel, "_read_watchdog_state", lambda: {})
    monkeypatch.setattr(parallel, "_wait_stopped", lambda *_a: None)

    parallel.stop_instance()

    writes = [call for call in wired.scheduler.calls if call[0] != "/Query"]
    assert writes[0] == ("/Change", "/TN", parallel.TASK_NAME, "/DISABLE")
    assert writes[1] == ("/End", "/TN", parallel.TASK_NAME)
    assert smart_names(wired) == {parallel.TASK_NAME}


def test_stop_marks_operator_stop_and_recovers_own_orphan(wired, monkeypatch) -> None:
    frozen = ProcessIdentity(321, wired.runner.resolve(), 987654)
    other = ProcessIdentity(999, wired.runner.resolve(), 111111)
    writes: list[dict[str, object]] = []
    kills: list[ProcessIdentity] = []
    monkeypatch.setattr(parallel, "_query_task_xml", lambda **_k: None)
    monkeypatch.setattr(
        parallel,
        "_read_state",
        lambda: {
            "runner_pid": frozen.pid,
            "runner_image_path": str(frozen.image_path),
            "runner_creation_time": frozen.creation_time,
        },
    )
    monkeypatch.setattr(
        parallel,
        "_read_watchdog_state",
        lambda: {
            "watchdog_pid": other.pid,
            "watchdog_image_path": str(other.image_path),
            "watchdog_creation_time": other.creation_time,
        },
    )
    monkeypatch.setattr(parallel, "_write_state", lambda document: writes.append(document))
    monkeypatch.setattr(windows_process, "same_process", lambda identity: identity == frozen)
    monkeypatch.setattr(parallel, "_kill_process_tree", lambda identity: kills.append(identity))
    monkeypatch.setattr(parallel, "_wait_stopped", lambda *_a: None)
    monkeypatch.setattr(
        parallel, "status_instance", lambda: {"runner_alive": False, "enabled": False}
    )

    assert parallel.stop_instance()["runner_alive"] is False

    # A stale/reused watchdog PID is never chosen while the runner identity matches.
    assert kills == [frozen]
    assert writes[0]["stop_requested_at"] is not None


def test_stop_leaves_a_reused_pid_alone(wired, monkeypatch) -> None:
    monkeypatch.setattr(parallel, "_query_task_xml", lambda **_k: None)
    monkeypatch.setattr(parallel, "_read_state", lambda: {"runner_pid": 321})
    monkeypatch.setattr(parallel, "_read_watchdog_state", lambda: {})
    monkeypatch.setattr(windows_process, "same_process", lambda _identity: False)
    monkeypatch.setattr(
        parallel, "_kill_process_tree", lambda _identity: pytest.fail("killed a reused PID")
    )
    monkeypatch.setattr(parallel, "_wait_stopped", lambda *_a: None)
    monkeypatch.setattr(
        parallel, "status_instance", lambda: {"runner_alive": False, "enabled": False}
    )

    parallel.stop_instance()


def test_uninstall_removes_only_its_own_instance_root(wired) -> None:
    parallel.install_instance(wired.config, wired.cloudflared, wired.token, wired.env_file)
    default_home = wired.tmp / "default-service-home"
    default_home.mkdir()
    (default_home / "service.json").write_text("{}", encoding="utf-8")

    parallel.uninstall_instance()

    assert not parallel.instance_root().exists()
    assert (default_home / "service.json").read_text(encoding="utf-8") == "{}"


def test_status_is_truthful_when_query_fails_and_never_touches_default(wired, monkeypatch) -> None:
    monkeypatch.setattr(
        parallel,
        "_query_task_xml",
        lambda **_k: (_ for _ in ()).throw(
            parallel.WindowsCloudflareParallelError(
                "parallel Cloudflare Task Scheduler query failed (hresult=0x80070005)"
            )
        ),
    )
    monkeypatch.setattr(parallel, "_read_state", lambda: {})
    monkeypatch.setattr(parallel, "_read_watchdog_state", lambda: {})

    snapshot = parallel.status_instance()

    assert snapshot["registered"] is None
    assert snapshot["enabled"] is None
    assert snapshot["task_valid"] is False
    assert snapshot["manifest_installed"] is False
    assert snapshot["runner_alive"] is False
    assert snapshot["http_origin_alive"] is False
    assert snapshot["task_query_error"].endswith("0x80070005)")


def test_status_reports_frozen_child_liveness_only(wired, monkeypatch) -> None:
    origin = ProcessIdentity(41, wired.runner.resolve(), 100)
    monkeypatch.setattr(parallel, "_query_task_xml", lambda **_k: None)
    monkeypatch.setattr(parallel, "_read_watchdog_state", lambda: {})
    monkeypatch.setattr(
        parallel,
        "_read_state",
        lambda: {
            "origin_pid": origin.pid,
            "origin_image_path": str(origin.image_path),
            "origin_creation_time": origin.creation_time,
            "connector_pid": 42,
            "connector_image_path": str(origin.image_path),
            "connector_creation_time": 101,
        },
    )
    monkeypatch.setattr(windows_process, "same_process", lambda identity: identity == origin)

    snapshot = parallel.status_instance()

    assert snapshot["http_origin_alive"] is True
    assert snapshot["cloudflare_connector_alive"] is False


def test_manifest_rejects_default_or_foreign_identity(wired) -> None:
    parallel.install_instance(wired.config, wired.cloudflared, wired.token, wired.env_file)
    document = json.loads(parallel.instance_manifest_path().read_text(encoding="utf-8"))

    for key, value in (
        ("task_name", windows_service.TASK_NAME),
        ("instance_id", "default"),
        ("provider", "openai"),
        ("parallel_mode", False),
        ("http_port", 8080),
        ("hostname", "other.example.invalid"),
    ):
        tampered = {**document, key: value}
        parallel.instance_manifest_path().write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(parallel.WindowsCloudflareParallelError, match="manifest is invalid"):
            parallel._manifest_document()


def test_managed_startup_rejects_manifest_path_substitution(wired) -> None:
    manifest = {
        "config_path": str(wired.tmp / "other.toml"),
        "cloudflared": str(wired.cloudflared),
        "token_file": str(wired.token),
        "env_file": str(wired.env_file),
    }

    with pytest.raises(parallel.WindowsCloudflareParallelError, match="path mismatch"):
        parallel._spec_from_manifest(
            manifest, wired.config, wired.cloudflared, wired.token, wired.env_file
        )


def test_scheduler_argv_carries_paths_and_private_subcommands_only(wired) -> None:
    watchdog = parallel._watchdog_argv(wired.config, wired.cloudflared, wired.token, wired.env_file)
    runner = parallel._runner_argv(wired.config, wired.cloudflared, wired.token, wired.env_file)

    assert "cloudflare-parallel" in watchdog and "_watchdog" in watchdog
    assert "cloudflare-parallel" in runner and "_run" in runner
    assert str(wired.token.resolve()) in runner
    assert SECRET not in " ".join(watchdog + runner)


def test_task_xml_is_least_privilege_without_secret_material(wired) -> None:
    payload = parallel.build_task_xml(
        user_sid=SID,
        task_executable=wired.runner,
        task_arguments=parallel._watchdog_argv(
            wired.config, wired.cloudflared, wired.token, wired.env_file
        ),
        working_directory=parallel.instance_root(),
        enabled=False,
        start_boundary="2026-11-10T09:00:00+08:00",
    )
    text = payload.decode("utf-16")

    assert f"<UserId>{SID}</UserId>" in text
    assert "<LogonType>InteractiveToken</LogonType>" in text
    assert "<RunLevel>LeastPrivilege</RunLevel>" in text
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in text
    assert "<Enabled>false</Enabled>" in text
    assert parallel.TASK_URI in text
    assert SECRET not in text
    for forbidden in ("HighestAvailable", "LocalSystem", "S4U", "SYSTEM", "Password"):
        assert forbidden not in text


def test_restart_is_stop_then_start(wired, monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(parallel, "stop_instance", lambda: order.append("stop") or {})
    monkeypatch.setattr(parallel, "start_instance", lambda: order.append("start") or {})

    parallel.restart_instance()

    assert order == ["stop", "start"]


def test_backoff_is_bounded_and_resets() -> None:
    delay = parallel.INITIAL_RESTART_DELAY_SECONDS
    for _ in range(5):
        delay = parallel._next_restart_delay(1.0, delay)
    assert delay == parallel.MAX_RESTART_DELAY_SECONDS
    assert (
        parallel._next_restart_delay(parallel.HEALTHY_RUN_RESET_SECONDS, delay)
        == parallel.INITIAL_RESTART_DELAY_SECONDS
    )
    watchdog_delay = parallel.WATCHDOG_INITIAL_RESTART_DELAY_SECONDS
    for _ in range(5):
        watchdog_delay = parallel._next_watchdog_restart_delay(1.0, watchdog_delay)
    assert watchdog_delay == parallel.WATCHDOG_MAX_RESTART_DELAY_SECONDS


def test_runner_without_a_manifest_fails_closed(wired, monkeypatch) -> None:
    monkeypatch.setattr(parallel, "_ensure_windows", lambda: None)

    with pytest.raises(parallel.WindowsCloudflareParallelError, match="not installed"):
        parallel.run_managed(wired.config, wired.cloudflared, wired.token, wired.env_file)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows CLI route")
def test_cli_routes_parallel_status_without_default_service(monkeypatch, capsys) -> None:
    from serverfs_mcp import cli

    monkeypatch.setattr(
        parallel,
        "status_instance",
        lambda: {"task_name": parallel.TASK_NAME, "parallel_mode": True},
    )
    monkeypatch.setattr(
        windows_service,
        "cloudflare_status",
        lambda: pytest.fail("routed to the default Cloudflare provider"),
    )
    args = argparse.Namespace(service_command="cloudflare-parallel", parallel_command="status")

    assert cli.cmd_service(args) == 0
    result = json.loads(capsys.readouterr().err)
    assert result["task_name"] == parallel.TASK_NAME


def test_cli_reports_parallel_errors_without_secret(monkeypatch, capsys) -> None:
    from serverfs_mcp import cli

    def reject():
        raise parallel.WindowsCloudflareParallelError("parallel Cloudflare task is not registered")

    monkeypatch.setattr(parallel, "status_instance", reject)
    args = argparse.Namespace(service_command="cloudflare-parallel", parallel_command="status")

    assert cli.cmd_service(args) == 2
    assert SECRET not in capsys.readouterr().err
