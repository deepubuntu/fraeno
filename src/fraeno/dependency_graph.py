from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from fraeno.models import Dependency, Ecosystem, ScanReport, SourceLocation

UNKNOWN = "unknown"

_UBUNTU_RELEASES = {
    "focal": "20.04",
    "jammy": "22.04",
    "noble": "24.04",
}

_COMPONENT_ALIASES = {
    "eigen3": "eigen",
    "libeigen3-dev": "eigen",
    "libopencv-dev": "opencv",
    "opencv": "opencv",
    "opencv-python": "opencv",
    "opencv-python-headless": "opencv",
    "opencv4": "opencv",
    "protobuf": "protobuf",
    "protobuf-compiler": "protobuf",
    "libprotobuf-dev": "protobuf",
}


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    DECLARED = "declared"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TargetPlatform:
    ros_distribution: str = UNKNOWN
    operating_system: str = UNKNOWN
    operating_system_version: str = UNKNOWN
    architecture: str = UNKNOWN

    def to_dict(self) -> dict[str, str]:
        return {
            "ros_distribution": self.ros_distribution,
            "operating_system": self.operating_system,
            "operating_system_version": self.operating_system_version,
            "architecture": self.architecture,
        }


@dataclass(frozen=True)
class Provenance:
    provider: str
    source: str
    evidence: str
    metadata: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "source": self.source,
            "evidence": self.evidence,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class ResolutionResult:
    status: ResolutionStatus
    version: str | None = None
    artifact_name: str | None = None
    component: str | None = None
    reason: str | None = None
    provenance: tuple[Provenance, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)


class ResolutionProvider(Protocol):
    name: str

    def resolve(
        self,
        dependency: Dependency,
        target: TargetPlatform,
    ) -> ResolutionResult | None: ...


@dataclass(frozen=True)
class ManifestNode:
    id: str
    path: str
    kind: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "path": self.path,
            "kind": self.kind,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class DeclarationNode:
    id: str
    manifest_id: str
    ecosystem: Ecosystem
    name: str
    source: SourceLocation
    dependency_type: str
    constraint: str | None
    declared_value: str | None
    component_id: str
    component_link: str
    target: TargetPlatform
    container_stage: str | None
    resolution_status: ResolutionStatus
    artifact_id: str | None
    unknown_reason: str | None
    provenance: tuple[Provenance, ...]
    metadata: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "manifest_id": self.manifest_id,
            "ecosystem": self.ecosystem.value,
            "name": self.name,
            "source": {
                "path": self.source.path,
                "line": self.source.line,
            },
            "dependency_type": self.dependency_type,
            "constraint": self.constraint,
            "declared_value": self.declared_value,
            "component_id": self.component_id,
            "component_link": self.component_link,
            "target": {
                **self.target.to_dict(),
                "container_stage": self.container_stage,
            },
            "resolution": {
                "status": self.resolution_status.value,
                "artifact_id": self.artifact_id,
                "unknown_reason": self.unknown_reason,
                "provenance": [item.to_dict() for item in self.provenance],
            },
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class ArtifactNode:
    id: str
    ecosystem: Ecosystem
    name: str
    version: str
    component_id: str
    target: TargetPlatform
    container_stage: str | None
    resolution_status: ResolutionStatus
    provenance: tuple[Provenance, ...]
    metadata: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ecosystem": self.ecosystem.value,
            "name": self.name,
            "version": self.version,
            "component_id": self.component_id,
            "target": {
                **self.target.to_dict(),
                "container_stage": self.container_stage,
            },
            "resolution_status": self.resolution_status.value,
            "provenance": [item.to_dict() for item in self.provenance],
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class ComponentNode:
    id: str
    name: str
    declarations: tuple[str, ...]
    artifacts: tuple[str, ...]
    ecosystems: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "declarations": list(self.declarations),
            "artifacts": list(self.artifacts),
            "ecosystems": list(self.ecosystems),
        }


