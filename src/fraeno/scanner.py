from __future__ import annotations

import configparser
import re
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from pathlib import Path

import yaml
from packaging.requirements import InvalidRequirement, Requirement

from fraeno.models import Dependency, Ecosystem, ScanReport, SourceLocation

IGNORED_DIRECTORIES = {
    ".git",
    ".fraeno",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "install",
    "log",
    "node_modules",
}

PACKAGE_XML_TAGS = {
    "depend": "runtime",
    "build_depend": "build",
    "build_export_depend": "build",
    "buildtool_depend": "build",
    "exec_depend": "runtime",
    "test_depend": "test",
}


class RepositoryScanner:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.warnings: list[str] = []

    def scan(self) -> ScanReport:
        dependencies: list[Dependency] = []
        files_scanned: list[str] = []

        for path in self._candidate_files():
            parser = self._parser_for(path)
            if parser is None:
                continue
            relative = path.relative_to(self.root).as_posix()
            try:
                dependencies.extend(parser(path, relative))
                files_scanned.append(relative)
            except (OSError, UnicodeDecodeError, ValueError, ET.ParseError) as error:
                self.warnings.append(f"{relative}: {error}")

        dependencies.sort(
            key=lambda item: (
                item.ecosystem.value,
                item.name.lower(),
                item.source.path,
                item.source.line or 0,
            )
        )
        return ScanReport(
            root=self.root.as_posix(),
            dependencies=dependencies,
            files_scanned=sorted(files_scanned),
            warnings=self.warnings,
        )

    def _candidate_files(self) -> Iterable[Path]:
        for path in self.root.rglob("*"):
            if any(part in IGNORED_DIRECTORIES for part in path.relative_to(self.root).parts):
                continue
            if path.is_file():
                yield path

    def _parser_for(
        self, path: Path
    ) -> Callable[[Path, str], list[Dependency]] | None:
        name = path.name
        if name == "package.xml":
            return self._parse_package_xml
        if name.startswith("requirements") and path.suffix == ".txt":
            return self._parse_requirements
        if name == "pyproject.toml":
            return self._parse_pyproject
        if name == "CMakeLists.txt":
            return self._parse_cmake
        if name == ".gitmodules":
            return self._parse_gitmodules
        if name.lower().startswith("dockerfile"):
            return self._parse_dockerfile
        if path.suffix == ".repos":
            return self._parse_repos
        return None

    @staticmethod
    def _find_line(text: str, needle: str) -> int | None:
        for number, line in enumerate(text.splitlines(), start=1):
            if needle in line:
                return number
        return None

    def _parse_package_xml(self, path: Path, relative: str) -> list[Dependency]:
        text = path.read_text()
        root = ET.fromstring(text)
        dependencies: list[Dependency] = []
        for tag, dependency_type in PACKAGE_XML_TAGS.items():
            for element in root.findall(tag):
                if not element.text or not element.text.strip():
                    continue
                name = element.text.strip()
                constraints = [
                    f"{attribute.removeprefix('version_')}:{value}"
                    for attribute, value in element.attrib.items()
                    if attribute.startswith("version_")
                ]
                dependencies.append(
                    Dependency(
                        ecosystem=Ecosystem.ROS,
                        name=name,
                        source=SourceLocation(
                            relative, self._find_line(text, f">{name}<")
                        ),
                        constraint=",".join(sorted(constraints)) or None,
                        dependency_type=dependency_type,
                        metadata={"manifest_tag": tag},
                    )
                )
        return dependencies

    def _parse_requirements(self, path: Path, relative: str) -> list[Dependency]:
        dependencies: list[Dependency] = []
        for number, raw_line in enumerate(path.read_text().splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith(("#", "-", "http://", "https://", "git+")):
                continue
            try:
                requirement = Requirement(line)
            except InvalidRequirement:
                self.warnings.append(f"{relative}:{number}: unsupported requirement {line!r}")
                continue
            specifier = str(requirement.specifier) or None
            resolved = None
            if specifier and specifier.startswith("==") and "," not in specifier:
                resolved = specifier.removeprefix("==")
            dependencies.append(
                Dependency(
                    ecosystem=Ecosystem.PYTHON,
                    name=requirement.name,
                    source=SourceLocation(relative, number),
                    constraint=specifier,
                    resolved=resolved,
                    metadata={
                        "extras": sorted(requirement.extras),
                        "marker": str(requirement.marker) if requirement.marker else None,
                    },
                )
            )
        return dependencies

    def _parse_pyproject(self, path: Path, relative: str) -> list[Dependency]:
        data = tomllib.loads(path.read_text())
        project = data.get("project", {})
        groups: list[tuple[str, list[str]]] = [
            ("runtime", project.get("dependencies", [])),
        ]
        for extra, requirements in project.get("optional-dependencies", {}).items():
            groups.append((f"optional:{extra}", requirements))

        dependencies: list[Dependency] = []
        text = path.read_text()
        for dependency_type, requirements in groups:
            for raw_requirement in requirements:
                try:
                    requirement = Requirement(raw_requirement)
                except InvalidRequirement:
                    self.warnings.append(
                        f"{relative}: unsupported requirement {raw_requirement!r}"
                    )
                    continue
                specifier = str(requirement.specifier) or None
                resolved = None
                if specifier and specifier.startswith("==") and "," not in specifier:
                    resolved = specifier.removeprefix("==")
                dependencies.append(
                    Dependency(
                        ecosystem=Ecosystem.PYTHON,
                        name=requirement.name,
                        source=SourceLocation(
                            relative, self._find_line(text, raw_requirement)
                        ),
                        constraint=specifier,
                        resolved=resolved,
                        dependency_type=dependency_type,
                        metadata={"manifest": "pyproject"},
                    )
                )
        return dependencies

    def _parse_cmake(self, path: Path, relative: str) -> list[Dependency]:
        text = path.read_text()
        pattern = re.compile(
            r"find_package\s*\(\s*([A-Za-z0-9_.+-]+)(?:\s+([0-9][A-Za-z0-9_.+-]*))?",
            re.IGNORECASE,
        )
        return [
            Dependency(
                ecosystem=Ecosystem.CMAKE,
                name=match.group(1),
                source=SourceLocation(
                    relative, text[: match.start()].count("\n") + 1
                ),
                constraint=match.group(2),
                metadata={"manifest": "cmake"},
            )
            for match in pattern.finditer(text)
        ]

    def _parse_gitmodules(self, path: Path, relative: str) -> list[Dependency]:
        config = configparser.ConfigParser()
        config.read_string(path.read_text())
        dependencies: list[Dependency] = []
        for section in config.sections():
            if not section.startswith("submodule "):
                continue
            url = config.get(section, "url", fallback="")
            name = section.removeprefix("submodule ").strip('"')
            branch = config.get(section, "branch", fallback=None)
            dependencies.append(
                Dependency(
                    ecosystem=Ecosystem.GIT,
                    name=name,
                    source=SourceLocation(relative),
                    constraint=branch,
                    metadata={
                        "url": url,
                        "path": config.get(section, "path", fallback=name),
                        "manifest": "gitmodules",
                    },
                )
            )
        return dependencies

    def _parse_dockerfile(self, path: Path, relative: str) -> list[Dependency]:
        text = path.read_text()
        logical_lines = re.sub(r"\\\s*\n", " ", text).splitlines()
        dependencies: list[Dependency] = []
        for logical_line in logical_lines:
            stripped = logical_line.strip()
            from_match = re.match(
                r"FROM(?:\s+--platform=\S+)?\s+([^\s]+)", stripped, re.IGNORECASE
            )
            if from_match:
                reference = from_match.group(1)
                image, resolved = self._split_image_reference(reference)
                dependencies.append(
                    Dependency(
                        ecosystem=Ecosystem.DOCKER,
                        name=image,
                        source=SourceLocation(
                            relative, self._find_line(text, reference)
                        ),
                        constraint=resolved,
                        resolved=resolved,
                        metadata={"reference": reference},
                    )
                )

            apt_match = re.search(
                r"\bapt-get\s+install\b(.*?)(?:&&|;|$)", stripped, re.IGNORECASE
            )
            if not apt_match:
                continue
            tokens = re.split(r"\s+", apt_match.group(1).strip())
            for token in tokens:
                if not token or token.startswith("-") or token in {"\\", "sudo"}:
                    continue
                name, separator, version = token.partition("=")
                if not re.match(r"^[A-Za-z0-9][A-Za-z0-9+.-]*(?::[A-Za-z0-9_-]+)?$", name):
                    continue
                dependencies.append(
                    Dependency(
                        ecosystem=Ecosystem.APT,
                        name=name,
                        source=SourceLocation(
                            relative, self._find_line(text, token)
                        ),
                        constraint=f"=={version}" if separator else None,
                        resolved=version or None,
                        metadata={"manifest": "dockerfile"},
                    )
                )
        return dependencies

    @staticmethod
    def _split_image_reference(reference: str) -> tuple[str, str | None]:
        if "@" in reference:
            image, digest = reference.split("@", maxsplit=1)
            return image, digest
        final_segment = reference.rsplit("/", maxsplit=1)[-1]
        if ":" in final_segment:
            image, tag = reference.rsplit(":", maxsplit=1)
            return image, tag
        return reference, None

    def _parse_repos(self, path: Path, relative: str) -> list[Dependency]:
        data = yaml.safe_load(path.read_text()) or {}
        repositories = data.get("repositories", {})
        dependencies: list[Dependency] = []
        for name, details in repositories.items():
            if not isinstance(details, dict):
                continue
            version = details.get("version")
            dependencies.append(
                Dependency(
                    ecosystem=Ecosystem.GIT,
                    name=str(name),
                    source=SourceLocation(relative),
                    constraint=str(version) if version is not None else None,
                    resolved=str(version) if version is not None else None,
                    metadata={
                        "url": details.get("url"),
                        "vcs_type": details.get("type"),
                        "manifest": "vcstool",
                    },
                )
            )
        return dependencies
