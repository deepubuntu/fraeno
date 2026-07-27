from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fraeno import __version__
from fraeno.config import FraenoConfig
from fraeno.validation.compare import ComparisonReport, Outcome, compare_systems
from fraeno.validation.observation import ObservationError, SystemObservation


@dataclass(frozen=True)
class StepResult:
    name: str
    command: tuple[str, ...]
    exit_code: int
    duration_seconds: float
    stdout: str
    stderr: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["command"] = list(self.command)
        return value

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> StepResult:
        command = raw.get("command")
        if not isinstance(command, list) or not all(
            isinstance(part, str) for part in command
        ):
            raise ValueError("workspace step command must be a list of strings")
        return cls(
            name=str(raw["name"]),
            command=tuple(command),
            exit_code=int(raw["exit_code"]),
            duration_seconds=float(raw["duration_seconds"]),
            stdout=str(raw["stdout"]),
            stderr=str(raw["stderr"]),
        )


@dataclass(frozen=True)
class WorkspaceRun:
    phase: str
    workspace: str
    steps: tuple[StepResult, ...]
    observation: SystemObservation | None
    error: str | None

    @property
    def succeeded(self) -> bool:
        return self.error is None and all(step.exit_code == 0 for step in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "workspace": self.workspace,
            "succeeded": self.succeeded,
            "steps": [step.to_dict() for step in self.steps],
            "observation": self.observation.to_dict() if self.observation else None,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> WorkspaceRun:
        raw_steps = raw.get("steps")
        if not isinstance(raw_steps, list):
            raise ValueError("workspace run steps must be a list")
        if not all(isinstance(step, dict) for step in raw_steps):
            raise ValueError("workspace run steps must contain objects")
        raw_observation = raw.get("observation")
        if raw_observation is not None and not isinstance(raw_observation, dict):
            raise ValueError("workspace observation must be an object or null")
        raw_error = raw.get("error")
        if raw_error is not None and not isinstance(raw_error, str):
            raise ValueError("workspace error must be a string or null")
        return cls(
            phase=str(raw["phase"]),
            workspace=str(raw["workspace"]),
            steps=tuple(StepResult.from_dict(step) for step in raw_steps),
            observation=(
                SystemObservation.from_dict(raw_observation)
                if raw_observation is not None
                else None
            ),
            error=raw_error,
        )


@dataclass(frozen=True)
class ValidationRun:
    engine_version: str
    baseline: WorkspaceRun
    candidate: WorkspaceRun
    comparison: ComparisonReport | None

    @property
    def outcome(self) -> Outcome:
        if not self.baseline.succeeded:
            return Outcome.ERROR
        if not self.candidate.succeeded:
            return Outcome.BLOCK
        if self.comparison is None:
            return Outcome.ERROR
        return self.comparison.outcome

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "engine": {"name": "fraeno", "version": self.engine_version},
            "outcome": self.outcome.value,
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "comparison": self.comparison.to_dict() if self.comparison else None,
        }


def run_validation(
    baseline_path: Path,
    candidate_path: Path,
    config: FraenoConfig,
) -> ValidationRun:
    baseline_domain_id = secrets.randbelow(50) * 2 + 1
    baseline = run_workspace(
        baseline_path,
        config,
        "baseline",
        ros_domain_id=baseline_domain_id,
        trusted_workspace=baseline_path,
    )
    if not baseline.succeeded:
        return ValidationRun(
            engine_version=__version__,
            baseline=baseline,
            candidate=_not_run(candidate_path, "candidate", "Baseline is invalid."),
            comparison=None,
        )

    candidate = run_workspace(
        candidate_path,
        config,
        "candidate",
        ros_domain_id=baseline_domain_id + 1,
        trusted_workspace=baseline_path,
    )
    if (
        not candidate.succeeded
        or baseline.observation is None
        or candidate.observation is None
    ):
        return ValidationRun(
            engine_version=__version__,
            baseline=baseline,
            candidate=candidate,
            comparison=None,
        )

    return ValidationRun(
        engine_version=__version__,
        baseline=baseline,
        candidate=candidate,
        comparison=compare_systems(
            baseline.observation, candidate.observation, config.validation
        ),
    )