@dataclass(frozen=True, order=True)
class DependencyEdge:
    source: str
    target: str
    relationship: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "target": self.target,
            "relationship": self.relationship,
        }


@dataclass(frozen=True)
class ResolvedDependencyGraph:
    target: TargetPlatform
    manifests: tuple[ManifestNode, ...]
    declarations: tuple[DeclarationNode, ...]
    artifacts: tuple[ArtifactNode, ...]
    components: tuple[ComponentNode, ...]
    edges: tuple[DependencyEdge, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "target": self.target.to_dict(),
            "manifests": [item.to_dict() for item in self.manifests],
            "declarations": [item.to_dict() for item in self.declarations],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "components": [item.to_dict() for item in self.components],
            "edges": [item.to_dict() for item in self.edges],
            "warnings": list(self.warnings),
        }


class ManifestResolutionProvider:
    name = "manifest"

    def resolve(
        self,
        dependency: Dependency,
        target: TargetPlatform,
    ) -> ResolutionResult | None:
        del target
        source = _source_locator(dependency.source)
        if dependency.resolved is None:
            return ResolutionResult(
                status=ResolutionStatus.UNKNOWN,
                reason=_unknown_reason(dependency),
                provenance=(
                    Provenance(
                        provider=self.name,
                        source=source,
                        evidence="The declaration does not identify one concrete artifact.",
                    ),
                ),
            )

        immutable = (
            dependency.ecosystem is Ecosystem.DOCKER and dependency.resolved.startswith("sha256:")
        ) or (
            dependency.ecosystem is Ecosystem.GIT
            and re.fullmatch(r"[0-9a-fA-F]{7,64}", dependency.resolved) is not None
        )
        return ResolutionResult(
            status=(ResolutionStatus.RESOLVED if immutable else ResolutionStatus.DECLARED),
            version=dependency.resolved,
            artifact_name=dependency.name,
            provenance=(
                Provenance(
                    provider=self.name,
                    source=source,
                    evidence=(
                        "The manifest pins an immutable reference."
                        if immutable
                        else "The manifest declares an exact version or tag."
                    ),
                ),
            ),
        )


