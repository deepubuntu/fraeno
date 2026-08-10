from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx
import yaml
from packaging.version import InvalidVersion, Version

from fraeno.dependency_graph import Provenance, TargetPlatform, infer_target_platform
from fraeno.models import Dependency, Ecosystem, ScanReport


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class CatalogRecord:
    current: str
    target: str
    source: str
    release_date: str
    evidence: str
    metadata: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True)
class CatalogQuery:
    provider: str
    dependency: Dependency
    target: TargetPlatform


class UpdateCatalog(Protocol):
    def lookup(self, query: CatalogQuery) -> Sequence[CatalogRecord]: ...


@dataclass(frozen=True)
class UpdateCandidate:
    ecosystem: Ecosystem
    name: str
    current: str
    target: str
    source: str
    release_date: str
    provenance: tuple[Provenance, ...]
    source_files: tuple[str, ...]
    metadata: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def identity(self) -> str:
        return f"{self.ecosystem.value}:{self.name}"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["ecosystem"] = self.ecosystem.value
        value["provenance"] = [item.to_dict() for item in self.provenance]
        return value


@dataclass(frozen=True)
class UpdateRefusal:
    ecosystem: Ecosystem
    name: str
    current_values: tuple[str, ...]
    source_files: tuple[str, ...]
    reason: str
    provider: str

    @property
    def identity(self) -> str:
        return f"{self.ecosystem.value}:{self.name}"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["ecosystem"] = self.ecosystem.value
        return value