def run_workspace(
    workspace: Path,
    config: FraenoConfig,
    phase: str,
    *,
    ros_domain_id: int | None = None,
    command_uid: int | None = None,
    command_gid: int | None = None,
    trusted_workspace: Path | None = None,
) -> WorkspaceRun:
    workspace = workspace.resolve()
    if not workspace.is_dir():
        return _not_run(workspace, phase, f"Workspace does not exist: {workspace}")

    environment = os.environ.copy()
    environment["PATH"] = (
        str(Path(sys.executable).parent)
        + os.pathsep
        + environment.get("PATH", "")
    )
    environment["FRAENO_PHASE"] = phase
    environment["FRAENO_PROJECT"] = config.project_name
    if command_uid is not None:
        sandbox_home = workspace / ".fraeno-home"
        environment["HOME"] = str(sandbox_home)
        environment["COLCON_HOME"] = str(sandbox_home / ".colcon")
        environment["XDG_CACHE_HOME"] = str(sandbox_home / ".cache")
    if trusted_workspace is not None:
        environment["FRAENO_TRUSTED_ROOT"] = str(trusted_workspace.resolve())
    if ros_domain_id is not None:
        environment["ROS_DOMAIN_ID"] = str(ros_domain_id)
    step_results: list[StepResult] = []

    for step in config.validation.steps:
        result = _run_command(
            step.name,
            step.command,
            workspace,
            environment,
            step.timeout_seconds,
            command_uid=command_uid,
            command_gid=command_gid,
        )
        step_results.append(result)
        if result.exit_code != 0:
            return WorkspaceRun(
                phase=phase,
                workspace=workspace.as_posix(),
                steps=tuple(step_results),
                observation=None,
                error=f"Step failed: {step.name}",
            )

    observation_result = _run_command(
        "observe robot system",
        config.validation.observation_command,
        workspace,
        environment,
        config.validation.observation_timeout_seconds,
        command_uid=command_uid,
        command_gid=command_gid,
    )
    step_results.append(observation_result)
    if observation_result.exit_code != 0:
        return WorkspaceRun(
            phase=phase,
            workspace=workspace.as_posix(),
            steps=tuple(step_results),
            observation=None,
            error="Observation command failed.",
        )
    try:
        raw = json.loads(observation_result.stdout)
        if not isinstance(raw, dict):
            raise ObservationError("observation must be a JSON object")
        observation = SystemObservation.from_dict(raw)
    except (json.JSONDecodeError, ObservationError, TypeError, ValueError) as error:
        return WorkspaceRun(
            phase=phase,
            workspace=workspace.as_posix(),
            steps=tuple(step_results),
            observation=None,
            error=f"Invalid observation: {error}",
        )

    return WorkspaceRun(
        phase=phase,
        workspace=workspace.as_posix(),
        steps=tuple(step_results),
        observation=observation,
        error=None,
    )


def _run_command(
    name: str,
    command: tuple[str, ...],
    workspace: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    *,
    command_uid: int | None = None,
    command_gid: int | None = None,
) -> StepResult:
    started = time.monotonic()
    try:
        identity: dict[str, Any] = {}
        if command_uid is not None:
            identity["user"] = command_uid
            identity["extra_groups"] = ()
        if command_gid is not None:
            identity["group"] = command_gid
        completed = subprocess.run(
            command,
            cwd=workspace,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            start_new_session=True,
            **identity,
        )
        return StepResult(
            name=name,
            command=command,
            exit_code=completed.returncode,
            duration_seconds=round(time.monotonic() - started, 3),
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    except subprocess.TimeoutExpired as error:
        return StepResult(
            name=name,
            command=command,
            exit_code=124,
            duration_seconds=round(time.monotonic() - started, 3),
            stdout=_decoded(error.stdout),
            stderr=_decoded(error.stderr) + f"\nTimed out after {timeout_seconds} seconds.",
        )
    except OSError as error:
        return StepResult(
            name=name,
            command=command,
            exit_code=127,
            duration_seconds=round(time.monotonic() - started, 3),
            stdout="",
            stderr=str(error),
        )


def _decoded(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def _not_run(workspace: Path, phase: str, error: str) -> WorkspaceRun:
    return WorkspaceRun(
        phase=phase,
        workspace=workspace.resolve().as_posix(),
        steps=(),
        observation=None,
        error=error,
    )