class DependencyGraphBuilder:
    def __init__(
        self,
        target: TargetPlatform,
        providers: Sequence[ResolutionProvider] = (),
    ) -> None:
        self.target = target
        self.providers = (*providers, ManifestResolutionProvider())

    def build(self, report: ScanReport, root: Path) -> ResolvedDependencyGraph:
        manifests = self._manifests(report, root)
        manifest_ids = {item.path: item.id for item in manifests}
        declarations: list[DeclarationNode] = []
        artifact_by_id: dict[str, ArtifactNode] = {}
        edges: set[DependencyEdge] = set()
        occurrences: dict[tuple[str, ...], int] = {}

        for dependency in report.dependencies:
            base_key = _declaration_base_key(dependency)
            occurrence = occurrences.get(base_key, 0) + 1
            occurrences[base_key] = occurrence
            declaration_id = _stable_id(
                "declaration",
                *base_key,
                str(occurrence),
            )
            component, component_link = component_identity(dependency)
            resolution = self._resolve(dependency)
            if resolution.component:
                component = _normalize_component(resolution.component)
                component_link = f"provider:{self._provider_name(resolution)}"
            component_id = f"component:{component}"
            container_stage = _optional_string(dependency.metadata.get("container_stage"))
            artifact_id: str | None = None
            if resolution.version is not None:
                artifact_name = resolution.artifact_name or dependency.name
                artifact_id = _artifact_id(
                    dependency.ecosystem,
                    artifact_name,
                    resolution.version,
                    self.target,
                    container_stage,
                )
                artifact_by_id.setdefault(
                    artifact_id,
                    ArtifactNode(
                        id=artifact_id,
                        ecosystem=dependency.ecosystem,
                        name=artifact_name,
                        version=resolution.version,
                        component_id=component_id,
                        target=self.target,
                        container_stage=container_stage,
                        resolution_status=resolution.status,
                        provenance=resolution.provenance,
                        metadata=resolution.metadata,
                    ),
                )

            manifest_id = manifest_ids[dependency.source.path]
            declaration = DeclarationNode(
                id=declaration_id,
                manifest_id=manifest_id,
                ecosystem=dependency.ecosystem,
                name=dependency.name,
                source=dependency.source,
                dependency_type=dependency.dependency_type,
                constraint=dependency.constraint,
                declared_value=dependency.resolved or dependency.constraint,
                component_id=component_id,
                component_link=component_link,
                target=self.target,
                container_stage=container_stage,
                resolution_status=resolution.status,
                artifact_id=artifact_id,
                unknown_reason=resolution.reason,
                provenance=resolution.provenance,
                metadata=dependency.metadata,
            )
            declarations.append(declaration)
            edges.add(
                DependencyEdge(
                    source=manifest_id,
                    target=declaration_id,
                    relationship="declares",
                )
            )
            edges.add(
                DependencyEdge(
                    source=declaration_id,
                    target=component_id,
                    relationship="represents",
                )
            )
            if artifact_id is not None:
                edges.add(
                    DependencyEdge(
                        source=declaration_id,
                        target=artifact_id,
                        relationship="resolves_to",
                    )
                )
                edges.add(
                    DependencyEdge(
                        source=artifact_id,
                        target=component_id,
                        relationship="represents",
                    )
                )

        declarations.sort(key=lambda item: item.id)
        artifacts = tuple(sorted(artifact_by_id.values(), key=lambda item: item.id))
        components = self._components(declarations, artifacts)
        return ResolvedDependencyGraph(
            target=self.target,
            manifests=tuple(manifests),
            declarations=tuple(declarations),
            artifacts=artifacts,
            components=components,
            edges=tuple(sorted(edges)),
            warnings=tuple(sorted(report.warnings)),
        )

    def _resolve(self, dependency: Dependency) -> ResolutionResult:
        for provider in self.providers:
            result = provider.resolve(dependency, self.target)
            if result is not None:
                return result
        raise RuntimeError("the manifest resolution provider must return a result")

    def _provider_name(self, resolution: ResolutionResult) -> str:
        if resolution.provenance:
            return resolution.provenance[0].provider
        return UNKNOWN

    @staticmethod
    def _manifests(report: ScanReport, root: Path) -> list[ManifestNode]:
        manifests = [
            ManifestNode(
                id=f"manifest:{relative}",
                path=relative,
                kind=_manifest_kind(relative),
                sha256=hashlib.sha256((root / relative).read_bytes()).hexdigest(),
            )
            for relative in report.files_scanned
        ]
        return sorted(manifests, key=lambda item: item.id)

    @staticmethod
    def _components(
        declarations: list[DeclarationNode],
        artifacts: tuple[ArtifactNode, ...],
    ) -> tuple[ComponentNode, ...]:
        artifact_ids: dict[str, list[str]] = {}
        for artifact in artifacts:
            artifact_ids.setdefault(artifact.component_id, []).append(artifact.id)

        grouped: dict[str, list[DeclarationNode]] = {}
        for declaration in declarations:
            grouped.setdefault(declaration.component_id, []).append(declaration)

        components = [
            ComponentNode(
                id=component_id,
                name=component_id.removeprefix("component:"),
                declarations=tuple(sorted(item.id for item in items)),
                artifacts=tuple(sorted(artifact_ids.get(component_id, []))),
                ecosystems=tuple(sorted({item.ecosystem.value for item in items})),
            )
            for component_id, items in grouped.items()
        ]
        return tuple(sorted(components, key=lambda item: item.id))


