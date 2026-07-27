import os
from pathlib import Path

import pytest

from fraeno import __version__
from fraeno.config import FraenoConfig, ValidationConfig
from fraeno.validation.contract import (
    CapturedWorkspace,
    RunnerContractError,
    assemble_validation,
)
from fraeno.validation.runner import StepResult, WorkspaceRun
from fraeno.validation.sandbox import SandboxError, require_protected_output

ROOT = Path(__file__).parents[1]


def workspace_run(phase: str) -> WorkspaceRun:
    return WorkspaceRun(
        phase=phase,
        workspace="/workspace",
        steps=(
            StepResult(
                name="build",
                command=("true",),
                exit_code=0,
                duration_seconds=0.1,
                stdout="",
                stderr="",
            ),
        ),
        observation=None,
        error=None,
    )


def test_workspace_evidence_round_trips_with_engine_version() -> None:
    captured = CapturedWorkspace(
        engine_version=__version__,
        run=workspace_run("baseline"),
    )

    restored = CapturedWorkspace.from_dict(captured.to_dict())

    assert restored == captured
    assert restored.to_dict()["engine"]["version"] == __version__


def test_assemble_rejects_evidence_from_another_engine() -> None:
    config = FraenoConfig(
        version=1,
        project_name="external",
        validation=ValidationConfig(
            steps=(),
            observation_command=("true",),
        ),
    )
    baseline = CapturedWorkspace("old-version", workspace_run("baseline"))
    candidate = CapturedWorkspace("old-version", workspace_run("candidate"))

    with pytest.raises(RunnerContractError, match="not produced by this"):
        assemble_validation(baseline, candidate, config)


def test_assemble_rejects_swapped_sandbox_evidence() -> None:
    config = FraenoConfig(
        version=1,
        project_name="external",
        validation=ValidationConfig(
            steps=(),
            observation_command=("true",),
        ),
    )
    baseline = CapturedWorkspace(__version__, workspace_run("candidate"))
    candidate = CapturedWorkspace(__version__, workspace_run("baseline"))

    with pytest.raises(RunnerContractError, match="wrong phase"):
        assemble_validation(baseline, candidate, config)


def test_protected_output_refuses_a_directory_writable_by_candidate(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o777)

    with pytest.raises(SandboxError, match="can write"):
        require_protected_output(
            tmp_path / "run.json",
            command_uid=os.getuid() + 1,
            command_gid=os.getgid() + 1,
        )


def test_protected_output_accepts_root_only_directory(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)

    require_protected_output(
        tmp_path / "run.json",
        command_uid=os.getuid() + 1,
        command_gid=os.getgid() + 1,
    )


def test_customer_template_has_no_internal_fixture_dependency() -> None:
    workflow = (ROOT / "templates/github/fraeno-validation.yml").read_text()

    assert "FRAENO_RUNNER_IMAGE" in workflow
    assert "@sha256:" in workflow
    assert "inputs.base_repository" in workflow
    assert "inputs.head_repository" in workflow
    assert "fixtures/ros2_qos_robot" not in workflow
    assert "src/fraeno" not in workflow


def test_customer_orchestrator_matches_the_reviewed_runner_script() -> None:
    assert (
        ROOT / "templates/github/run-isolated-validation.sh"
    ).read_bytes() == (ROOT / "runner/run-isolated-validation.sh").read_bytes()
