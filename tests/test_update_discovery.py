from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx

from fraeno.cli import main
from fraeno.dependency_graph import TargetPlatform
from fraeno.models import Dependency, Ecosystem, ScanReport, SourceLocation
from fraeno.scanner import RepositoryScanner
from fraeno.update_discovery import (
    AptUpdateProvider,
    CatalogRecord,
    DockerUpdateProvider,
    FixtureUpdateCatalog,
    PythonUpdateProvider,
    RegistryUpdateCatalog,
    RosdepUpdateProvider,
    discover_updates,
)
from fraeno.updates import apply_next_update

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "update_discovery"
TARGET = TargetPlatform(
    ros_distribution="humble",
    operating_system="ubuntu",
    operating_system_version="22.04",
    architecture="amd64",
)


def test_discovers_all_supported_sources_with_complete_evidence() -> None:
    repository = FIXTURE_ROOT / "robot_repo"
    report = RepositoryScanner(repository).scan()
    catalog = FixtureUpdateCatalog.from_path(FIXTURE_ROOT / "catalog.json")

    discovery = discover_updates(report, target=TARGET, catalog=catalog)

    assert [candidate.identity for candidate in discovery.candidates] == [
        "apt:curl",
        "docker:ros",
        "git:navigation",
        "python:requests",
        "ros:rclcpp",
    ]
    assert discovery.refusals == ()
    assert discovery.warnings == ()
    for candidate in discovery.candidates:
        assert candidate.current
        assert candidate.target
        assert candidate.source.startswith("https://")
        assert candidate.release_date.endswith("Z")
        assert candidate.provenance[0].provider
        assert candidate.provenance[0].evidence
        assert candidate.source_files


def test_refuses_conflicting_current_versions_before_catalog_lookup() -> None:
    report = ScanReport(
        root="/fixture",
        dependencies=[
            Dependency(
                ecosystem=Ecosystem.PYTHON,
                name="requests",
                resolved="2.31.0",
                constraint="==2.31.0",
                source=SourceLocation("requirements.txt", 1),
            ),
            Dependency(
                ecosystem=Ecosystem.PYTHON,
                name="requests",
                resolved="2.32.0",
                constraint="==2.32.0",
                source=SourceLocation("tools/requirements.txt", 1),
            ),
        ],
        files_scanned=["requirements.txt", "tools/requirements.txt"],
    )

    discovery = discover_updates(
        report,
        target=TARGET,
        catalog=FixtureUpdateCatalog([]),
        providers=(PythonUpdateProvider(),),
    )

    assert discovery.candidates == ()
    assert discovery.refusals[0].identity == "python:requests"
    assert "different current values" in discovery.refusals[0].reason
    assert discovery.refusals[0].current_values == ("2.31.0", "2.32.0")


def test_refuses_ambiguous_catalog_targets() -> None:
    report = ScanReport(
        root="/fixture",
        dependencies=[
            Dependency(
                ecosystem=Ecosystem.APT,
                name="curl",
                resolved="1.0",
                constraint="==1.0",
                source=SourceLocation("Dockerfile", 2),
            )
        ],
        files_scanned=["Dockerfile"],
    )

    class AmbiguousCatalog:
        def lookup(self, query: object) -> tuple[CatalogRecord, ...]:
            del query
            return (
                CatalogRecord(
                    current="1.0",
                    target="1.1",
                    source="https://packages.example/1.1",
                    release_date="2026-07-01T00:00:00Z",
                    evidence="first repository",
                ),
                CatalogRecord(
                    current="1.0",
                    target="2.0",
                    source="https://packages.example/2.0",
                    release_date="2026-07-02T00:00:00Z",
                    evidence="second repository",
                ),
            )

    discovery = discover_updates(
        report,
        target=TARGET,
        catalog=AmbiguousCatalog(),
        providers=(AptUpdateProvider(),),
    )

    assert discovery.candidates == ()
    assert "ambiguous targets" in discovery.refusals[0].reason
    assert "1.1" in discovery.refusals[0].reason
    assert "2.0" in discovery.refusals[0].reason


