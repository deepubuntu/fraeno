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
    FixtureUpdateCatalog,
    PythonUpdateProvider,
    RegistryUpdateCatalog,
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
        raise AssertionError(f"unexpected request: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        catalog = RegistryUpdateCatalog(client)
        discovery = discover_updates(report, target=TARGET, catalog=catalog)

    assert [candidate.identity for candidate in discovery.candidates] == [
        "apt:curl",
        "docker:ros",
        "git:navigation",
        "python:requests",
    ]
    assert discovery.refusals[0].identity == "ros:rclcpp"
    assert "currently resolved package" in discovery.refusals[0].reason
