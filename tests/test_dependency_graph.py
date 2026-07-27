import json
from pathlib import Path
from typing import Any

from fraeno.cli import main
from fraeno.dependency_graph import (
    Provenance,
    ResolutionResult,
    ResolutionStatus,
    TargetPlatform,
)
from fraeno.lockfile import build_lockfile, compare_lockfiles, write_lockfile
from fraeno.models import Dependency, Ecosystem
from fraeno.scanner import RepositoryScanner

FIXTURES = Path(__file__).parents[1] / "fixtures" / "dependency_graph"


def fixture_lock(name: str) -> dict[str, Any]:
    root = FIXTURES / name
    report = RepositoryScanner(root).scan()
    assert report.warnings == []
    return build_lockfile(report, root)


def test_builds_deterministic_cross_layer_lock() -> None:
    root = FIXTURES / "baseline"
    report = RepositoryScanner(root).scan()

    first = build_lockfile(report, root)
    second = build_lockfile(report, root)

    assert first == second
    assert "generated_at" not in first
    assert first["schema_version"] == 2
    assert first["target"] == {
        "ros_distribution": "humble",
        "operating_system": "ubuntu",
        "operating_system_version": "22.04",
        "architecture": "amd64",
    }
    assert [item["id"] for item in first["manifests"]] == sorted(
        item["id"] for item in first["manifests"]
    )
    assert [item["id"] for item in first["declarations"]] == sorted(
        item["id"] for item in first["declarations"]
    )
    assert [item["id"] for item in first["artifacts"]] == sorted(
        item["id"] for item in first["artifacts"]
    )
    assert first["edges"] == sorted(
        first["edges"],
        key=lambda item: (
            item["source"],
            item["target"],
            item["relationship"],
        ),
    )


def test_keeps_duplicate_paths_linked_to_shared_component() -> None:
    lockfile = fixture_lock("baseline")

    opencv = next(item for item in lockfile["components"] if item["id"] == "component:opencv")
    declarations = {item["id"]: item for item in lockfile["declarations"]}
    linked = [declarations[item_id] for item_id in opencv["declarations"]]

    assert len(linked) == 4
    assert {item["ecosystem"] for item in linked} == {
        "apt",
        "cmake",
        "python",
    }
    apt = [item for item in linked if item["ecosystem"] == "apt"]
    assert {item["target"]["container_stage"] for item in apt} == {
        "builder",
        "runtime",
    }
    assert len({item["id"] for item in apt}) == 2
    assert all(item["component_link"] == "known_alias" for item in linked)


def test_unknown_resolutions_are_explicit_and_have_provenance() -> None:
    lockfile = fixture_lock("baseline")

    declarations = lockfile["declarations"]
    rclcpp = next(
        item for item in declarations if item["ecosystem"] == "ros" and item["name"] == "rclcpp"
    )
    cmake_opencv = next(
        item for item in declarations if item["ecosystem"] == "cmake" and item["name"] == "OpenCV"
    )
    numpy = next(
        item for item in declarations if item["ecosystem"] == "python" and item["name"] == "numpy"
    )

    assert rclcpp["resolution"]["status"] == "unknown"
    assert "rosdep" in rclcpp["resolution"]["unknown_reason"]
    assert cmake_opencv["resolution"]["status"] == "unknown"
    assert "CMake" in cmake_opencv["resolution"]["unknown_reason"]
    assert numpy["resolution"]["status"] == "unknown"
    assert rclcpp["resolution"]["artifact_id"] is None
    assert rclcpp["resolution"]["provenance"][0]["provider"] == "manifest"


def test_distinguishes_declared_versions_from_immutable_resolutions() -> None:
    lockfile = fixture_lock("baseline")

    docker = next(item for item in lockfile["declarations"] if item["ecosystem"] == "docker")
    git = next(item for item in lockfile["declarations"] if item["ecosystem"] == "git")
    python = next(
        item
        for item in lockfile["declarations"]
        if item["ecosystem"] == "python" and item["name"] == "opencv-python"
    )

    assert docker["resolution"]["status"] == "declared"
    assert python["resolution"]["status"] == "declared"
    assert git["resolution"]["status"] == "resolved"


