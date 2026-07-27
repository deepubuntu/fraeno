from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from packaging.requirements import Requirement

from fraeno.dependency_graph import TargetPlatform
from fraeno.models import Dependency, Ecosystem, ScanReport
from fraeno.update_discovery import (
    DEFAULT_PROVIDERS,
    PythonUpdateProvider,
    RegistryUpdateCatalog,
    UpdateCandidate,
    UpdateCatalog,
    UpdateDiscoveryProvider,
    UpdateDiscoveryReport,
    discover_updates,
)


class UpdateError(ValueError):
    pass


@dataclass(frozen=True)
class UpdateResult:
    dependency: str
    previous: str
    target: str
    changed_files: tuple[str, ...]
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def find_python_updates(
    report: ScanReport,
    client: httpx.Client | None = None,
) -> tuple[list[UpdateCandidate], list[str]]:
    catalog = RegistryUpdateCatalog(client)
    try:
        discovery = discover_updates(
            report,
            catalog=catalog,
            providers=(PythonUpdateProvider(),),
        )
    finally:
        catalog.close()
    warnings = list(discovery.warnings)
    warnings.extend(
        f"{item.identity}: {item.reason}" for item in discovery.refusals
    )
    return list(discovery.candidates), warnings


def apply_update(
    root: Path,
    report: ScanReport,
    identity: str,
    target: str,
    *,
    dry_run: bool = False,
) -> UpdateResult:
    if ":" not in identity:
        raise UpdateError("dependency must use the form ecosystem:name")
    ecosystem_name, name = identity.split(":", maxsplit=1)
    try:
        ecosystem = Ecosystem(ecosystem_name)
    except ValueError as error:
        raise UpdateError(f"unsupported ecosystem: {ecosystem_name}") from error

    matches = [
        dependency
        for dependency in report.dependencies
        if dependency.ecosystem is ecosystem
        and dependency.name.lower() == name.lower()
    ]
    if not matches:
        raise UpdateError(f"dependency not found: {identity}")
    if ecosystem in {Ecosystem.ROS, Ecosystem.CMAKE}:
        raise UpdateError(
            f"{ecosystem.value} declarations are observed but not automatically "
            "rewritten in Fraeno v1"
        )

    previous_values = {
        value
        for dependency in matches
        if (value := dependency.resolved or dependency.constraint)
    }
    if len(previous_values) != 1:
        raise UpdateError(
            "all managed declarations must currently resolve to the same value"
        )
    previous = next(iter(previous_values))
    changed_files: list[str] = []
    for relative in sorted({dependency.source.path for dependency in matches}):
        source_matches = [
            dependency for dependency in matches if dependency.source.path == relative
        ]
        path = root / relative
        original = path.read_text()
        updated = _rewrite(path, original, source_matches, target)
        if updated == original:
            raise UpdateError(f"could not safely rewrite {relative}")
        changed_files.append(relative)
        if not dry_run:
            path.write_text(updated)

    return UpdateResult(
        dependency=identity,
        previous=previous,
        target=target,
        changed_files=tuple(changed_files),
        dry_run=dry_run,
    )


def apply_next_python_update(
    root: Path,
    report: ScanReport,
    *,
    client: httpx.Client | None = None,
    dry_run: bool = False,
) -> tuple[UpdateResult | None, list[str]]:
    candidates, warnings = find_python_updates(report, client)
    if not candidates:
        return None, warnings
    candidate = candidates[0]
    return (
        apply_update(
            root,
            report,
            candidate.identity,
            candidate.target,
            dry_run=dry_run,
        ),
        warnings,
    )


def apply_next_update(
    root: Path,
    report: ScanReport,
    *,
    target: TargetPlatform | None = None,
    catalog: UpdateCatalog | None = None,
    providers: Sequence[UpdateDiscoveryProvider] = DEFAULT_PROVIDERS,
    dry_run: bool = False,
) -> tuple[UpdateResult | None, UpdateDiscoveryReport]:
    discovery = discover_updates(
        report,
        target=target,
        catalog=catalog,
        providers=providers,
    )
    managed = {
        Ecosystem.PYTHON,
        Ecosystem.DOCKER,
        Ecosystem.APT,
        Ecosystem.GIT,
    }
    candidate = next(
        (item for item in discovery.candidates if item.ecosystem in managed),
        None,
    )
    if candidate is None:
        return None, discovery
    return (
        apply_update(
            root,
            report,
            candidate.identity,
            candidate.target,
            dry_run=dry_run,
        ),
        discovery,
    )