@dataclass(frozen=True)
class ProviderDiscovery:
    candidates: tuple[UpdateCandidate, ...] = ()
    refusals: tuple[UpdateRefusal, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class UpdateDiscoveryReport:
    candidates: tuple[UpdateCandidate, ...]
    refusals: tuple[UpdateRefusal, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "updates": [item.to_dict() for item in self.candidates],
            "refusals": [item.to_dict() for item in self.refusals],
            "warnings": list(self.warnings),
        }


class UpdateDiscoveryProvider(Protocol):
    name: str

    def supports(self, dependency: Dependency) -> bool: ...

    def discover(
        self,
        dependencies: Sequence[Dependency],
        target: TargetPlatform,
        catalog: UpdateCatalog,
    ) -> ProviderDiscovery: ...


class _CatalogProvider:
    name = ""
    ecosystem = Ecosystem.PYTHON

    def supports(self, dependency: Dependency) -> bool:
        return dependency.ecosystem is self.ecosystem

    def discover(
        self,
        dependencies: Sequence[Dependency],
        target: TargetPlatform,
        catalog: UpdateCatalog,
    ) -> ProviderDiscovery:
        refusal = self._validate(dependencies)
        if refusal is not None:
            return ProviderDiscovery(refusals=(refusal,))
        target_refusal = self._validate_target(dependencies, target)
        if target_refusal is not None:
            return ProviderDiscovery(refusals=(target_refusal,))

        dependency = dependencies[0]
        try:
            records = tuple(catalog.lookup(CatalogQuery(self.name, dependency, target)))
        except (CatalogError, httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            return ProviderDiscovery(warnings=(f"{self.name}:{dependency.name}: {error}",))
        if not records:
            return self._empty_result(dependencies)

        distinct = {
            (
                record.current,
                record.target,
            )
            for record in records
        }
        if len(distinct) != 1:
            targets = ", ".join(sorted({record.target for record in records}))
            return ProviderDiscovery(
                refusals=(
                    self._refusal(
                        dependencies,
                        f"the catalog returned ambiguous targets: {targets}",
                    ),
                )
            )

        record = records[0]
        declared = self._declared_current(dependencies)
        if declared is not None and record.current != declared:
            return ProviderDiscovery(
                refusals=(
                    self._refusal(
                        dependencies,
                        "the catalog current value does not match the repository "
                        f"({record.current!r} != {declared!r})",
                    ),
                )
            )
        if record.target == record.current:
            return ProviderDiscovery()
        reason = self._validate_record(record)
        if reason is not None:
            return ProviderDiscovery(refusals=(self._refusal(dependencies, reason),))

        provenance = Provenance(
            provider=self.name,
            source=record.source,
            evidence=record.evidence,
            metadata=record.metadata,
        )
        return ProviderDiscovery(
            candidates=(
                UpdateCandidate(
                    ecosystem=dependency.ecosystem,
                    name=dependency.name,
                    current=record.current,
                    target=record.target,
                    source=record.source,
                    release_date=record.release_date,
                    provenance=(provenance,),
                    source_files=_source_files(dependencies),
                    metadata=record.metadata,
                ),
            )
        )

    def _validate(self, dependencies: Sequence[Dependency]) -> UpdateRefusal | None:
        values = _current_values(dependencies)
        if len(values) > 1:
            return self._refusal(
                dependencies,
                "repository declarations resolve to different current values",
            )
        if not values:
            return self._refusal(
                dependencies,
                "the repository does not declare one exact current value",
            )
        return None

    def _declared_current(self, dependencies: Sequence[Dependency]) -> str | None:
        values = _current_values(dependencies)
        return values[0] if len(values) == 1 else None

    def _validate_record(self, record: CatalogRecord) -> str | None:
        del record
        return None

    def _validate_target(
        self,
        dependencies: Sequence[Dependency],
        target: TargetPlatform,
    ) -> UpdateRefusal | None:
        del dependencies, target
        return None

    def _empty_result(self, dependencies: Sequence[Dependency]) -> ProviderDiscovery:
        del dependencies
        return ProviderDiscovery()

    def _refusal(
        self,
        dependencies: Sequence[Dependency],
        reason: str,
    ) -> UpdateRefusal:
        dependency = dependencies[0]
        return UpdateRefusal(
            ecosystem=dependency.ecosystem,
            name=dependency.name,
            current_values=_current_values(dependencies),
            source_files=_source_files(dependencies),
            reason=reason,
            provider=self.name,
        )


class PythonUpdateProvider(_CatalogProvider):
    name = "pypi"
    ecosystem = Ecosystem.PYTHON

    def _validate_record(self, record: CatalogRecord) -> str | None:
        try:
            current = Version(record.current)
            target = Version(record.target)
        except InvalidVersion:
            return "PyPI returned a version Fraeno cannot compare safely"
        if target.is_prerelease:
            return "pre-release Python versions are not proposed automatically"
        if target <= current:
            return "the PyPI target is not newer than the current version"
        return None


class DockerUpdateProvider(_CatalogProvider):
    name = "docker"
    ecosystem = Ecosystem.DOCKER

    def _validate_target(
        self,
        dependencies: Sequence[Dependency],
        target: TargetPlatform,
    ) -> UpdateRefusal | None:
        if target.architecture == "unknown":
            return self._refusal(
                dependencies,
                "the target architecture is unknown, so a Docker digest is ambiguous",
            )
        return None

    def _validate_record(self, record: CatalogRecord) -> str | None:
        if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", record.target):
            return None
        try:
            current = Version(record.current.removeprefix("v"))
            target = Version(record.target.removeprefix("v"))
        except InvalidVersion:
            return (
                "Docker tags are not ordered versions and the registry did not "
                "return an immutable digest"
            )
        if target.is_prerelease or target <= current:
            return "the Docker target is not a newer stable version"
        return None


class AptUpdateProvider(_CatalogProvider):
    name = "apt"
    ecosystem = Ecosystem.APT

    def _validate_target(
        self,
        dependencies: Sequence[Dependency],
        target: TargetPlatform,
    ) -> UpdateRefusal | None:
        if (
            target.operating_system == "unknown"
            or target.operating_system_version == "unknown"
            or target.architecture == "unknown"
        ):
            return self._refusal(
                dependencies,
                "the operating system, release, and architecture must be known "
                "before comparing APT packages",
            )
        return None

    def _validate_record(self, record: CatalogRecord) -> str | None:
        if _compare_debian_versions(record.target, record.current) <= 0:
            return "the APT target is not newer than the current package version"
        return None


class VcstoolUpdateProvider(_CatalogProvider):
    name = "vcstool"
    ecosystem = Ecosystem.GIT

    def supports(self, dependency: Dependency) -> bool:
        return super().supports(dependency) and dependency.metadata.get("manifest") == "vcstool"

    def _validate_record(self, record: CatalogRecord) -> str | None:
        if re.fullmatch(r"[0-9a-fA-F]{40}", record.target) is None:
            return "vcstool targets must resolve to a full immutable commit SHA"
        return None


class RosdepUpdateProvider(_CatalogProvider):
    name = "rosdep"
    ecosystem = Ecosystem.ROS

    def _validate_target(
        self,
        dependencies: Sequence[Dependency],
        target: TargetPlatform,
    ) -> UpdateRefusal | None:
        if "unknown" in target.to_dict().values():
            return self._refusal(
                dependencies,
                "the ROS distribution, operating system, release, and architecture "
                "must be known before resolving rosdep",
            )
        return None

    def _validate(self, dependencies: Sequence[Dependency]) -> UpdateRefusal | None:
        values = _current_values(dependencies)
        if len(values) > 1:
            return self._refusal(
                dependencies,
                "rosdep resolutions disagree about the current system package",
            )
        return None

    def _empty_result(self, dependencies: Sequence[Dependency]) -> ProviderDiscovery:
        return ProviderDiscovery(
            refusals=(
                self._refusal(
                    dependencies,
                    "rosdep did not return both the currently resolved package and "
                    "a newer package for this target",
                ),
            )
        )

    def _declared_current(self, dependencies: Sequence[Dependency]) -> str | None:
        del dependencies
        return None


DEFAULT_PROVIDERS: tuple[UpdateDiscoveryProvider, ...] = (
    PythonUpdateProvider(),
    DockerUpdateProvider(),
    AptUpdateProvider(),
    VcstoolUpdateProvider(),
    RosdepUpdateProvider(),
)


def discover_updates(
    report: ScanReport,
    *,
    target: TargetPlatform | None = None,
    catalog: UpdateCatalog | None = None,
    providers: Sequence[UpdateDiscoveryProvider] = DEFAULT_PROVIDERS,
) -> UpdateDiscoveryReport:
    active_target = target or infer_target_platform(report)
    own_catalog = catalog is None
    active_catalog = catalog or RegistryUpdateCatalog()
    candidates: list[UpdateCandidate] = []
    refusals: list[UpdateRefusal] = []
    warnings: list[str] = []
    try:
        for provider in providers:
            groups: dict[str, list[Dependency]] = {}
            for dependency in report.dependencies:
                if provider.supports(dependency):
                    groups.setdefault(dependency.name.lower(), []).append(dependency)
            for name in sorted(groups):
                discovery = provider.discover(groups[name], active_target, active_catalog)
                candidates.extend(discovery.candidates)
                refusals.extend(discovery.refusals)
                warnings.extend(discovery.warnings)
    finally:
        if own_catalog and isinstance(active_catalog, RegistryUpdateCatalog):
            active_catalog.close()

    candidates.sort(key=lambda item: (item.ecosystem.value, item.name.lower()))
    refusals.sort(key=lambda item: (item.ecosystem.value, item.name.lower()))
    return UpdateDiscoveryReport(
        candidates=tuple(candidates),
        refusals=tuple(refusals),
        warnings=tuple(sorted(warnings)),
    )


class FixtureUpdateCatalog:
    def __init__(self, records: Sequence[dict[str, Any]]) -> None:
        self._records = tuple(records)

    @classmethod
    def from_path(cls, path: Path) -> FixtureUpdateCatalog:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict) or not isinstance(raw.get("records"), list):
            raise CatalogError("fixture catalog must contain a records list")
        records = raw["records"]
        if not all(isinstance(item, dict) for item in records):
            raise CatalogError("every fixture catalog record must be an object")
        return cls(records)

    def lookup(self, query: CatalogQuery) -> Sequence[CatalogRecord]:
        matches: list[CatalogRecord] = []
        for raw in self._records:
            if raw.get("provider") != query.provider:
                continue
            if str(raw.get("name", "")).lower() != query.dependency.name.lower():
                continue
            target_selector = raw.get("target_platform", {})
            if not isinstance(target_selector, dict):
                raise CatalogError("target_platform must be an object")
            if not _target_matches(target_selector, query.target):
                continue
            matches.append(_record_from_mapping(raw))
        return matches


class RegistryUpdateCatalog:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=15,
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )
        self._rosdistro_cache: dict[str, dict[str, tuple[str, str]]] = {}
        self._rosdep_cache: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def lookup(self, query: CatalogQuery) -> Sequence[CatalogRecord]:
        handlers = {
            "pypi": self._pypi,
            "docker": self._docker,
            "apt": self._apt,
            "vcstool": self._vcstool,
            "rosdep": self._rosdep,
        }
        handler = handlers.get(query.provider)
        if handler is None:
            raise CatalogError(f"unsupported registry provider: {query.provider}")
        return handler(query)

    def _pypi(self, query: CatalogQuery) -> Sequence[CatalogRecord]:
        response = self.client.get(f"https://pypi.org/pypi/{quote(query.dependency.name)}/json")
        response.raise_for_status()
        data = response.json()
        target = str(data["info"]["version"])
        current = query.dependency.resolved or ""
        releases = data.get("releases", {}).get(target, [])
        release_date = "unknown"
        if releases:
            release_date = str(releases[0].get("upload_time_iso_8601") or "unknown")
        return (
            CatalogRecord(
                current=current,
                target=target,
                source=str(response.url),
                release_date=release_date,
                evidence="PyPI identified the latest published project release.",
                metadata={"index": "pypi"},
            ),
        )

    def _docker(self, query: CatalogQuery) -> Sequence[CatalogRecord]:
        current = query.dependency.resolved or ""
        if not current or current.startswith("sha256:"):
            return ()
        registry, remainder = _split_image_reference(query.dependency.name)
        if registry == "ghcr.io":
            return self._ghcr(query, remainder, current)
        namespace, repository = _docker_hub_name(query.dependency.name)
        response = self.client.get(
            "https://hub.docker.com/v2/namespaces/"
            f"{quote(namespace)}/repositories/{quote(repository)}/tags/{quote(current)}"
        )
        response.raise_for_status()
        data = response.json()
        digest = _docker_digest(data, query.target.architecture)
        if digest is None:
            raise CatalogError("Docker Hub did not return one digest for the target architecture")
        return (
            CatalogRecord(
                current=current,
                target=digest,
                source=str(response.url),
                release_date=str(data.get("last_updated") or "unknown"),
                evidence=(
                    "Docker Hub resolved the mutable image tag to the published "
                    "target-architecture digest."
                ),
                metadata={
                    "registry": "docker-hub",
                    "tag": current,
                    "architecture": query.target.architecture,
                },
            ),
        )

    def _apt(self, query: CatalogQuery) -> Sequence[CatalogRecord]:
        current = query.dependency.resolved or ""
        series = _ubuntu_series(query.target.operating_system_version)
        if query.target.operating_system != "ubuntu" or series is None:
            return ()
        architecture = query.target.architecture
        if architecture == "unknown":
            return ()
        response = self.client.get(
            "https://api.launchpad.net/devel/ubuntu/+archive/primary",
            params={
                "ws.op": "getPublishedBinaries",
                "binary_name": query.dependency.name.split(":", maxsplit=1)[0],
                "distro_arch_series": f"/ubuntu/{series}/{architecture}",
                "exact_match": "true",
                "status": "Published",
                "order_by_date": "true",
                "ws.size": "1",
            },
        )
        response.raise_for_status()
        entries = response.json().get("entries", [])
        if not entries:
            return ()
        entry = entries[0]
        target = str(entry["binary_package_version"])
        return (
            CatalogRecord(
                current=current,
                target=target,
                source=str(entry.get("self_link") or response.url),
                release_date=str(entry.get("date_published") or "unknown"),
                evidence=(
                    "Launchpad returned the newest published binary for the exact "
                    "Ubuntu series and architecture."
                ),
                metadata={
                    "series": series,
                    "architecture": architecture,
                    "archive": "ubuntu-primary",
                },
            ),
        )

    def _vcstool(self, query: CatalogQuery) -> Sequence[CatalogRecord]:
        current = query.dependency.resolved or ""
        repository = _github_repository(query.dependency.metadata.get("url"))
        if repository is None:
            return ()
        repository_response = self.client.get(f"https://api.github.com/repos/{repository}")
        repository_response.raise_for_status()
        default_branch = str(repository_response.json()["default_branch"])
        commit_response = self.client.get(
            f"https://api.github.com/repos/{repository}/commits/{quote(default_branch)}"
        )
        commit_response.raise_for_status()
        commit = commit_response.json()
        target = str(commit["sha"])
        if current == target:
            return ()
        if re.fullmatch(r"[0-9a-fA-F]{40}", current):
            comparison = self.client.get(
                f"https://api.github.com/repos/{repository}/compare/"
                f"{quote(current)}...{quote(target)}"
            )
            comparison.raise_for_status()
            if comparison.json().get("status") != "ahead":
                raise CatalogError(
                    "the default branch is not a verified descendant of the current SHA"
                )
        elif current != default_branch:
            raise CatalogError("the vcstool ref is neither a commit SHA nor the default branch")
        commit_date = commit.get("commit", {}).get("committer", {}).get("date")
        return (
            CatalogRecord(
                current=current,
                target=target,
                source=str(commit_response.url),
                release_date=str(commit_date or "unknown"),
                evidence=(
                    "GitHub resolved the repository default branch to an immutable "
                    "descendant commit."
                ),
                metadata={
                    "repository": repository,
                    "default_branch": default_branch,
                },
            ),
        )

    def _ghcr(
        self,
        query: CatalogQuery,
        repository: str,
        tag: str,
    ) -> Sequence[CatalogRecord]:
        token_response = self.client.get(
            "https://ghcr.io/token",
            params={"scope": f"repository:{repository}:pull"},
        )
        token_response.raise_for_status()
        token = str(token_response.json().get("token") or "")
        if not token:
            raise CatalogError("GHCR did not issue an anonymous pull token")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": ", ".join(_OCI_MANIFEST_TYPES),
        }
        manifest_response = self.client.get(
            f"https://ghcr.io/v2/{repository}/manifests/{quote(tag)}",
            headers=headers,
        )
        manifest_response.raise_for_status()
        manifest = manifest_response.json()
        architecture = query.target.architecture
        if str(manifest.get("mediaType") or "") in _OCI_INDEX_TYPES:
            digest = _oci_platform_digest(manifest, architecture)
            if digest is None:
                raise CatalogError("GHCR did not return one digest for the target architecture")
        else:
            digest = str(manifest_response.headers.get("docker-content-digest") or "")
            if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest) is None:
                raise CatalogError("GHCR did not return a content digest for the image tag")
            built_for = self._ghcr_config_architecture(repository, manifest, headers)
            if architecture != "unknown" and built_for != architecture:
                raise CatalogError(
                    f"the GHCR image is built for {built_for}, not {architecture}"
                )
        created = _oci_created_annotation(manifest)
        return (
            CatalogRecord(
                current=tag,
                target=digest,
                source=str(manifest_response.url),
                release_date=created,
                evidence=(
                    "GHCR resolved the mutable image tag to the published "
                    "target-architecture digest."
                ),
                metadata={
                    "registry": "ghcr",
                    "tag": tag,
                    "architecture": architecture,
                },
            ),
        )

    def _ghcr_config_architecture(
        self,
        repository: str,
        manifest: dict[str, Any],
        headers: dict[str, str],
    ) -> str:
        config = manifest.get("config")
        if not isinstance(config, dict) or not config.get("digest"):
            raise CatalogError("the GHCR image manifest does not name a config blob")
        response = self.client.get(
            f"https://ghcr.io/v2/{repository}/blobs/{quote(str(config['digest']))}",
            headers=headers,
        )
        response.raise_for_status()
        return str(response.json().get("architecture") or "unknown")

    def _rosdep(self, query: CatalogQuery) -> Sequence[CatalogRecord]:
        series = _ubuntu_series(query.target.operating_system_version)
        if query.target.operating_system != "ubuntu" or series is None:
            return ()
        distribution = query.target.ros_distribution
        key = query.dependency.name
        current = query.dependency.resolved or ""
        released = self._rosdistro_release_versions(distribution).get(key)
        if released is not None:
            version, repository = released
            return (
                CatalogRecord(
                    current=current or "unreleased",
                    target=version,
                    source=f"{_ROSDISTRO_ROOT}/{distribution}/distribution.yaml",
                    release_date="unknown",
                    evidence=(
                        "The rosdistro index lists the newest version released "
                        "into the target ROS distribution."
                    ),
                    metadata={
                        "distribution": distribution,
                        "repository": repository,
                        "apt_package": f"ros-{distribution}-{key.replace('_', '-')}",
                    },
                ),
            )
        packages = self._rosdep_system_packages(key, series)
        if packages is None:
            raise CatalogError("the rosdep key is not in the rosdistro database")
        if len(packages) != 1:
            raise CatalogError(
                f"the rosdep key resolves to {len(packages)} APT packages, "
                "so one update target is ambiguous"
            )
        return self._rosdep_apt_record(query, key, packages[0], series)

    def _rosdep_apt_record(
        self,
        query: CatalogQuery,
        key: str,
        package: str,
        series: str,
    ) -> Sequence[CatalogRecord]:
        architecture = query.target.architecture
        response = self.client.get(
            "https://api.launchpad.net/devel/ubuntu/+archive/primary",
            params={
                "ws.op": "getPublishedBinaries",
                "binary_name": package,
                "distro_arch_series": f"/ubuntu/{series}/{architecture}",
                "exact_match": "true",
                "status": "Published",
                "order_by_date": "true",
                "ws.size": "1",
            },
        )
        response.raise_for_status()
        entries = response.json().get("entries", [])
        if not entries:
            return ()
        entry = entries[0]
        return (
            CatalogRecord(
                current=query.dependency.resolved or "unresolved",
                target=str(entry["binary_package_version"]),
                source=str(entry.get("self_link") or response.url),
                release_date=str(entry.get("date_published") or "unknown"),
                evidence=(
                    "The rosdistro database mapped the rosdep key to one Ubuntu "
                    "package and Launchpad returned its newest published binary."
                ),
                metadata={
                    "rosdep_key": key,
                    "apt_package": package,
                    "series": series,
                    "architecture": architecture,
                },
            ),
        )

    def _rosdistro_release_versions(
        self,
        distribution: str,
    ) -> dict[str, tuple[str, str]]:
        cached = self._rosdistro_cache.get(distribution)
        if cached is not None:
            return cached
        document = self._fetch_yaml(f"{_ROSDISTRO_ROOT}/{distribution}/distribution.yaml")
        versions: dict[str, tuple[str, str]] = {}
        repositories = document.get("repositories")
        if isinstance(repositories, dict):
            for repository_name, repository in repositories.items():
                if not isinstance(repository, dict):
                    continue
                release = repository.get("release")
                if not isinstance(release, dict) or not release.get("version"):
                    continue
                version = str(release["version"])
                packages = release.get("packages")
                names = (
                    [str(name) for name in packages]
                    if isinstance(packages, list)
                    else [str(repository_name)]
                )
                for name in names:
                    versions[name] = (version, str(repository_name))
        self._rosdistro_cache[distribution] = versions
        return versions

    def _rosdep_system_packages(self, key: str, series: str) -> list[str] | None:
        for path in ("rosdep/base.yaml", "rosdep/python.yaml"):
            document = self._rosdep_document(path)
            entry = document.get(key)
            if entry is None:
                continue
            packages = _rosdep_ubuntu_packages(entry, series)
            if packages is not None:
                return packages
        return None

    def _rosdep_document(self, path: str) -> dict[str, Any]:
        cached = self._rosdep_cache.get(path)
        if cached is not None:
            return cached
        document = self._fetch_yaml(f"{_ROSDISTRO_ROOT}/{path}")
        self._rosdep_cache[path] = document
        return document

    def _fetch_yaml(self, url: str) -> dict[str, Any]:
        response = self.client.get(url)
        response.raise_for_status()
        document = yaml.safe_load(response.text)
        if not isinstance(document, dict):
            raise CatalogError(f"the rosdistro document is not a mapping: {url}")
        return document