def test_missing_target_evidence_is_recorded_as_unknown(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.32.5\n")
    report = RepositoryScanner(tmp_path).scan()

    lockfile = build_lockfile(report, tmp_path)

    assert lockfile["target"] == {
        "ros_distribution": "unknown",
        "operating_system": "unknown",
        "operating_system_version": "unknown",
        "architecture": "unknown",
    }


def test_explicit_target_overrides_inference() -> None:
    root = FIXTURES / "baseline"
    report = RepositoryScanner(root).scan()
    target = TargetPlatform(
        ros_distribution="jazzy",
        operating_system="ubuntu",
        operating_system_version="24.04",
        architecture="arm64",
    )

    lockfile = build_lockfile(report, root, target=target)

    assert lockfile["target"] == target.to_dict()
    assert all(item["target"]["architecture"] == "arm64" for item in lockfile["declarations"])


class FixtureRosdepProvider:
    name = "fixture-rosdep"

    def resolve(
        self,
        dependency: Dependency,
        target: TargetPlatform,
    ) -> ResolutionResult | None:
        if dependency.ecosystem is not Ecosystem.ROS or dependency.name != "rclcpp":
            return None
        assert target.ros_distribution == "humble"
        return ResolutionResult(
            status=ResolutionStatus.RESOLVED,
            version="16.0.15-1jammy.20260601",
            artifact_name="ros-humble-rclcpp",
            component="rclcpp",
            provenance=(
                Provenance(
                    provider=self.name,
                    source="https://packages.ros.org/ros2/ubuntu",
                    evidence="Fixture rosdep rule and APT package index agree.",
                    metadata={"rosdep_key": "rclcpp"},
                ),
            ),
            metadata={"release_date": "2026-06-01"},
        )


def test_provider_can_supply_resolution_without_changing_graph_contract() -> None:
    root = FIXTURES / "baseline"
    report = RepositoryScanner(root).scan()

    lockfile = build_lockfile(
        report,
        root,
        providers=(FixtureRosdepProvider(),),
    )

    rclcpp = next(
        item
        for item in lockfile["declarations"]
        if item["ecosystem"] == "ros" and item["name"] == "rclcpp"
    )
    artifact = next(
        item for item in lockfile["artifacts"] if item["id"] == rclcpp["resolution"]["artifact_id"]
    )
    assert rclcpp["resolution"]["status"] == "resolved"
    assert rclcpp["component_link"] == "provider:fixture-rosdep"
    assert artifact["name"] == "ros-humble-rclcpp"
    assert artifact["provenance"][0]["provider"] == "fixture-rosdep"
    assert artifact["metadata"]["release_date"] == "2026-06-01"


def test_lock_comparison_explains_exact_cross_layer_change() -> None:
    comparison = compare_lockfiles(
        fixture_lock("baseline"),
        fixture_lock("candidate"),
    )

    assert comparison["changed"]
    assert comparison["target_changes"] == []
    cross_layer = comparison["cross_layer_changes"]
    assert len(cross_layer) == 1
    opencv = cross_layer[0]
    assert opencv["component"] == "opencv"
    assert opencv["ecosystems"] == ["apt", "cmake", "python"]
    assert len(opencv["declaration_changes"]) == 4
    explanation = opencv["explanation"]
    assert "4.5.4+dfsg-9ubuntu4 to 4.8.0+dfsg-5" in explanation
    assert "4.5 to 4.8" in explanation
    assert "4.5.5.64 to 4.8.1.78" in explanation
    assert "Dockerfile line 2" in explanation
    assert "requirements.txt line 1" in explanation
    assert comparison["summary"]["cross_layer_components_changed"] == 1


def test_lock_comparison_reports_no_change_for_same_lock() -> None:
    lockfile = fixture_lock("baseline")

    comparison = compare_lockfiles(lockfile, lockfile)

    assert not comparison["changed"]
    assert comparison["manifest_changes"] == []
    assert comparison["declaration_changes"] == []
    assert comparison["component_changes"] == []


def test_compare_locks_cli_writes_machine_readable_explanation(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    output = tmp_path / "comparison.json"
    write_lockfile(fixture_lock("baseline"), baseline)
    write_lockfile(fixture_lock("candidate"), candidate)

    result = main(
        [
            "compare-locks",
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
            "--output",
            str(output),
        ]
    )

    assert result == 0
    payload = json.loads(output.read_text())
    assert payload["cross_layer_changes"][0]["component"] == "opencv"
