"""Opt-in Cloudflare acceptance from an exact independent Linux Git tree.

The historical pinned v0.25 fixture remains unchanged in scope. The v0.27
candidate uses a fresh image built from the explicitly selected Git commit,
not the old test image or a mutable production image tag.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from uuid import uuid4

import pytest

from test_linux_v0250_cloudflare_public import (
    CLOUDFLARE_FILES,
    CLOUDFLARED_IMAGE,
    DOCKER,
    HOSTNAME,
    ROOT,
    _check_explicit_identity,
    _exercise_local_candidate,
    _exercise_public_candidate,
    _runner_env,
)

REPO = ROOT.parent
SHA_ENV = "SERVERFS_LINUX_V27_CLOUDFLARE_SHA"
LOCAL_ENV = "SERVERFS_LINUX_V27_CLOUDFLARE_LOCAL_ACCEPT"
PUBLIC_ENV = "SERVERFS_LINUX_V27_CLOUDFLARE_PUBLIC_ACCEPT"


def _candidate_sha(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("a full lowercase candidate commit SHA is required")
    return value


def test_candidate_identity_rejects_old_image_and_other_hostname():
    image = "serverfs-cf-acceptance:synthetic-candidate"
    _check_explicit_identity(HOSTNAME, HOSTNAME, image, expected_image=image)
    with pytest.raises(ValueError, match="candidate"):
        _check_explicit_identity(HOSTNAME, HOSTNAME, "serverfs-mcp:latest", expected_image=image)
    with pytest.raises(ValueError, match="hostname"):
        _check_explicit_identity("other.example", HOSTNAME, image, expected_image=image)
    for value in ("", "HEAD", "abc123", "f" * 39, "g" * 40):
        with pytest.raises(ValueError, match="full lowercase"):
            _candidate_sha(value)
    assert _candidate_sha("a" * 40) == "a" * 40


@pytest.fixture
def exact_candidate(tmp_path: Path):
    assert sys.platform == "linux" and DOCKER and os.getuid() != 0
    assert not tmp_path.resolve().is_relative_to(REPO.resolve())
    sha = _candidate_sha(os.environ.get(SHA_ENV, ""))
    git = shutil.which("git")
    assert git
    env = _runner_env(tmp_path)
    daemon = [DOCKER, "--host", "unix:///var/run/docker.sock"]

    def run(args, *, cwd=REPO, timeout=60):
        result = subprocess.run(
            args, cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False
        )
        assert result.returncode == 0, f"{args[0]} operation failed: {result.returncode}"
        return result.stdout

    assert run([git, "cat-file", "-t", sha]).strip() == b"commit"
    # The test helpers render Compose from ROOT, not the archived source.
    # Require exact candidate Linux tree identity and exact config bytes.
    assert run([git, "rev-parse", "HEAD:linux"]) == run([git, "rev-parse", sha + ":linux"])
    for name in CLOUDFLARE_FILES:
        local = ROOT / name
        assert local.is_file() and not local.is_symlink()
        assert local.read_bytes() == run([git, "show", sha + ":linux/" + name])

    archive = tmp_path / "candidate.tar"
    source = tmp_path / "source"
    source.mkdir()
    run([git, "archive", "--output", str(archive), sha + ":linux"])
    with tarfile.open(archive) as bundle:
        assert all(member.isfile() or member.isdir() for member in bundle.getmembers())
        bundle.extractall(source, filter="data")
    nonce = uuid4().hex
    tag = "serverfs-cf-acceptance:" + nonce
    assert not run([*daemon, "image", "ls", "-q", tag]).strip()
    try:
        run(
            [
                *daemon,
                "build",
                "--tag",
                tag,
                "--label",
                "io.serverfs.acceptance.commit=" + sha,
                "--label",
                "io.serverfs.acceptance.run=" + nonce,
                str(source),
            ],
            cwd=source,
            timeout=600,
        )
        image_id = run([*daemon, "image", "inspect", "--format", "{{.Id}}", tag]).decode().strip()
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
        assert (
            run(
                [
                    *daemon,
                    "image",
                    "inspect",
                    "--format",
                    '{{index .Config.Labels "io.serverfs.acceptance.commit"}}',
                    image_id,
                ]
            )
            .decode()
            .strip()
            == sha
        )
        fixture = tmp_path / "fixture"
        fixture.mkdir(mode=0o700)
        print(f"V27_CLOUDFLARE_IMAGE sha={sha} image={image_id}", flush=True)
        yield fixture, tag, image_id
    finally:
        # Only the freshly generated test tag is removed. No prune or
        # production image/project operations are allowed.
        if run([*daemon, "image", "ls", "-q", tag]).strip():
            run([*daemon, "image", "rm", tag])
        assert not run([*daemon, "image", "ls", "-q", tag]).strip()


@pytest.mark.skipif(os.environ.get(LOCAL_ENV) != "1", reason="explicit v0.27 local opt-in")
def test_v27_cloudflare_local_candidate(exact_candidate):
    fixture, tag, image_id = exact_candidate
    # Local-only preparation renders the connector but must never start it.
    # Its pinned image is required by the separate public acceptance test.
    _exercise_local_candidate(fixture, tag, image_id, require_connector_image=False)
    print("V27_CLOUDFLARE_LOCAL_PASS", flush=True)


@pytest.mark.skipif(
    os.environ.get(PUBLIC_ENV) != "1", reason="explicit test Tunnel handoff required"
)
def test_v27_cloudflare_public_candidate(exact_candidate):
    fixture, tag, image_id = exact_candidate
    assert os.environ.get("CLOUDFLARE_TEST_HOSTNAME") == HOSTNAME
    assert os.environ.get("APPROVED_CLOUDFLARE_TEST_HOSTNAME") == HOSTNAME
    assert os.environ.get("CLOUDFLARED_IMAGE") == CLOUDFLARED_IMAGE
    assert os.environ.get("CLOUDFLARE_TUNNEL_TOKEN_FILE")
    _exercise_public_candidate(fixture, tag, tag, image_id)
    print("V27_CLOUDFLARE_PUBLIC_PASS", flush=True)
