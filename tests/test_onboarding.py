from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from fraeno.cli import main
from fraeno.config import load_config
from fraeno.onboarding import (
    APP_SLUG,
    APP_URL,
    CHECK_NAME,
    CheckStatus,
    OnboardingError,
    doctor_repository,
    initialize_repository,
)

ROOT = Path(__file__).parents[1]
RUNNER_DIGEST = (
    "us-central1-docker.pkg.dev/example/fraeno-runner/runner@sha256:"
    + "a" * 64
)


def initialize(root: Path) -> None:
    initialize_repository(
        root,
        project_name="warehouse-robot",
        build_command="colcon build --event-handlers console_direct+",
        setup_script="install/setup.bash",
        launch_command="ros2 launch warehouse_bringup system.launch.py",
        required_nodes=("/controller",),
        required_topics=("/robot/command",),
        required_services=("/robot/health",),
        required_actions=("/robot/move",),
        required_transforms=("base_link->sensor_link",),
        required_diagnostics=("controller",),
        rate_topics=("/robot/command",),
        diagnostics_topics=("/diagnostics",),
        transform_topics=("/tf", "/tf_static"),
    )


def completed(
    command: tuple[str, ...],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_init_writes_a_valid_contract_and_release_matched_templates(
    tmp_path: Path,
) -> None:
    initialize(tmp_path)

    config = load_config(tmp_path / ".fraeno.yml")
    raw = yaml.safe_load((tmp_path / ".fraeno.yml").read_text())

    assert config.project_name == "warehouse-robot"
    assert config.validation.ros2_observer is not None
    assert config.validation.ros2_observer.launch_command == (
        "ros2",
        "launch",
        "warehouse_bringup",
        "system.launch.py",
    )
    assert config.validation.required_nodes == {"/controller"}
    assert "python3 -m fraeno.cli observe-ros2" in (
        config.validation.observation_command[-1]
    )
    assert raw["target"] == {
        "ros_distribution": "humble",
        "operating_system": "ubuntu",
        "operating_system_version": "22.04",
        "architecture": "amd64",
    }
    workflow = tmp_path / ".github/workflows/fraeno-validation.yml"
    updates = tmp_path / ".github/workflows/fraeno-updates.yml"
    runner = tmp_path / ".github/fraeno/run-isolated-validation.sh"
    assert workflow.read_bytes() == (
        ROOT / "templates/github/fraeno-validation.yml"
    ).read_bytes()
    assert runner.read_bytes() == (
        ROOT / "templates/github/run-isolated-validation.sh"
    ).read_bytes()
    assert updates.read_bytes() == (
        ROOT / "templates/github/fraeno-updates.yml"
    ).read_bytes()
    assert runner.stat().st_mode & 0o111
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in (
        workflow.read_text()
    )
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in (
        workflow.read_text()
    )


def test_init_accepts_the_arm64_target(tmp_path: Path) -> None:
    initialize_repository(
        tmp_path,
        project_name="jetson-robot",
        build_command="colcon build",
        setup_script="install/setup.bash",
        launch_command="ros2 launch robot system.launch.py",
        architecture="arm64",
    )

    raw = yaml.safe_load((tmp_path / ".fraeno.yml").read_text())
    assert raw["target"] == {
        "ros_distribution": "humble",
        "operating_system": "ubuntu",
        "operating_system_version": "22.04",
        "architecture": "arm64",
    }


def test_init_refuses_an_unsupported_architecture(tmp_path: Path) -> None:
    with pytest.raises(OnboardingError, match="amd64 or arm64, not riscv64"):
        initialize_repository(
            tmp_path,
            project_name="robot",
            build_command="colcon build",
            setup_script="install/setup.bash",
            launch_command="ros2 launch robot system.launch.py",
            architecture="riscv64",
        )

    assert list(tmp_path.iterdir()) == []


def test_init_refuses_to_replace_any_trusted_file(tmp_path: Path) -> None:
    (tmp_path / ".fraeno.yml").write_text("owned by the repository\n")

    with pytest.raises(OnboardingError, match=r"refusing.*\.fraeno\.yml"):
        initialize(tmp_path)

    assert (tmp_path / ".fraeno.yml").read_text() == "owned by the repository\n"
    assert not (tmp_path / ".github").exists()


def test_init_refuses_a_broken_symlink_at_a_generated_file(
    tmp_path: Path,
) -> None:
    destination = tmp_path / ".fraeno.yml"
    missing_target = tmp_path / "missing-config"
    destination.symlink_to(missing_target)

    with pytest.raises(OnboardingError, match=r"refusing.*\.fraeno\.yml"):
        initialize(tmp_path)

    assert destination.is_symlink()
    assert destination.readlink() == missing_target
    assert not missing_target.exists()
    assert not (tmp_path / ".github").exists()


def test_init_refuses_a_symlinked_generated_parent_without_writing_outside(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    outside = tmp_path / "outside"
    repository.mkdir()
    outside.mkdir()
    (repository / ".github").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OnboardingError, match="symlinked onboarding directory"):
        initialize(repository)

    assert not (repository / ".fraeno.yml").exists()
    assert list(outside.iterdir()) == []


def test_init_refuses_a_non_directory_generated_parent(tmp_path: Path) -> None:
    (tmp_path / ".github").write_text("not a directory\n")

    with pytest.raises(OnboardingError, match="non-directory onboarding path"):
        initialize(tmp_path)

    assert not (tmp_path / ".fraeno.yml").exists()
    assert (tmp_path / ".github").read_text() == "not a directory\n"


def test_open_pr_uses_only_generated_paths_and_sets_immutable_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_which(name: str) -> str:
        return f"/usr/bin/{name}"

    def fake_run(
        command: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout, env
        assert cwd == tmp_path
        commands.append(command)
        if "rev-parse" in command:
            return completed(command, stdout=f"{tmp_path}\n")
        if command[1:3] == ("status", "--porcelain"):
            return completed(command)
        if command[1:3] == ("pr", "create"):
            return completed(
                command,
                stdout="https://github.com/example/robot/pull/1\n",
            )
        return completed(command)

    monkeypatch.setattr("fraeno.onboarding.shutil.which", fake_which)
    monkeypatch.setattr("fraeno.onboarding._run", fake_run)

    result = initialize_repository(
        tmp_path,
        project_name="robot",
        build_command="colcon build",
        setup_script="install/setup.bash",
        launch_command="ros2 launch robot system.launch.py",
        open_pull_request=True,
        runner_image=RUNNER_DIGEST,
    )

    assert result.pull_request_url == "https://github.com/example/robot/pull/1"
    add = next(command for command in commands if command[1] == "add")
    assert add[3:] == (
        ".fraeno.yml",
        ".github/workflows/fraeno-validation.yml",
        ".github/workflows/fraeno-updates.yml",
        ".github/fraeno/run-isolated-validation.sh",
    )
    variable = next(command for command in commands if command[1:3] == ("variable", "set"))
    assert variable[-1] == RUNNER_DIGEST
    assert ("--draft") in next(
        command for command in commands if command[1:3] == ("pr", "create")
    )


def test_open_pr_requires_a_clean_tree_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        command: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout, env
        if "rev-parse" in command:
            return completed(command, stdout=f"{tmp_path}\n")
        if command[1:3] == ("status", "--porcelain"):
            return completed(command, stdout=" M README.md\n")
        return completed(command)

    monkeypatch.setattr(
        "fraeno.onboarding.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr("fraeno.onboarding._run", fake_run)

    with pytest.raises(OnboardingError, match="no uncommitted changes"):
        initialize_repository(
            tmp_path,
            project_name="robot",
            build_command="colcon build",
            setup_script="install/setup.bash",
            launch_command="ros2 launch robot system.launch.py",
            open_pull_request=True,
            runner_image=RUNNER_DIGEST,
        )

    assert list(tmp_path.iterdir()) == []


def test_doctor_names_every_missing_required_file(tmp_path: Path) -> None:
    report = doctor_repository(tmp_path, local_only=True)

    failures = [check for check in report.checks if check.status is CheckStatus.FAIL]
    assert [check.message for check in failures] == [
        ".fraeno.yml is missing.",
        ".github/workflows/fraeno-validation.yml is missing.",
        ".github/workflows/fraeno-updates.yml is missing.",
        ".github/fraeno/run-isolated-validation.sh is missing.",
    ]
    assert not report.ready


def test_doctor_accepts_a_declared_arm64_target(tmp_path: Path) -> None:
    initialize(tmp_path)
    config_path = tmp_path / ".fraeno.yml"
    raw = yaml.safe_load(config_path.read_text())
    raw["target"]["architecture"] = "arm64"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False, width=100))

    report = doctor_repository(tmp_path, local_only=True)

    target = next(check for check in report.checks if check.name == "Target")
    assert target.status is CheckStatus.PASS
    assert "arm64" in target.message


def test_doctor_refuses_an_unsupported_target_architecture(tmp_path: Path) -> None:
    initialize(tmp_path)
    config_path = tmp_path / ".fraeno.yml"
    raw = yaml.safe_load(config_path.read_text())
    raw["target"]["architecture"] = "riscv64"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False, width=100))

    report = doctor_repository(tmp_path, local_only=True)

    target = next(check for check in report.checks if check.name == "Target")
    assert target.status is CheckStatus.FAIL
    assert "amd64 or arm64" in target.message
    assert "riscv64" in target.message
    assert not report.ready