def test_refuses_unpinned_apt_dependency() -> None:
    report = ScanReport(
        root="/fixture",
        dependencies=[
            Dependency(
                ecosystem=Ecosystem.APT,
                name="curl",
                source=SourceLocation("Dockerfile", 2),
            )
        ],
        files_scanned=["Dockerfile"],
    )

    discovery = discover_updates(
        report,
        target=TARGET,
        catalog=FixtureUpdateCatalog([]),
        providers=(AptUpdateProvider(),),
    )

    assert discovery.candidates == ()
    assert discovery.refusals[0].reason == (
        "the repository does not declare one exact current value"
    )


def test_applies_one_fixture_update_without_network(tmp_path: Path) -> None:
    repository = tmp_path / "robot_repo"
    shutil.copytree(FIXTURE_ROOT / "robot_repo", repository)
    report = RepositoryScanner(repository).scan()
    catalog = FixtureUpdateCatalog.from_path(FIXTURE_ROOT / "catalog.json")

    result, discovery = apply_next_update(
        repository,
        report,
        target=TARGET,
        catalog=catalog,
    )

    assert result is not None
    assert result.dependency == "apt:curl"
    assert result.changed_files == ("Dockerfile",)
    assert "curl=7.81.0-1ubuntu1.21" in (repository / "Dockerfile").read_text()
    assert len(discovery.candidates) == 5