def infer_target_platform(report: ScanReport) -> TargetPlatform:
    ros_distributions: set[str] = set()
    operating_systems: set[str] = set()
    operating_system_versions: set[str] = set()
    architectures: set[str] = set()

    for dependency in report.dependencies:
        if dependency.ecosystem is not Ecosystem.DOCKER:
            continue
        platform = _optional_string(dependency.metadata.get("platform"))
        if platform:
            architecture = platform.rsplit("/", maxsplit=1)[-1]
            if architecture:
                architectures.add(architecture)

        reference = _optional_string(dependency.metadata.get("reference"))
        if not reference:
            continue
        tag = _docker_tag(reference)
        if tag is None:
            continue
        ros_match = re.match(
            r"^(?P<ros>[a-z][a-z0-9]*)-ros-[a-z0-9-]*-(?P<ubuntu>focal|jammy|noble)$",
            tag.lower(),
        )
        if ros_match:
            ros_distributions.add(ros_match.group("ros"))
            operating_systems.add("ubuntu")
            operating_system_versions.add(_UBUNTU_RELEASES[ros_match.group("ubuntu")])

    return TargetPlatform(
        ros_distribution=_single_or_unknown(ros_distributions),
        operating_system=_single_or_unknown(operating_systems),
        operating_system_version=_single_or_unknown(operating_system_versions),
        architecture=_single_or_unknown(architectures),
    )


def component_identity(dependency: Dependency) -> tuple[str, str]:
    normalized = _normalize_component(dependency.name)
    alias = _COMPONENT_ALIASES.get(normalized)
    if alias is not None:
        return alias, "known_alias"
    return f"{dependency.ecosystem.value}:{normalized}", "ecosystem_identity"


def _normalize_component(name: str) -> str:
    normalized = re.sub(r"[-_.]+", "-", name.strip().lower())
    return normalized.strip("-")


def _source_locator(source: SourceLocation) -> str:
    if source.line is None:
        return source.path
    return f"{source.path}#{source.line}"


def _unknown_reason(dependency: Dependency) -> str:
    reasons = {
        Ecosystem.ROS: "The ROS key has not been resolved through rosdep for this target.",
        Ecosystem.CMAKE: "CMake discovery does not identify a concrete artifact.",
        Ecosystem.PYTHON: "The Python declaration is not an exact version pin.",
        Ecosystem.DOCKER: "The container image has no tag or digest.",
        Ecosystem.APT: "The APT declaration has no exact version pin.",
        Ecosystem.GIT: "The Git declaration has no revision.",
    }
    return reasons[dependency.ecosystem]


def _declaration_base_key(dependency: Dependency) -> tuple[str, ...]:
    return (
        dependency.ecosystem.value,
        dependency.name.lower(),
        dependency.source.path,
        dependency.dependency_type,
        _optional_string(dependency.metadata.get("container_stage")) or "",
    )


def _artifact_id(
    ecosystem: Ecosystem,
    name: str,
    version: str,
    target: TargetPlatform,
    container_stage: str | None,
) -> str:
    return _stable_id(
        "artifact",
        ecosystem.value,
        name.lower(),
        version,
        target.ros_distribution,
        target.operating_system,
        target.operating_system_version,
        target.architecture,
        container_stage or "",
    )


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\0".join(parts).encode()
    return f"{prefix}:{hashlib.sha256(payload).hexdigest()[:20]}"


def _manifest_kind(relative: str) -> str:
    name = Path(relative).name
    if name == "package.xml":
        return "ros_package"
    if name == "CMakeLists.txt":
        return "cmake"
    if name == "pyproject.toml":
        return "python_project"
    if name.startswith("requirements") and name.endswith(".txt"):
        return "python_requirements"
    if name.lower().startswith("dockerfile"):
        return "dockerfile"
    if name == ".gitmodules":
        return "gitmodules"
    if name.endswith(".repos"):
        return "vcstool"
    return "unknown"


def _docker_tag(reference: str) -> str | None:
    if "@" in reference:
        return None
    final_segment = reference.rsplit("/", maxsplit=1)[-1]
    if ":" not in final_segment:
        return None
    return reference.rsplit(":", maxsplit=1)[-1]


def _single_or_unknown(values: set[str]) -> str:
    if len(values) == 1:
        return next(iter(values))
    return UNKNOWN


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None