def _record_from_mapping(raw: dict[str, Any]) -> CatalogRecord:
    required = ("current", "target", "source", "release_date", "evidence")
    missing = [name for name in required if not raw.get(name)]
    if missing:
        raise CatalogError("fixture catalog record is missing " + ", ".join(sorted(missing)))
    metadata = raw.get("metadata", {})
    if not isinstance(metadata, dict):
        raise CatalogError("fixture catalog metadata must be an object")
    return CatalogRecord(
        current=str(raw["current"]),
        target=str(raw["target"]),
        source=str(raw["source"]),
        release_date=str(raw["release_date"]),
        evidence=str(raw["evidence"]),
        metadata=metadata,
    )


def _target_matches(selector: dict[str, Any], target: TargetPlatform) -> bool:
    values = target.to_dict()
    return all(values.get(key) == value for key, value in selector.items())


def _current_values(dependencies: Sequence[Dependency]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                value
                for dependency in dependencies
                if (value := dependency.resolved or dependency.constraint)
            }
        )
    )


def _source_files(dependencies: Sequence[Dependency]) -> tuple[str, ...]:
    return tuple(sorted({item.source.path for item in dependencies}))


_ROSDISTRO_ROOT = "https://raw.githubusercontent.com/ros/rosdistro/master"

_OCI_INDEX_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)

