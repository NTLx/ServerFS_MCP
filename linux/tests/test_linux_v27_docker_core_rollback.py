"""Historical Docker Core replacement with unchanged Compose project and workspace.

Only a disposable hosted runner may build/use these exact test-owned image tags.
Never pull, retag or stop a production ServerFS image or Compose project.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from uuid import uuid4

import pytest

from test_linux_v0250_http_e2e import _http_status, _port, _tool

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
BASELINE = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("SERVERFS_LINUX_DOCKER_ROLLBACK") != "1",
    reason="explicit hosted Linux historical Docker Core rollback required",
)


def test_historical_docker_core_upgrade_and_rollback(tmp_path: Path) -> None:
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
    assert os.getuid() != 0
    assert not tmp_path.resolve().is_relative_to(REPO)
    docker = shutil.which("docker")
    assert docker
    suffix = uuid4().hex[:16]
    project = "sf27rollback-" + suffix
    daemon = [docker, "--host", "unix:///var/run/docker.sock"]
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    candidate = os.environ["GITHUB_SHA"]

    def run(args, *, cwd=REPO, extra=None, timeout=60, check=True):
        result = subprocess.run(
            args,
            cwd=cwd,
            env={**env, **(extra or {})},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check:
            # Do not disclose the synthetic bearer from a Compose diagnostic.
            assert result.returncode == 0, f"{args[0]} operation failed: {result.returncode}"
        return result.stdout.strip()

    assert run(["git", "rev-parse", "HEAD"]) == candidate
    assert not run(
        [*daemon, "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]
    )
    assert not run(
        [*daemon, "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"]
    )
    sources = []
    tags = []
    images = []
    port = _port()
    key = "synthetic-" + uuid4().hex
    workdir = tmp_path / "workspace"
    workdir.mkdir(mode=0o700)
    marker = workdir / "fixture.txt"
    marker.write_text("historical-persistent-content", encoding="utf-8")
    original = marker.read_bytes()
    identity = (workdir.stat().st_dev, workdir.stat().st_ino)
    empty_env = tmp_path / "empty.env"
    empty_env.touch(mode=0o600)
    compose_env = {
        "SERVERFS_UID": str(os.getuid()),
        "SERVERFS_GID": str(os.getgid()),
        "SERVERFS_HTTP_HOST": "0.0.0.0",
        "SERVERFS_HTTP_PORT": "8000",
        "SERVERFS_HTTP_PUBLISH_HOST": "127.0.0.1",
        "SERVERFS_HTTP_PUBLISH_PORT": str(port),
        "SERVERFS_HTTP_ALLOWED_HOSTS": f"127.0.0.1:{port},127.0.0.1:8000",
        "SERVERFS_HTTP_API_KEY": key,
        "WORKDIR_01_ALIAS": "fixture",
        "WORKDIR_01_PATH": str(workdir),
        "WORKDIR_01_READ_ONLY": "false",
    }
    for slot in range(2, 17):
        disabled = tmp_path / f"disabled-{slot}"
        disabled.mkdir()
        (disabled / ".serverfs-disabled").touch()
        compose_env[f"WORKDIR_{slot:02d}_PATH"] = str(disabled)
    configs = (
        "compose.yml",
        "compose.http.yml",
        "deployment/linux/compose.publish-http.yml",
        "deployment/linux/compose.bridge-disabled.yml",
    )

    def compose(index, *args, check=True):
        command = [*daemon, "compose", "--env-file", str(empty_env), "-p", project]
        for name in configs:
            command.extend(("-f", str(sources[index] / name)))
        return run(
            command + list(args),
            cwd=sources[index],
            extra={**compose_env, "SERVERFS_IMAGE": tags[index]},
            check=check,
        )

    def call(name, arguments=None):
        return asyncio.run(_tool(port, key, name, arguments))

    def read(name, expected):
        result = call("read_text_file", {"workdir": "fixture", "path": name})
        assert not result.is_error and expected in str(result.content)

    def write(name, content):
        result = call(
            "create_text_file",
            {
                "workdir": "fixture",
                "path": name,
                "content": content,
            },
        )
        assert not result.is_error
        assert (workdir / name).read_text(encoding="utf-8") == content

    generations = []
    last_started = None
    try:
        for label, tree in (("old", BASELINE), ("new", candidate + ":linux")):
            source = tmp_path / label
            source.mkdir()
            archive = tmp_path / (label + ".tar")
            run(["git", "archive", "--output", str(archive), tree])
            with tarfile.open(archive) as bundle:
                assert all(m.isfile() or m.isdir() for m in bundle.getmembers())
                bundle.extractall(source, filter="data")
            sources.append(source)
            tag = f"serverfs-rollback-test:{suffix}-{label}"
            assert not run([*daemon, "image", "ls", "-q", tag])
            tags.append(tag)
            run([*daemon, "build", "--tag", tag, str(source)], timeout=600)
            image = run([*daemon, "image", "inspect", "--format", "{{.Id}}", tag])
            assert image.startswith("sha256:") and len(image) == 71
            assert (
                run(
                    [
                        *daemon,
                        "image",
                        "inspect",
                        "--format",
                        '{{index .Config.Labels "io.modelcontextprotocol.server.name"}}',
                        image,
                    ]
                )
                == "io.github.NTLx/serverfs-mcp"
            )
            images.append(image)
            print(f"DOCKER_ROLLBACK_IMAGE {label} {image}", flush=True)
        for step, index in enumerate((0, 1, 0)):
            config = json.loads(compose(index, "config", "--format", "json"))
            core = config["services"]["serverfs-mcp"]
            assert core["environment"]["SERVERFS_AGENT_BRIDGE_ENABLED"] == "false"
            assert core["environment"]["SERVERFS_HTTP_API_KEY"] == key
            assert "PYTHONPATH" not in core["environment"]
            assert not any("/run/serverfs-agent" in v["target"] for v in core["volumes"])
            assert core["ports"][0]["host_ip"] == "127.0.0.1"
            last_started = index
            compose(
                index,
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                "--force-recreate",
                "serverfs-mcp",
            )
            container = compose(index, "ps", "-q", "serverfs-mcp")
            assert container and container not in generations
            generations.append(container)
            inspect = json.loads(run([*daemon, "inspect", container]))[0]
            assert inspect["Image"] == images[index]
            assert inspect["Config"]["Labels"]["com.docker.compose.project"] == project
            mounts = inspect["Mounts"]
            assert any(
                m["Destination"] == "/workdirs/01" and Path(m["Source"]) == workdir and m["RW"]
                for m in mounts
            )
            assert all(m["Destination"].startswith("/workdirs/") for m in mounts)
            assert not any(e.startswith("PYTHONPATH=") for e in inspect["Config"]["Env"])
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if _http_status(port) == 401:
                    break
                time.sleep(0.2)
            else:
                pytest.fail("installed historical/candidate Core did not reach authenticated HTTP")
            assert _http_status(port, "wrong-synthetic-key") == 401
            assert {"read_text_file", "create_text_file"} <= call("tools/list")
            read("fixture.txt", "historical-persistent-content")
            if step == 0:
                write("created-old.txt", "written-by-baseline")
            else:
                read("created-old.txt", "written-by-baseline")
                if step == 1:
                    write("created-new.txt", "written-by-candidate")
                else:
                    read("created-new.txt", "written-by-candidate")
            assert marker.read_bytes() == original
            assert (workdir.stat().st_dev, workdir.stat().st_ino) == identity
            compose(index, "stop", "serverfs-mcp")
            assert run([*daemon, "inspect", "--format", "{{.State.Running}}", container]) == "false"
        assert len(set(generations)) == 3
    finally:
        if last_started is not None:
            compose(last_started, "down", "--timeout", "5", "--remove-orphans")
        assert not run(
            [*daemon, "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]
        )
        assert not run(
            [
                *daemon,
                "network",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ]
        )
        for tag in reversed(tags):
            if run([*daemon, "image", "ls", "-q", tag]):
                run([*daemon, "image", "rm", tag])
            assert not run([*daemon, "image", "ls", "-q", tag])
    print("LINUX_HISTORICAL_DOCKER_CORE_ROLLBACK_PASS", flush=True)