def _rewrite(
    path: Path,
    text: str,
    dependencies: list[Dependency],
    target: str,
) -> str:
    dependency = dependencies[0]
    if dependency.ecosystem is Ecosystem.PYTHON:
        if path.name == "pyproject.toml":
            return _rewrite_pyproject(text, dependencies, target)
        return _rewrite_requirements(text, dependencies, target)
    if dependency.ecosystem is Ecosystem.DOCKER:
        return _rewrite_docker_image(text, dependency, target)
    if dependency.ecosystem is Ecosystem.APT:
        return _rewrite_apt(text, dependency, target)
    if dependency.ecosystem is Ecosystem.GIT:
        if dependency.metadata.get("manifest") != "vcstool":
            raise UpdateError("git submodule updates require the git index and are not v1")
        return _rewrite_repos(text, dependency, target)
    raise UpdateError(f"no safe updater for {dependency.ecosystem.value}")


def _rewrite_requirements(
    text: str, dependencies: list[Dependency], target: str
) -> str:
    lines = text.splitlines(keepends=True)
    for dependency in dependencies:
        if dependency.source.line is None:
            raise UpdateError("requirement source line is unknown")
        index = dependency.source.line - 1
        raw = lines[index]
        newline = "\n" if raw.endswith("\n") else ""
        requirement = Requirement(raw.strip())
        extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
        marker = f"; {requirement.marker}" if requirement.marker else ""
        lines[index] = f"{requirement.name}{extras}=={target}{marker}{newline}"
    return "".join(lines)


def _rewrite_pyproject(
    text: str, dependencies: list[Dependency], target: str
) -> str:
    lines = text.splitlines(keepends=True)
    for dependency in dependencies:
        if dependency.source.line is None:
            raise UpdateError("pyproject source line is unknown")
        index = dependency.source.line - 1
        raw = lines[index]
        match = re.search(r"(?P<quote>['\"])(?P<requirement>[^'\"]+)(?P=quote)", raw)
        if not match:
            raise UpdateError("only quoted PEP 508 pyproject dependencies are managed")
        requirement = Requirement(match.group("requirement"))
        extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
        marker = f"; {requirement.marker}" if requirement.marker else ""
        replacement = f"{requirement.name}{extras}=={target}{marker}"
        lines[index] = (
            raw[: match.start("requirement")]
            + replacement
            + raw[match.end("requirement") :]
        )
    return "".join(lines)


def _rewrite_docker_image(
    text: str, dependency: Dependency, target: str
) -> str:
    reference = str(dependency.metadata.get("reference") or "")
    if not reference:
        raise UpdateError("Docker image reference is unavailable")
    image = dependency.name
    new_reference = f"{image}@{target}" if target.startswith("sha256:") else f"{image}:{target}"
    return text.replace(reference, new_reference, 1)


def _rewrite_apt(text: str, dependency: Dependency, target: str) -> str:
    if not dependency.resolved:
        raise UpdateError("unversioned APT packages are observed but not rewritten")
    pattern = re.compile(
        rf"(?<![A-Za-z0-9+.-]){re.escape(dependency.name)}="
        rf"{re.escape(dependency.resolved)}(?![A-Za-z0-9+.-])"
    )
    return pattern.sub(f"{dependency.name}={target}", text, count=1)


def _rewrite_repos(
    text: str, dependency: Dependency, target: str
) -> str:
    lines = text.splitlines(keepends=True)
    repository_pattern = re.compile(
        rf"^(?P<indent>\s*){re.escape(dependency.name)}\s*:\s*(?:#.*)?$"
    )
    start: int | None = None
    indent = 0
    for index, line in enumerate(lines):
        match = repository_pattern.match(line.rstrip("\n"))
        if match:
            start = index
            indent = len(match.group("indent"))
            break
    if start is None:
        raise UpdateError("repository entry could not be located structurally")
    for index in range(start + 1, len(lines)):
        stripped = lines[index].lstrip()
        current_indent = len(lines[index]) - len(stripped)
        if stripped.strip() and current_indent <= indent:
            break
        if re.match(r"\s*version\s*:", lines[index]):
            newline = "\n" if lines[index].endswith("\n") else ""
            prefix = lines[index][: len(lines[index]) - len(stripped)]
            lines[index] = f"{prefix}version: {target}{newline}"
            return "".join(lines)
    raise UpdateError("repository version field could not be located")