_OCI_MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)


def _split_image_reference(image: str) -> tuple[str | None, str]:
    parts = image.split("/")
    if "." in parts[0] or ":" in parts[0] or parts[0] == "localhost":
        return parts[0], "/".join(parts[1:])
    return None, image


def _docker_hub_name(image: str) -> tuple[str, str]:
    registry, remainder = _split_image_reference(image)
    if registry is not None and registry not in {"docker.io", "index.docker.io"}:
        raise CatalogError(
            "only public Docker Hub and GHCR images are supported automatically"
        )
    parts = remainder.split("/")
    if len(parts) == 1:
        return "library", parts[0]
    return parts[0], "/".join(parts[1:])


def _oci_platform_digest(manifest: dict[str, Any], architecture: str) -> str | None:
    manifests = manifest.get("manifests", [])
    if not isinstance(manifests, list):
        return None
    matches = [
        str(entry["digest"])
        for entry in manifests
        if isinstance(entry, dict)
        and entry.get("digest")
        and isinstance(entry.get("platform"), dict)
        and entry["platform"].get("os") == "linux"
        and (
            architecture == "unknown"
            or entry["platform"].get("architecture") == architecture
        )
    ]
    unique = sorted(set(matches))
    return unique[0] if len(unique) == 1 else None


def _oci_created_annotation(manifest: dict[str, Any]) -> str:
    annotations = manifest.get("annotations")
    if isinstance(annotations, dict):
        created = annotations.get("org.opencontainers.image.created")
        if created:
            return str(created)
    return "unknown"


