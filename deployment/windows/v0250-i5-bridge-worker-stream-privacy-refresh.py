"""One-use offline I5 Worker repair: content-free, bounded Claude stream events.

Only the disposable, Disabled i5-shared Agent Bridge Worker may be reinstalled.
The previous stream-capable Wheel is retained, immutable and checked against
the installed Worker before any write. No production, Core, Owner, Task
Scheduler lifecycle, Tunnel, network, Provider or credential modifications.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PREVIOUS_SLOT = "bridge-worker-partial-stream-refresh-1"
NEW_SLOT = "bridge-worker-stream-privacy-refresh-1"
ADAPTER = "adapters/claude.py"
PHASE = "entry"


class PrivacyRefreshError(RuntimeError):
    """Sanitized error without paths, provider content or credentials."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise PrivacyRefreshError(message)


def helpers():
    import importlib.util

    source = Path(__file__).with_name("v0250-i5-bridge-worker-partial-stream-refresh.py")
    spec = importlib.util.spec_from_file_location("i5_prev_stream_refresh", source)
    require(spec is not None and spec.loader is not None, "previous refresh helper missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def guarded_baseline(previous, *, postflight: bool):
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    race = previous._load_helpers()
    race._host()
    require(previous.REPO == REPO and race.REPO == REPO, "source root mismatch")
    require(previous.CHANGED == ADAPTER, "approved adapter module changed")
    gate = race.helper("v0250-i5-provider-shared-preflight.py", "i5_privacy_protected")
    named = race.helper("v0250-i6-named-connectors-stage.py", "i5_privacy_i6_protected")
    require(
        gate.BRIDGE == "i5-shared" and named.IDS == ("i6-edge-a", "i6-edge-b"),
        "protected fixture identities differ",
    )
    root, venv, python, package, sid, _original = race._baseline(gate, named, storage)
    require(sid == current_user_sid(), "user SID changed")
    retained = tuple((root / "build" / PREVIOUS_SLOT).glob("serverfs_agent_bridge-*.whl"))
    require(len(retained) == 1, "last Wheel not uniquely retained")
    storage.check_private(retained[0], directory=False, sid=sid)
    require(
        race._archive_matches(
            _only_wheel(root / "build" / (NEW_SLOT if postflight else PREVIOUS_SLOT)),
            package,
        ),
        "installed Worker does not match expected retained Wheel",
    )
    return race, gate, named, storage, root, venv, python, package, sid, retained[0]


def _only_wheel(directory: Path) -> Path:
    require(directory.is_dir() and not directory.is_symlink(), "Wheel directory missing")
    wheels = tuple(directory.glob("serverfs_agent_bridge-*.whl"))
    require(len(wheels) == 1, "one exact Wheel required")
    require(wheels[0].is_file() and not wheels[0].is_symlink(), "Wheel unsafe")
    return wheels[0]


def _source_delta_guard(race, package: Path, sid: str) -> None:
    from serverfs_mcp.windows_v25 import storage

    source = REPO / "agent_bridge" / "src" / "serverfs_agent_bridge"
    current = (source / ADAPTER).read_bytes()
    require(current != (package / ADAPTER).read_bytes(), "privacy change already installed")
    text = current.decode("utf-8")
    require(
        "include_partial_messages=True" in text
        and "text_emission_recorded" in text
        and '"agent.text_delta"' in text
        and '"size_bytes": len(text.encode("utf-8"))' in text
        and '{"text": text, "index": index}' not in text,
        "content-free one-event contract not found in adapter source",
    )
    for relative in race.CHECKS:
        storage.check_private(package / relative, directory=False, sid=sid)
        if relative != ADAPTER:
            require(
                (source / relative).read_bytes() == (package / relative).read_bytes(),
                "unrelated Worker module differs from installed",
            )


def main(argv: list[str] | None = None) -> None:
    global PHASE
    p = argparse.ArgumentParser()
    actions = p.add_mutually_exclusive_group(required=True)
    actions.add_argument("--audit", action="store_true")
    actions.add_argument("--stage", action="store_true")
    actions.add_argument("--postflight", action="store_true")
    args = p.parse_args(argv)

    previous = helpers()
    PHASE = "nonadmin-protected-baseline-and-retained-wheel"
    race, gate, named, storage, root, venv, python, package, sid, old_wheel = guarded_baseline(
        previous, postflight=args.postflight
    )
    print("I5_PRIVACY_PROTECTED_I5_I6_AND_PREVIOUS_WHEEL=PASS")
    candidate = root / "build" / NEW_SLOT
    if args.audit or args.stage:
        require(not candidate.exists() and not candidate.is_symlink(), "one-use slot occupied")
        _source_delta_guard(race, package, sid)
        print("I5_PRIVACY_CLAUDE_ONLY_CONTENT_FREE_SOURCE=PASS")
        if args.audit:
            print("I5_PRIVACY_REFRESH_AUDIT=PASS")
            return

    installer = race.helper("v0250-i2-stage.py", "i5_privacy_offline_wheel")
    require(installer.REPO == REPO, "Wheel builder root mismatch")
    if args.stage:
        PHASE = "one-use-private-Wheel-build"
        installer._new_dir(candidate, sid)
        wheel = installer._wheel(
            REPO / "agent_bridge",
            candidate,
            "serverfs_agent_bridge-*.whl",
            race.CHECKS,
        )
        installer._protect_tree(candidate, sid)
        previous._check_wheel_matches_source(wheel, race)
        print("I5_PRIVACY_NEW_WHEEL_SOURCE_PARITY=VERIFIED")

        PHASE = "offline-dry-run"
        command = [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--reinstall",
            "--python",
            str(python),
            str(wheel),
        ]
        env = dict(os.environ)
        env["UV_LINK_MODE"] = "copy"
        installer.run([*command, "--dry-run"], env=env, timeout=60)
        race._baseline(gate, named, storage)
        require(race._archive_matches(old_wheel, package), "Worker changed during dry-run")
        print("I5_PRIVACY_OFFLINE_DRY_RUN=PASS")

        PHASE = "only-private-I5-Worker-reinstall"
        installer.run(command, env=env, timeout=120)
        installer._protect_tree(venv, sid)
        print("I5_PRIVACY_PRIVATE_WORKER_INSTALL=PASS")

    PHASE = "installed-source-Wheel-and-private-postflight"
    race._assert_source_parity(package, sid)
    updated_wheel = _only_wheel(candidate)
    storage.check_private(updated_wheel, directory=False, sid=sid)
    previous._check_wheel_matches_source(updated_wheel, race)
    require(
        race._archive_matches(updated_wheel, package),
        "installed Worker differs from new Wheel",
    )
    storage.check_private(old_wheel, directory=False, sid=sid)
    previous._installed_stream_api(python, installer)
    race._baseline(gate, named, storage)
    print("I5_PRIVACY_INSTALLED_SOURCE_ACL_AND_SDK=PASS")
    print("I5_PRIVACY_REFRESH_POSTFLIGHT=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - only stable phase and exception class
        print(
            "I5_PRIVACY_REFRESH=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