def test_doctor_verifies_local_github_and_app_requirements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize(tmp_path)
    (tmp_path / "install").mkdir()
    (tmp_path / "install/setup.bash").write_text("# built\n")

    def fake_run(
        command: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout, env
        if command[1:3] == ("version", "--format"):
            return completed(command, stdout="27.5.1\n")
        if command[1:3] == ("auth", "status"):
            return completed(command)
        if command[1:3] == ("repo", "view"):
            return completed(command, stdout="example/robot\n")
        if command[1] != "api":
            raise AssertionError(command)
        endpoint = command[-1]
        return completed(command, stdout=json.dumps(github_payload(endpoint)))

    monkeypatch.setattr(
        "fraeno.onboarding.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr("fraeno.onboarding._run", fake_run)

    report = doctor_repository(tmp_path, pull_request=7)

    assert report.ready
    assert report.github_app_verified
    assert not report.live_observer_verified
    assert not [
        check for check in report.checks if check.status is CheckStatus.FAIL
    ]
    assert next(
        check
        for check in report.checks
        if check.name == "Fraeno App installation"
    ).message == "Fraeno received pull request #7 and created its check."
    assert next(
        check for check in report.checks if check.name == "Fraeno App round trip"
    ).status is CheckStatus.PASS
    assert next(
        check for check in report.checks if check.name == "Robot integration result"
    ).status is CheckStatus.PASS
    registration = next(
        check for check in report.checks if check.name == "Fraeno App registration"
    )
    assert registration.status is CheckStatus.PASS
    assert "does not prove" in registration.message


def test_doctor_does_not_treat_registration_metadata_as_effective_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize(tmp_path)
    (tmp_path / "install").mkdir()
    (tmp_path / "install/setup.bash").write_text("# built\n")

    def fake_run(
        command: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout, env
        if command[1:3] == ("version", "--format"):
            return completed(command, stdout="27.5.1\n")
        if command[1:3] == ("auth", "status"):
            return completed(command)
        if command[1] == "api":
            payload = github_payload(command[-1])
            if "/check-runs?" in command[-1]:
                payload["check_runs"][0]["status"] = "in_progress"
                payload["check_runs"][0]["conclusion"] = None
            return completed(command, stdout=json.dumps(payload))
        raise AssertionError(command)

    monkeypatch.setattr(
        "fraeno.onboarding.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr("fraeno.onboarding._run", fake_run)

    report = doctor_repository(
        tmp_path,
        github_repository="example/robot",
        pull_request=7,
    )

    assert not report.ready
    assert not report.github_app_verified
    assert next(
        check for check in report.checks if check.name == "Fraeno App registration"
    ).status is CheckStatus.PASS
    assert next(
        check for check in report.checks if check.name == "Fraeno App round trip"
    ).status is CheckStatus.FAIL


def test_doctor_separates_a_completed_round_trip_from_a_failed_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize(tmp_path)
    (tmp_path / "install").mkdir()
    (tmp_path / "install/setup.bash").write_text("# built\n")

    def fake_run(
        command: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout, env
        if command[1:3] == ("version", "--format"):
            return completed(command, stdout="27.5.1\n")
        if command[1:3] == ("auth", "status"):
            return completed(command)
        if command[1] == "api":
            payload = github_payload(command[-1])
            if "/check-runs?" in command[-1]:
                payload["check_runs"][0]["conclusion"] = "failure"
            return completed(command, stdout=json.dumps(payload))
        raise AssertionError(command)

    monkeypatch.setattr(
        "fraeno.onboarding.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr("fraeno.onboarding._run", fake_run)

    report = doctor_repository(
        tmp_path,
        github_repository="example/robot",
        pull_request=7,
    )

    assert report.github_app_verified
    assert not report.ready
    assert next(
        check for check in report.checks if check.name == "Fraeno App round trip"
    ).status is CheckStatus.PASS
    result = next(
        check for check in report.checks if check.name == "Robot integration result"
    )
    assert result.status is CheckStatus.FAIL
    assert "completed with failure" in result.message


def github_payload(endpoint: str) -> dict[str, Any]:
    if endpoint == "repos/example/robot":
        return {"default_branch": "main"}
    if "/contents/" in endpoint:
        return {"path": endpoint.split("/contents/", maxsplit=1)[1]}
    if endpoint.endswith("/actions/variables/FRAENO_RUNNER_IMAGE"):
        return {"name": "FRAENO_RUNNER_IMAGE", "value": RUNNER_DIGEST}
    if endpoint.endswith("/pulls/7"):
        return {"head": {"sha": "b" * 40}}
    if "/check-runs?" in endpoint:
        return {
            "check_runs": [
                {
                    "name": CHECK_NAME,
                    "status": "completed",
                    "conclusion": "success",
                    "details_url": (
                        "https://github.com/example/robot/actions/runs/123456"
                    ),
                    "app": {
                        "slug": APP_SLUG,
                        "permissions": {
                            "actions": "write",
                            "checks": "write",
                            "contents": "read",
                            "metadata": "read",
                            "pull_requests": "read",
                        },
                        "events": ["check_run", "pull_request", "workflow_run"],
                    },
                }
            ]
        }
    raise AssertionError(endpoint)


def test_doctor_names_exact_missing_app_permission_and_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize(tmp_path)
    (tmp_path / "install").mkdir()
    (tmp_path / "install/setup.bash").write_text("# built\n")

    def fake_run(
        command: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout, env
        if command[1:3] == ("version", "--format"):
            return completed(command, stdout="27.5.1\n")
        if command[1:3] == ("auth", "status"):
            return completed(command)
        if command[1] == "api":
            payload = github_payload(command[-1])
            if "/check-runs?" in command[-1]:
                app = payload["check_runs"][0]["app"]
                app["permissions"]["actions"] = "read"
                app["events"].remove("workflow_run")
            return completed(command, stdout=json.dumps(payload))
        raise AssertionError(command)

    monkeypatch.setattr(
        "fraeno.onboarding.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr("fraeno.onboarding._run", fake_run)

    report = doctor_repository(
        tmp_path,
        github_repository="example/robot",
        pull_request=7,
    )

    registration = next(
        check for check in report.checks if check.name == "Fraeno App registration"
    )
    assert registration.status is CheckStatus.FAIL
    assert "permissions actions: write" in registration.message
    assert "events workflow_run" in registration.message
    assert not report.github_app_verified


def test_doctor_names_missing_app_installation_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize(tmp_path)
    (tmp_path / "install").mkdir()
    (tmp_path / "install/setup.bash").write_text("# built\n")

    def fake_run(
        command: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout, env
        if command[1:3] == ("version", "--format"):
            return completed(command, stdout="27.5.1\n")
        if command[1:3] == ("auth", "status"):
            return completed(command)
        if command[1] == "api":
            payload = github_payload(command[-1])
            if "/check-runs?" in command[-1]:
                payload = {"check_runs": []}
            return completed(command, stdout=json.dumps(payload))
        raise AssertionError(command)

    monkeypatch.setattr(
        "fraeno.onboarding.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr("fraeno.onboarding._run", fake_run)

    report = doctor_repository(
        tmp_path,
        github_repository="example/robot",
        pull_request=7,
    )

    installation = next(
        check
        for check in report.checks
        if check.name == "Fraeno App installation"
    )
    assert installation.status is CheckStatus.FAIL
    assert CHECK_NAME in installation.message
    assert APP_URL in (installation.fix or "")


def test_cli_init_and_doctor_json_are_documented_commands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [
                "init",
                str(tmp_path),
                "--launch-command",
                "ros2 launch robot system.launch.py",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "created .fraeno.yml" in output

    assert main(["doctor", str(tmp_path), "--local-only", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["repository"] == str(tmp_path)
    assert payload["live_observer_verified"] is False
    assert payload["github_app_verified"] is False


def test_onboarding_copy_uses_plain_punctuation() -> None:
    copy = (ROOT / "docs/onboarding.md").read_text()

    assert "—" not in copy
    assert "DeepUbuntu Fraeno" not in copy