def _rosdep_ubuntu_packages(entry: Any, series: str) -> list[str] | None:
    if not isinstance(entry, dict):
        return None
    ubuntu = entry.get("ubuntu")
    if ubuntu is None:
        return None
    if isinstance(ubuntu, list):
        return [str(package) for package in ubuntu]
    if isinstance(ubuntu, dict):
        if series in ubuntu:
            return _rosdep_manager_packages(ubuntu[series])
        if "*" in ubuntu:
            return _rosdep_manager_packages(ubuntu["*"])
        if "apt" in ubuntu or "pip" in ubuntu:
            return _rosdep_manager_packages(ubuntu)
    return None


def _rosdep_manager_packages(entry: Any) -> list[str] | None:
    if entry is None:
        return None
    if isinstance(entry, list):
        return [str(package) for package in entry]
    if isinstance(entry, dict):
        manager = entry.get("apt", entry.get("pip"))
        if isinstance(manager, list):
            return [str(package) for package in manager]
        if isinstance(manager, dict) and isinstance(manager.get("packages"), list):
            return [str(package) for package in manager["packages"]]
    return None


def _docker_digest(data: dict[str, Any], architecture: str) -> str | None:
    images = data.get("images", [])
    if not isinstance(images, list):
        return None
    matches = [
        str(image["digest"])
        for image in images
        if isinstance(image, dict)
        and image.get("digest")
        and (architecture == "unknown" or image.get("architecture") == architecture)
    ]
    unique = sorted(set(matches))
    if len(unique) == 1:
        return unique[0]
    digest = data.get("digest")
    return str(digest) if digest and architecture == "unknown" else None


