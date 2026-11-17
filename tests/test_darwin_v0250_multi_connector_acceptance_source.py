"""Source checks for the opt-in native multi-Connector acceptance script."""

import ast
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment/macos/v0250_multi_connector_native_acceptance.py"
)
SOURCE = SCRIPT.read_text()
TREE = ast.parse(SOURCE)


def _function(name: str) -> ast.FunctionDef:
    return next(
        node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_collision_status_check_runs_after_signed_connector_staging() -> None:
    main = _function("main")
    stage = next(
        node.lineno
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "stage_openai_http_connector"
    )
    status_checks = [
        node.lineno
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_connector_status"
    ]
    collision_phase = SOURCE.split('phase = "collision-check"', 1)[1].split(
        "temp = Path(tempfile.mkdtemp", 1
    )[0]

    assert "_manager(" not in collision_phase
    assert status_checks and min(status_checks) > stage


def test_tunnel_service_key_maps_to_openai_launchd_component() -> None:
    function_source = ast.get_source_segment(SOURCE, _function("_connector_status"))

    assert '"openai" if service_provider == "tunnel" else service_provider' in function_source
    assert "identity.macos_label(component, connector_id)" in function_source


def test_cleanup_uses_bridge_config_marker_and_retains_failed_runs() -> None:
    assert "_no_owned_children(identity, resource.config_path)" in SOURCE
    assert "tested\n            and not errors" in SOURCE


def test_missing_signed_connector_requires_specific_refusal_and_launchctl_absence() -> None:
    main_source = ast.get_source_segment(SOURCE, _function("main"))

    assert "except DarwinInstanceRuntimeError as exc:" in main_source
    assert 'str(exc) != "requested component is not staged"' in main_source
    assert '"Could not find service" not in missing_job.stderr' in main_source
    assert "runtime.darwin_service._launchd_job_pid(missing_label) is not None" in main_source


def test_each_started_connector_is_recorded_before_start_for_scoped_cleanup() -> None:
    main_source = ast.get_source_segment(SOURCE, _function("main"))

    assert main_source.index("started_keys.append((component, connector_id, key))") < (
        main_source.index("runtime.start_component(identity, component, connector_id=connector_id)")
    )
    assert "and roundtrips == 4" in main_source
