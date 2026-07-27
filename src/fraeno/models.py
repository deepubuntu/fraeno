from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Ecosystem(StrEnum):
    ROS = "ros"
    CMAKE = "cmake"
    PYTHON = "python"
    DOCKER = "docker"
    APT = "apt"
    GIT = "git"


@dataclass(frozen=True, order=True)
class SourceLocation:
    path: str
    line: int | None = None


@dataclass(frozen=True)
class Dependency:
    ecosystem: Ecosystem
    name: str
    source: SourceLocation
    constraint: str | None = None
    resolved: str | None = None
    dependency_type: str = "runtime"
    metadata: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def identity(self) -> str:
        return f"{self.ecosystem.value}:{self.name}"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["ecosystem"] = self.ecosystem.value
        return value


@dataclass
class ScanReport:
    root: str
    dependencies: list[Dependency]
    files_scanned: list[str]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "root": self.root,
            "dependencies": [dependency.to_dict() for dependency in self.dependencies],
            "files_scanned": self.files_scanned,
            "warnings": self.warnings,
            "summary": {
                "dependencies": len(self.dependencies),
                "files": len(self.files_scanned),
                "ecosystems": sorted(
                    {dependency.ecosystem.value for dependency in self.dependencies}
                ),
            },
        }