def _ubuntu_series(version: str) -> str | None:
    return {
        "20.04": "focal",
        "22.04": "jammy",
        "24.04": "noble",
    }.get(version)


def _github_repository(raw_url: Any) -> str | None:
    if not isinstance(raw_url, str):
        return None
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:)"
        r"(?P<repository>[^/]+/[^/]+?)(?:\.git)?",
        raw_url,
    )
    return match.group("repository") if match else None


def _compare_debian_versions(left: str, right: str) -> int:
    left_epoch, left_upstream, left_revision = _debian_version_parts(left)
    right_epoch, right_upstream, right_revision = _debian_version_parts(right)
    if left_epoch != right_epoch:
        return 1 if left_epoch > right_epoch else -1
    upstream = _compare_debian_part(left_upstream, right_upstream)
    if upstream:
        return upstream
    return _compare_debian_part(left_revision, right_revision)


def _debian_version_parts(value: str) -> tuple[int, str, str]:
    epoch_text, separator, remainder = value.partition(":")
    if separator and epoch_text.isdigit():
        epoch = int(epoch_text)
    else:
        epoch = 0
        remainder = value
    upstream, separator, revision = remainder.rpartition("-")
    if not separator:
        return epoch, remainder, "0"
    return epoch, upstream, revision


def _compare_debian_part(left: str, right: str) -> int:
    left_index = 0
    right_index = 0
    while left_index < len(left) or right_index < len(right):
        while (left_index < len(left) and not left[left_index].isdigit()) or (
            right_index < len(right) and not right[right_index].isdigit()
        ):
            left_order = _debian_character_order(
                left[left_index] if left_index < len(left) else None
            )
            right_order = _debian_character_order(
                right[right_index] if right_index < len(right) else None
            )
            if left_order != right_order:
                return 1 if left_order > right_order else -1
            if left_index < len(left):
                left_index += 1
            if right_index < len(right):
                right_index += 1

        left_zero = left_index
        right_zero = right_index
        while left_zero < len(left) and left[left_zero] == "0":
            left_zero += 1
        while right_zero < len(right) and right[right_zero] == "0":
            right_zero += 1
        left_end = left_zero
        right_end = right_zero
        while left_end < len(left) and left[left_end].isdigit():
            left_end += 1
        while right_end < len(right) and right[right_end].isdigit():
            right_end += 1
        left_digits = left[left_zero:left_end]
        right_digits = right[right_zero:right_end]
        if len(left_digits) != len(right_digits):
            return 1 if len(left_digits) > len(right_digits) else -1
        if left_digits != right_digits:
            return 1 if left_digits > right_digits else -1
        left_index = left_end
        right_index = right_end
    return 0


def _debian_character_order(character: str | None) -> int:
    if character == "~":
        return -1
    if character is None:
        return 0
    if character.isalpha():
        return ord(character)
    return ord(character) + 256
