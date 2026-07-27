from pathlib import Path

import httpx
import pytest

from fraeno.scanner import RepositoryScanner
from fraeno.updates import (
    apply_next_python_update,
    apply_update,
    apply_updates,
    find_python_updates,
)


def test_updates_exact_python_pin(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("requests==2.31.0\n")
    report = RepositoryScanner(tmp_path).scan()

    result = apply_update(tmp_path, report, "python:requests", "2.32.5")

    assert result.changed_files == ("requirements.txt",)
    assert requirements.read_text() == "requests==2.32.5\n"


def test_dry_run_does_not_change_file(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("requests==2.31.0\n")
    report = RepositoryScanner(tmp_path).scan()

    result = apply_update(
        tmp_path, report, "python:requests", "2.32.5", dry_run=True
    )

    assert result.dry_run
    assert requirements.read_text() == "requests==2.31.0\n"


def test_pins_docker_image_digest(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM ros:humble-ros-base-jammy\n")
    report = RepositoryScanner(tmp_path).scan()

    apply_update(
        tmp_path,
        report,
        "docker:ros",
        "sha256:0123456789abcdef",
    )

    assert dockerfile.read_text() == "FROM ros@sha256:0123456789abcdef\n"


def test_updates_vcstool_ref_without_reformatting(tmp_path: Path) -> None:
    repos = tmp_path / "robot.repos"
    repos.write_text(
        "repositories:\n"
        "  navigation:\n"
        "    type: git\n"
        "    url: https://github.com/example/navigation.git\n"
        "    version: old-sha\n"
    )
    report = RepositoryScanner(tmp_path).scan()

    apply_update(tmp_path, report, "git:navigation", "new-sha")

    assert "    version: new-sha\n" in repos.read_text()
    assert "https://github.com/example/navigation.git" in repos.read_text()


def test_finds_newer_pypi_release(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
    report = RepositoryScanner(tmp_path).scan()

    def response(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/pypi/requests/json"
        return httpx.Response(200, json={"info": {"version": "2.32.5"}})

    with httpx.Client(
        transport=httpx.MockTransport(response), base_url="https://pypi.org"
    ) as client:
        candidates, warnings = find_python_updates(report, client)

    assert warnings == []
    assert candidates[0].current == "2.31.0"
    assert candidates[0].target == "2.32.5"


def test_applies_next_update_one_at_a_time(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("alpha==1.0.0\nbeta==1.0.0\n")
    report = RepositoryScanner(tmp_path).scan()

    def response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"info": {"version": "1.1.0"}})

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        result, warnings = apply_next_python_update(
            tmp_path, report, client=client
        )

    assert warnings == []
    assert result is not None
    assert result.dependency == "python:alpha"
    assert requirements.read_text() == "alpha==1.1.0\nbeta==1.0.0\n"


def test_grouped_updates_are_planned_before_any_file_is_written(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("alpha==1.0.0\n")
    report = RepositoryScanner(tmp_path).scan()

    with pytest.raises(ValueError, match="dependency not found"):
        apply_updates(
            tmp_path,
            report,
            (
                ("python:alpha", "1.1.0"),
                ("python:missing", "2.0.0"),
            ),
        )

    assert requirements.read_text() == "alpha==1.0.0\n"