def test_outdated_cli_uses_fixture_catalog_without_network(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outdated.json"

    result = main(
        [
            "outdated",
            str(FIXTURE_ROOT / "robot_repo"),
            "--catalog",
            str(FIXTURE_ROOT / "catalog.json"),
            "--ros-distro",
            "humble",
            "--os",
            "ubuntu",
            "--os-version",
            "22.04",
            "--architecture",
            "amd64",
            "--output",
            str(output),
        ]
    )

    payload = json.loads(output.read_text())
    assert result == 0
    assert payload["schema_version"] == 2
    assert len(payload["updates"]) == 5
    assert payload["refusals"] == []


def test_registry_catalog_parses_provider_api_responses_without_network() -> None:
    repository = FIXTURE_ROOT / "robot_repo"
    report = RepositoryScanner(repository).scan()

    def response(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "pypi.org":
            return httpx.Response(
                200,
                json={
                    "info": {"version": "2.32.5"},
                    "releases": {"2.32.5": [{"upload_time_iso_8601": "2025-08-18T20:46:00Z"}]},
                },
            )
        if host == "hub.docker.com":
            return httpx.Response(
                200,
                json={
                    "last_updated": "2026-07-20T10:30:00Z",
                    "images": [
                        {
                            "architecture": "amd64",
                            "digest": "sha256:" + ("2" * 64),
                        }
                    ],
                },
            )
        if host == "api.launchpad.net":
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {
                            "binary_package_version": "7.81.0-1ubuntu1.21",
                            "date_published": "2026-06-17T14:20:00Z",
                            "self_link": "https://api.launchpad.net/curl-update",
                        }
                    ]
                },
            )
        if host == "api.github.com" and path.endswith("/navigation2"):
            return httpx.Response(200, json={"default_branch": "main"})
        if host == "api.github.com" and path.endswith("/commits/main"):
            return httpx.Response(
                200,
                json={
                    "sha": "3" * 40,
                    "commit": {"committer": {"date": "2026-07-19T08:15:00Z"}},
                },
            )
        if host == "api.github.com" and "/compare/" in path:
            return httpx.Response(200, json={"status": "ahead"})
        if host == "raw.githubusercontent.com" and path.endswith("/humble/distribution.yaml"):
            return httpx.Response(
                200,
                text=(
                    "repositories:\n"
                    "  rclcpp:\n"
                    "    release:\n"
                    "      version: 16.0.15-1\n"
                    "      packages: [rclcpp]\n"
                ),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        catalog = RegistryUpdateCatalog(client)
        discovery = discover_updates(report, target=TARGET, catalog=catalog)

    assert [candidate.identity for candidate in discovery.candidates] == [
        "apt:curl",
        "docker:ros",
        "git:navigation",
        "python:requests",
        "ros:rclcpp",
    ]
    assert discovery.refusals == ()
    rosdep = discovery.candidates[-1]
    assert rosdep.target == "16.0.15-1"
    assert rosdep.metadata["apt_package"] == "ros-humble-rclcpp"


def test_registry_catalog_resolves_ghcr_tags_to_architecture_digests() -> None:
    report = ScanReport(
        root="/fixture",
        dependencies=[
            Dependency(
                ecosystem=Ecosystem.DOCKER,
                name="ghcr.io/acme/robot-runner",
                resolved="1.4.0",
                constraint="==1.4.0",
                source=SourceLocation("Dockerfile", 1),
            )
        ],
        files_scanned=["Dockerfile"],
    )
    amd64_digest = "sha256:" + ("4" * 64)

    def response(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "ghcr.io" and path == "/token":
            return httpx.Response(200, json={"token": "anonymous"})
        if host == "ghcr.io" and path == "/v2/acme/robot-runner/manifests/1.4.0":
            assert request.headers["Authorization"] == "Bearer anonymous"
            return httpx.Response(
                200,
                json={
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "annotations": {
                        "org.opencontainers.image.created": "2026-07-30T12:00:00Z"
                    },
                    "manifests": [
                        {
                            "digest": amd64_digest,
                            "platform": {"os": "linux", "architecture": "amd64"},
                        },
                        {
                            "digest": "sha256:" + ("5" * 64),
                            "platform": {"os": "linux", "architecture": "arm64"},
                        },
                    ],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        catalog = RegistryUpdateCatalog(client)
        discovery = discover_updates(
            report,
            target=TARGET,
            catalog=catalog,
            providers=(DockerUpdateProvider(),),
        )

    assert [candidate.target for candidate in discovery.candidates] == [amd64_digest]
    candidate = discovery.candidates[0]
    assert candidate.release_date == "2026-07-30T12:00:00Z"
    assert candidate.metadata["registry"] == "ghcr"
    assert candidate.metadata["architecture"] == "amd64"


def test_registry_catalog_warns_when_ghcr_image_misses_target_architecture() -> None:
    report = ScanReport(
        root="/fixture",
        dependencies=[
            Dependency(
                ecosystem=Ecosystem.DOCKER,
                name="ghcr.io/acme/robot-runner",
                resolved="1.4.0",
                constraint="==1.4.0",
                source=SourceLocation("Dockerfile", 1),
            )
        ],
        files_scanned=["Dockerfile"],
    )

    def response(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "ghcr.io" and path == "/token":
            return httpx.Response(200, json={"token": "anonymous"})
        if host == "ghcr.io" and path == "/v2/acme/robot-runner/manifests/1.4.0":
            return httpx.Response(
                200,
                json={
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "manifests": [
                        {
                            "digest": "sha256:" + ("6" * 64),
                            "platform": {"os": "linux", "architecture": "arm64"},
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        catalog = RegistryUpdateCatalog(client)
        discovery = discover_updates(
            report,
            target=TARGET,
            catalog=catalog,
            providers=(DockerUpdateProvider(),),
        )

    assert discovery.candidates == ()
    assert any("target architecture" in warning for warning in discovery.warnings)


def test_registry_catalog_resolves_rosdep_system_keys_through_launchpad() -> None:
    report = ScanReport(
        root="/fixture",
        dependencies=[
            Dependency(
                ecosystem=Ecosystem.ROS,
                name="libboost-dev",
                source=SourceLocation("package.xml", 12),
            )
        ],
        files_scanned=["package.xml"],
    )

    def response(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "raw.githubusercontent.com" and path.endswith("/humble/distribution.yaml"):
            return httpx.Response(200, text="repositories: {}\n")
        if host == "raw.githubusercontent.com" and path.endswith("/rosdep/base.yaml"):
            return httpx.Response(200, text="libboost-dev:\n  ubuntu: [libboost-dev]\n")
        if host == "api.launchpad.net":
            assert request.url.params["binary_name"] == "libboost-dev"
            assert request.url.params["distro_arch_series"] == "/ubuntu/jammy/amd64"
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {
                            "binary_package_version": "1.74.0.3ubuntu7",
                            "date_published": "2026-05-02T09:00:00Z",
                            "self_link": "https://api.launchpad.net/libboost-dev",
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        catalog = RegistryUpdateCatalog(client)
        discovery = discover_updates(
            report,
            target=TARGET,
            catalog=catalog,
            providers=(RosdepUpdateProvider(),),
        )

    assert [candidate.target for candidate in discovery.candidates] == ["1.74.0.3ubuntu7"]
    candidate = discovery.candidates[0]
    assert candidate.metadata["rosdep_key"] == "libboost-dev"
    assert candidate.metadata["apt_package"] == "libboost-dev"
