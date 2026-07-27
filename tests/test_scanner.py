from pathlib import Path

from fraeno.models import Ecosystem
from fraeno.scanner import RepositoryScanner


def test_scans_cross_layer_dependencies(tmp_path: Path) -> None:
    (tmp_path / "package.xml").write_text(
        """
        <package format="3">
          <name>robot</name>
          <version>0.1.0</version>
          <description>fixture</description>
          <maintainer email="robot@example.com">Robot</maintainer>
          <license>MIT</license>
          <depend version_gte="1.2">rclcpp</depend>
          <test_depend>launch_testing</test_depend>
        </package>
        """
    )
    (tmp_path / "requirements.txt").write_text("numpy==2.1.0\n")
    (tmp_path / "CMakeLists.txt").write_text("find_package(OpenCV 4.8 REQUIRED)\n")
    (tmp_path / "Dockerfile").write_text(
        "FROM ros:humble-ros-base-jammy\n"
        "RUN apt-get update && apt-get install -y libeigen3-dev=3.4.0-4\n"
    )
    (tmp_path / "robot.repos").write_text(
        """
repositories:
  navigation:
    type: git
    url: https://github.com/example/navigation.git
    version: 0123456789abcdef
        """
    )

    report = RepositoryScanner(tmp_path).scan()

    identities = {dependency.identity for dependency in report.dependencies}
    assert {
        "ros:rclcpp",
        "ros:launch_testing",
        "python:numpy",
        "cmake:OpenCV",
        "docker:ros",
        "apt:libeigen3-dev",
        "git:navigation",
    } <= identities
    docker = next(
        item for item in report.dependencies if item.ecosystem is Ecosystem.DOCKER
    )
    assert docker.resolved == "humble-ros-base-jammy"
    assert report.warnings == []


def test_unsupported_requirement_is_reported(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("this is not a requirement !!!\n")
    report = RepositoryScanner(tmp_path).scan()
    assert report.dependencies == []
    assert "unsupported requirement" in report.warnings[0]
