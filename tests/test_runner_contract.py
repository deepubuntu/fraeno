import os
from pathlib import Path

import pytest
import yaml

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
    updates = (ROOT / "templates/github/fraeno-updates.yml").read_text()

    assert "FRAENO_RUNNER_IMAGE" in workflow
    assert "@sha256:" in workflow
    assert "inputs.base_repository" in workflow
    assert "inputs.head_repository" in workflow
    assert "fixtures/ros2_qos_robot" not in workflow
    assert "src/fraeno" not in workflow
    assert "FRAENO_RUNNER_IMAGE" in updates
    assert "python -m pip install ." not in updates
    assert "--ros-distro humble" in updates
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in updates
    assert (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
        in updates
    )


def test_customer_update_template_keeps_token_out_of_runner_container() -> None:
    raw = yaml.safe_load(
        (ROOT / "templates/github/fraeno-updates.yml").read_text()
    )
    steps = raw["jobs"]["propose"]["steps"]
    checkout = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["persist-credentials"] is False

    token_steps = {
        step["name"]
        for step in steps
        if step.get("env", {}).get("GH_TOKEN") == "${{ github.token }}"
    }
    assert token_steps == {
        "Read open Fraeno pull requests",
        "Publish deterministic update branch",
        "Create or refresh update pull request",
    }

    runner_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Apply one policy-approved proposal"
    )
    publish_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Publish deterministic update branch"
    )
    assert runner_index < publish_index
    assert "GH_TOKEN" not in steps[runner_index].get("env", {})
    assert '--volume "$PWD/.git:/workspace/.git:ro"' in (
        steps[runner_index]["run"]
    )
    assert "gh auth setup-git" in steps[publish_index]["run"]
    assert all(
        "docker run" not in str(step.get("run", ""))
        for step in steps[publish_index:]
    )


def test_update_git_boundary_has_an_adversarial_container_proof() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    proof = (
        ROOT / "tests/container/test-update-workflow-sandbox.sh"
    ).read_text()

    assert "test-update-workflow-sandbox.sh" in workflow
    assert '--volume "$repository/.git:/workspace/.git:ro"' in proof
    assert 'printf "malicious hook' in proof
    assert 'printf "malicious config' in proof
    assert "mv .git .git-shadow" in proof
    assert "git hash-object" in proof


def test_customer_orchestrator_matches_the_reviewed_runner_script() -> None:
    assert (
        ROOT / "templates/github/run-isolated-validation.sh"
    ).read_bytes() == (ROOT / "runner/run-isolated-validation.sh").read_bytes()


def test_customer_templates_are_packaged_for_fraeno_init() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()

    assert '"templates/github" = "fraeno/templates/github"' in pyproject
    assert "COPY templates ./templates" in (ROOT / "runner/Dockerfile").read_text()
