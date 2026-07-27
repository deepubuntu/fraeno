from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fraeno import __version__
from fraeno.config import FraenoConfig
from fraeno.validation.compare import compare_systems
from fraeno.validation.runner import ValidationRun, WorkspaceRun


class RunnerContractError(ValueError):
    pass


@dataclass(frozen=True)
class CapturedWorkspace:
    engine_version: str
    run: WorkspaceRun

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "engine": {"name": "fraeno", "version": self.engine_version},
            "workspace_run": self.run.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CapturedWorkspace:
        if raw.get("schema_version") != 1:
            raise RunnerContractError("unsupported workspace evidence schema")
        engine = raw.get("engine")
        if not isinstance(engine, dict) or engine.get("name") != "fraeno":
            raise RunnerContractError("workspace evidence engine is not Fraeno")
        engine_version = engine.get("version")
        if not isinstance(engine_version, str) or not engine_version:
            raise RunnerContractError("workspace evidence engine version is missing")
        raw_run = raw.get("workspace_run")
        if not isinstance(raw_run, dict):
            raise RunnerContractError("workspace evidence run is missing")
        return cls(
            engine_version=engine_version,
            run=WorkspaceRun.from_dict(raw_run),
        )


def assemble_validation(
    baseline: CapturedWorkspace,
    candidate: CapturedWorkspace,
    config: FraenoConfig,
) -> ValidationRun:
    if baseline.engine_version != candidate.engine_version:
        raise RunnerContractError(
            "baseline and candidate evidence use different Fraeno versions"
        )
    if baseline.engine_version != __version__:
        raise RunnerContractError(
            "evidence was not produced by this Fraeno engine version"
        )
    if baseline.run.phase != "baseline":
        raise RunnerContractError("baseline evidence has the wrong phase")
    if candidate.run.phase != "candidate":
        raise RunnerContractError("candidate evidence has the wrong phase")

    comparison = None
    if (
        baseline.run.succeeded
        and candidate.run.succeeded
        and baseline.run.observation is not None
        and candidate.run.observation is not None
    ):
        comparison = compare_systems(
            baseline.run.observation,
            candidate.run.observation,
            config.validation,
        )
    return ValidationRun(
        engine_version=__version__,
        baseline=baseline.run,
        candidate=candidate.run,
        comparison=comparison,
    )
