from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from fraeno.config import ConfigError, FraenoConfig, load_config
from fraeno.dependency_graph import TargetPlatform, infer_target_platform
from fraeno.scanner import RepositoryScanner
from fraeno.validation.observation import ObservationError, SystemObservation

CONFIG_PATH = Path(".fraeno.yml")
WORKFLOW_PATH = Path(".github/workflows/fraeno-validation.yml")
UPDATES_WORKFLOW_PATH = Path(".github/workflows/fraeno-updates.yml")
RUNNER_PATH = Path(".github/fraeno/run-isolated-validation.sh")
GENERATED_PATHS = (
    CONFIG_PATH,
    WORKFLOW_PATH,
    UPDATES_WORKFLOW_PATH,
    RUNNER_PATH,
)
SUPPORTED_TARGET = TargetPlatform(
    ros_distribution="humble",
    operating_system="ubuntu",
    operating_system_version="22.04",
    architecture="amd64",
)
REQUIRED_APP_PERMISSIONS = {
    "actions": "write",
    "checks": "write",
    "contents": "read",
    "metadata": "read",
    "pull_requests": "read",
}
REQUIRED_APP_EVENTS = frozenset({"check_run", "pull_request", "workflow_run"})
APP_SLUG = "fraeno-robotics"
APP_URL = "https://github.com/apps/fraeno-robotics"
CHECK_NAME = "Fraeno / robot integration"
RUNNER_IMAGE_PATTERN = re.compile(r"^.+@sha256:[0-9a-f]{64}$")


class OnboardingError(ValueError):
    pass


class CheckStatus(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    message: str
    fix: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "fix": self.fix,
        }


@dataclass(frozen=True)
class DoctorReport:
    repository: str
    checks: tuple[DoctorCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.status is not CheckStatus.FAIL for check in self.checks)

    @property
    def live_observer_verified(self) -> bool:
        return any(
            check.name == "Observer" and check.status is CheckStatus.PASS
            for check in self.checks
        )

    @property
    def github_app_verified(self) -> bool:
        passed = {
            check.name
            for check in self.checks
            if check.status is CheckStatus.PASS
        }
        return {
            "Fraeno App installation",
            "Fraeno App registration",
            "Fraeno App round trip",
        }.issubset(passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "repository": self.repository,
            "ready": self.ready,
            "live_observer_verified": self.live_observer_verified,
            "github_app_verified": self.github_app_verified,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class InitResult:
    repository: Path
    created: tuple[Path, ...]
    pull_request_url: str | None = None


def initialize_repository(
    repository: Path,
    *,
    project_name: str,
    build_command: str,
    setup_script: str,
    launch_command: str,
    required_nodes: tuple[str, ...] = (),
    required_topics: tuple[str, ...] = (),
    required_services: tuple[str, ...] = (),
    required_actions: tuple[str, ...] = (),
    required_transforms: tuple[str, ...] = (),
    required_diagnostics: tuple[str, ...] = (),
    rate_topics: tuple[str, ...] = (),
    diagnostics_topics: tuple[str, ...] = (),
    transform_topics: tuple[str, ...] = (),
    open_pull_request: bool = False,
    branch: str = "fraeno/onboarding",
    runner_image: str | None = None,
) -> InitResult:
    root = repository.resolve()
    if not root.is_dir():
        raise OnboardingError(f"repository directory does not exist at {root}")
    if not project_name.strip():
        raise OnboardingError("project name must not be empty")
    build = _parse_command(build_command, "build command")
    launch = _parse_command(launch_command, "launch command")
    if not setup_script.strip():
        raise OnboardingError("setup script must not be empty")
    if open_pull_request:
        _require_clean_git_repository(root)
        if runner_image is None:
            raise OnboardingError(
                "--runner-image is required with --open-pr so the trusted workflow "
                "can use an immutable runner"
            )
        _require_runner_digest(runner_image)
        if not branch.startswith("fraeno/") or branch == "fraeno/":
            raise OnboardingError("the onboarding branch must start with fraeno/")

    existing = _preflight_generated_paths(root)
    if existing:
        rendered = ", ".join(existing)
        raise OnboardingError(
            f"refusing to replace existing onboarding files: {rendered}"
        )

    config = _build_config(
        project_name=project_name.strip(),
        build_command=build,
        setup_script=setup_script.strip(),
        launch_command=launch,
        required_nodes=required_nodes,
        required_topics=required_topics,
        required_services=required_services,
        required_actions=required_actions,
        required_transforms=required_transforms,
        required_diagnostics=required_diagnostics,
        rate_topics=rate_topics,
        diagnostics_topics=diagnostics_topics,
        transform_topics=transform_topics,
    )
    created: list[Path] = []
    for relative in GENERATED_PATHS:
        destination = root / relative
        if relative == CONFIG_PATH:
            payload = yaml.safe_dump(
                config,
                sort_keys=False,
                width=100,
            ).encode("utf-8")
        else:
            payload = _template_bytes(relative.name)
        _write_generated_file(
            root,
            relative,
            payload,
            mode=0o755 if relative == RUNNER_PATH else 0o644,
        )
        created.append(destination)

    pull_request_url = None
    if open_pull_request:
        assert runner_image is not None
        pull_request_url = _propose_pull_request(
            root,
            branch=branch,
            runner_image=runner_image,
        )
    return InitResult(
        repository=root,
        created=tuple(created),
        pull_request_url=pull_request_url,
    )


def _preflight_generated_paths(root: Path) -> list[str]:
    existing: list[str] = []
    root_resolved = root.resolve(strict=True)
    for relative in GENERATED_PATHS:
        current = root
        for part in relative.parent.parts:
            current = current / part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                break
            except OSError as exc:
                raise OnboardingError(
                    f"cannot inspect onboarding path {current}: {exc}"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise OnboardingError(
                    f"refusing unsafe symlinked onboarding directory: {current}"
                )
            if not stat.S_ISDIR(metadata.st_mode):
                raise OnboardingError(
                    f"refusing non-directory onboarding path: {current}"
                )
            try:
                resolved = current.resolve(strict=True)
            except OSError as exc:
                raise OnboardingError(
                    f"cannot resolve onboarding directory {current}: {exc}"
                ) from exc
            if not resolved.is_relative_to(root_resolved):
                raise OnboardingError(
                    f"refusing onboarding directory outside the repository: {current}"
                )
        destination = root / relative
        try:
            metadata = destination.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise OnboardingError(
                f"cannot inspect onboarding file {destination}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise OnboardingError(
                f"refusing unsafe symlinked onboarding file: {destination}"
            )
        existing.append(relative.as_posix())
    return existing


def _write_generated_file(
    root: Path,
    relative: Path,
    payload: bytes,
    *,
    mode: int,
) -> None:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    root_fd = os.open(root, directory_flags)
    parent_fd = root_fd
    try:
        for part in relative.parent.parts:
            try:
                child_fd = os.open(
                    part,
                    directory_flags,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o755, dir_fd=parent_fd)
                try:
                    child_fd = os.open(
                        part,
                        directory_flags,
                        dir_fd=parent_fd,
                    )
                except OSError as exc:
                    raise OnboardingError(
                        f"refusing unsafe onboarding directory {relative.parent}"
                    ) from exc
            except OSError as exc:
                raise OnboardingError(
                    f"refusing unsafe onboarding directory {relative.parent}"
                ) from exc
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = child_fd

        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        try:
            file_fd = os.open(
                relative.name,
                file_flags,
                mode,
                dir_fd=parent_fd,
            )
        except FileExistsError as exc:
            raise OnboardingError(
                "refusing to replace existing onboarding file: "
                f"{relative.as_posix()}"
            ) from exc
        except OSError as exc:
            raise OnboardingError(
                f"cannot create onboarding file {relative.as_posix()}: {exc}"
            ) from exc
        try:
            with os.fdopen(file_fd, "wb", closefd=False) as generated:
                generated.write(payload)
                generated.flush()
            os.fchmod(file_fd, mode)
        finally:
            os.close(file_fd)
    finally:
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)


def doctor_repository(
    repository: Path,
    *,
    github_repository: str | None = None,
    pull_request: int | None = None,
    run_observer: bool = False,
    local_only: bool = False,
) -> DoctorReport:
    root = repository.resolve()
    checks: list[DoctorCheck] = []
    if not root.is_dir():
        return DoctorReport(
            repository=root.as_posix(),
            checks=(
                DoctorCheck(
                    "Repository",
                    CheckStatus.FAIL,
                    f"The repository directory does not exist at {root}.",
                    "Pass the path to a local repository.",
                ),
            ),
        )

    config = _check_local_files(root, checks)
    if config is not None:
        _check_target(root, checks)
        _check_commands(root, config, checks)
        _check_docker(checks)
        _check_observer(root, config, checks, run_observer=run_observer)

    if local_only:
        checks.append(
            DoctorCheck(
                "GitHub",
                CheckStatus.WARNING,
                "GitHub checks were skipped.",
                "Run fraeno doctor without --local-only before the first test pull request.",
            )
        )
    else:
        _check_github(
            root,
            checks,
            github_repository=github_repository,
            pull_request=pull_request,
        )
    return DoctorReport(repository=root.as_posix(), checks=tuple(checks))


def _build_config(
    *,
    project_name: str,
    build_command: tuple[str, ...],
    setup_script: str,
    launch_command: tuple[str, ...],
    required_nodes: tuple[str, ...],
    required_topics: tuple[str, ...],
    required_services: tuple[str, ...],
    required_actions: tuple[str, ...],
    required_transforms: tuple[str, ...],
    required_diagnostics: tuple[str, ...],
    rate_topics: tuple[str, ...],
    diagnostics_topics: tuple[str, ...],
    transform_topics: tuple[str, ...],
) -> dict[str, Any]:
    measured_topics = tuple(dict.fromkeys((*rate_topics, *required_topics)))
    contract: dict[str, Any] = {
        "required_nodes": list(dict.fromkeys(required_nodes)),
        "required_topics": list(dict.fromkeys(required_topics)),
        "required_services": list(dict.fromkeys(required_services)),
        "required_actions": list(dict.fromkeys(required_actions)),
        "required_transforms": list(dict.fromkeys(required_transforms)),
        "required_diagnostics": list(dict.fromkeys(required_diagnostics)),
        "maximum_topic_rate_regression_percent": 30,
    }
    return {
        "version": 1,
        "project": {"name": project_name},
        "target": {
            "ros_distribution": SUPPORTED_TARGET.ros_distribution,
            "operating_system": SUPPORTED_TARGET.operating_system,
            "operating_system_version": SUPPORTED_TARGET.operating_system_version,
            "architecture": SUPPORTED_TARGET.architecture,
        },
        "updates": {
            "update_types": [
                "major",
                "minor",
                "patch",
                "digest",
                "revision",
                "unknown",
            ],
            "cooldown_days": 7,
            "schedule": {"interval": "weekly", "day": "monday"},
            "max_open_pull_requests": 5,
        },
        "validation": {
            "steps": [
                {
                    "name": "build ROS 2 workspace",
                    "command": ["bash", "-lc", shlex.join(build_command)],
                    "timeout_seconds": 600,
                }
            ],
            "observe": {
                "command": [
                    "bash",
                    "-lc",
                    (
                        f"source {shlex.quote(setup_script)} && "
                        "python3 -m fraeno.cli observe-ros2 "
                        '--config "$FRAENO_TRUSTED_ROOT/.fraeno.yml"'
                    ),
                ],
                "timeout_seconds": 120,
                "ros2": {
                    "launch_command": list(launch_command),
                    "warmup_seconds": 2,
                    "graph_stabilization_timeout_seconds": 15,
                    "graph_stabilization_interval_seconds": 0.25,
                    "graph_stabilization_samples": 3,
                    "sample_seconds": 5,
                    "measurement_repetitions": 3,
                    "rate_topics": list(measured_topics),
                    "diagnostics_topics": list(
                        dict.fromkeys(diagnostics_topics)
                    ),
                    "transform_topics": list(dict.fromkeys(transform_topics)),
                    "shutdown_timeout_seconds": 5,
                },
            },
            "contract": contract,
        },
    }


def _template_bytes(name: str) -> bytes:
    packaged = (
        resources.files("fraeno")
        .joinpath("templates")
        .joinpath("github")
        .joinpath(name)
    )
    if packaged.is_file():
        return packaged.read_bytes()
    source = Path(__file__).resolve().parents[2] / "templates" / "github" / name
    if not source.is_file():
        raise OnboardingError(f"Fraeno package is missing onboarding template {name}")
    return source.read_bytes()


def _parse_command(command: str, field_name: str) -> tuple[str, ...]:
    try:
        parsed = tuple(shlex.split(command))
    except ValueError as error:
        raise OnboardingError(f"{field_name} is invalid: {error}") from error
    if not parsed:
        raise OnboardingError(f"{field_name} must not be empty")
    return parsed


def _require_runner_digest(image: str) -> None:
    if RUNNER_IMAGE_PATTERN.fullmatch(image) is None:
        raise OnboardingError(
            "runner image must include a full immutable @sha256 digest"
        )


def _require_clean_git_repository(root: Path) -> None:
    git = shutil.which("git")
    gh = shutil.which("gh")
    if git is None:
        raise OnboardingError("git is required to open the onboarding pull request")
    if gh is None:
        raise OnboardingError("GitHub CLI is required to open the onboarding pull request")
    top = _run((git, "rev-parse", "--show-toplevel"), cwd=root)
    if top.returncode != 0:
        raise OnboardingError("the target must be a Git repository for --open-pr")
    if Path(top.stdout.strip()).resolve() != root:
        raise OnboardingError("--open-pr must target the root of the Git repository")
    status = _run((git, "status", "--porcelain"), cwd=root)
    if status.returncode != 0:
        raise OnboardingError("git could not inspect the repository working tree")
    if status.stdout.strip():
        raise OnboardingError(
            "the repository must have no uncommitted changes before --open-pr"
        )
    auth = _run((gh, "auth", "status"), cwd=root)
    if auth.returncode != 0:
        raise OnboardingError("GitHub CLI is not signed in")


def _propose_pull_request(root: Path, *, branch: str, runner_image: str) -> str:
    git = shutil.which("git")
    gh = shutil.which("gh")
    assert git is not None
    assert gh is not None
    steps = (
        (git, "switch", "-c", branch),
        (
            git,
            "add",
            "--",
            *(path.as_posix() for path in GENERATED_PATHS),
        ),
        (git, "commit", "-m", "Set up Fraeno"),
        (git, "push", "--set-upstream", "origin", branch),
    )
    for command in steps:
        result = _run(command, cwd=root)
        if result.returncode != 0:
            raise OnboardingError(
                f"{' '.join(command[:2])} failed. The generated files remain in {root}."
            )
    variable = _run(
        (
            gh,
            "variable",
            "set",
            "FRAENO_RUNNER_IMAGE",
            "--body",
            runner_image,
        ),
        cwd=root,
    )
    if variable.returncode != 0:
        raise OnboardingError(
            "the onboarding branch was pushed, but the FRAENO_RUNNER_IMAGE "
            "repository variable could not be set"
        )
    pull_request = _run(
        (
            gh,
            "pr",
            "create",
            "--draft",
            "--title",
            "Set up Fraeno",
            "--body",
            (
                "This adds the trusted Fraeno contract and validation workflow.\n\n"
                "After review, merge this pull request, install the Fraeno App on this "
                "repository, then open the documented first test pull request."
            ),
        ),
        cwd=root,
    )
    if pull_request.returncode != 0:
        raise OnboardingError(
            "the onboarding branch was pushed, but GitHub could not create the pull request"
        )
    url = pull_request.stdout.strip().splitlines()[-1]
    if not url.startswith("https://github.com/"):
        raise OnboardingError("GitHub did not return the onboarding pull request URL")
    return url


def _run(
    command: tuple[str, ...],
    *,
    cwd: Path | None = None,
    timeout: int = 30,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(command, 127, "", str(error))


def _check_local_files(
    root: Path,
    checks: list[DoctorCheck],
) -> FraenoConfig | None:
    for path in GENERATED_PATHS:
        absolute = root / path
        if not absolute.is_file():
            checks.append(
                DoctorCheck(
                    f"Required file {path.as_posix()}",
                    CheckStatus.FAIL,
                    f"{path.as_posix()} is missing.",
                    "Run fraeno init to generate the trusted onboarding files.",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    f"Required file {path.as_posix()}",
                    CheckStatus.PASS,
                    f"{path.as_posix()} exists.",
                )
            )
    if not (root / CONFIG_PATH).is_file():
        return None
    try:
        config = load_config(root / CONFIG_PATH)
    except (ConfigError, OSError, yaml.YAMLError) as error:
        checks.append(
            DoctorCheck(
                "Fraeno config",
                CheckStatus.FAIL,
                f"{CONFIG_PATH.as_posix()} is invalid. {error}",
                "Correct the named field, then run fraeno doctor again.",
            )
        )
        return None
    checks.append(
        DoctorCheck(
            "Fraeno config",
            CheckStatus.PASS,
            f"The contract for {config.project_name} is valid.",
        )
    )
    runner = root / RUNNER_PATH
    if runner.is_file() and not runner.stat().st_mode & 0o111:
        checks.append(
            DoctorCheck(
                "Runner script",
                CheckStatus.FAIL,
                f"{RUNNER_PATH.as_posix()} is not executable.",
                f"Run chmod +x {RUNNER_PATH.as_posix()} and commit the mode change.",
            )
        )
    elif runner.is_file():
        checks.append(
            DoctorCheck(
                "Runner script",
                CheckStatus.PASS,
                "The trusted runner script is executable.",
            )
        )
    return config


def _check_target(root: Path, checks: list[DoctorCheck]) -> None:
    try:
        raw = yaml.safe_load((root / CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        checks.append(
            DoctorCheck(
                "Target",
                CheckStatus.FAIL,
                f"The target could not be read. {error}",
            )
        )
        return
    target_raw = raw.get("target") if isinstance(raw, dict) else None
    if not isinstance(target_raw, dict):
        checks.append(
            DoctorCheck(
                "Target",
                CheckStatus.FAIL,
                "The target section is missing from .fraeno.yml.",
                "Add ROS Humble, Ubuntu 22.04, and amd64 as the target.",
            )
        )
        return
    configured = TargetPlatform(
        ros_distribution=str(target_raw.get("ros_distribution", "unknown")),
        operating_system=str(target_raw.get("operating_system", "unknown")),
        operating_system_version=str(
            target_raw.get("operating_system_version", "unknown")
        ),
        architecture=str(target_raw.get("architecture", "unknown")),
    )
    if configured != SUPPORTED_TARGET:
        checks.append(
            DoctorCheck(
                "Target",
                CheckStatus.FAIL,
                (
                    "Fraeno v1 supports ROS Humble on Ubuntu 22.04 amd64, but "
                    f".fraeno.yml declares {_render_target(configured)}."
                ),
                "Use a supported target or stop before installing Fraeno.",
            )
        )
        return
    report = RepositoryScanner(root).scan()
    inferred = infer_target_platform(report)
    conflicts = [
        f"{field.replace('_', ' ')} {actual}"
        for field in (
            "ros_distribution",
            "operating_system",
            "operating_system_version",
            "architecture",
        )
        if (actual := getattr(inferred, field)) != "unknown"
        and actual != getattr(configured, field)
    ]
    if conflicts:
        checks.append(
            DoctorCheck(
                "Target",
                CheckStatus.FAIL,
                "Repository evidence conflicts with .fraeno.yml: " + ", ".join(conflicts),
                "Correct the target or the container base image before continuing.",
            )
        )
        return
    checks.append(
        DoctorCheck(
            "Target",
            CheckStatus.PASS,
            "The repository declares the supported ROS Humble Ubuntu 22.04 amd64 target.",
        )
    )


def _render_target(target: TargetPlatform) -> str:
    return (
        f"ROS {target.ros_distribution}, {target.operating_system} "
        f"{target.operating_system_version}, {target.architecture}"
    )


def _check_commands(
    root: Path,
    config: FraenoConfig,
    checks: list[DoctorCheck],
) -> None:
    executables: list[tuple[str, str]] = []
    for step in config.validation.steps:
        executables.append((f"Build command {step.name}", step.command[0]))
    if config.validation.ros2_observer is not None:
        executables.append(
            ("Launch command", config.validation.ros2_observer.launch_command[0])
        )
    executables.append(("Observer command", config.validation.observation_command[0]))
    for name, executable in executables:
        location = shutil.which(executable)
        if location is None:
            checks.append(
                DoctorCheck(
                    name,
                    CheckStatus.FAIL,
                    f"The required command {executable} is not on PATH.",
                    f"Install {executable} in the local target environment.",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name,
                    CheckStatus.PASS,
                    f"{executable} is available at {location}.",
                )
            )
    setup_script = _setup_script(config.validation.observation_command)
    if setup_script is None:
        checks.append(
            DoctorCheck(
                "ROS workspace",
                CheckStatus.FAIL,
                "The observation command does not source a ROS workspace setup file.",
                "Run fraeno init or source the built workspace before the observer.",
            )
        )
    elif not (root / setup_script).is_file():
        checks.append(
            DoctorCheck(
                "ROS workspace",
                CheckStatus.FAIL,
                f"The required setup file {setup_script.as_posix()} is missing.",
                "Run the configured build command, then run fraeno doctor again.",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "ROS workspace",
                CheckStatus.PASS,
                f"{setup_script.as_posix()} is ready.",
            )
        )


def _setup_script(command: tuple[str, ...]) -> Path | None:
    shell = command[-1]
    match = re.search(r"(?:^|&&|\s)source\s+(['\"]?)([^'\";&\s]+)\1", shell)
    if match is None:
        return None
    path = Path(match.group(2))
    if path.is_absolute() or ".." in path.parts:
        return None
    return path


def _check_docker(checks: list[DoctorCheck]) -> None:
    docker = shutil.which("docker")
    if docker is None:
        checks.append(
            DoctorCheck(
                "Docker",
                CheckStatus.FAIL,
                "The docker command is not on PATH.",
                "Install Docker and start its service.",
            )
        )
        return
    result = _run((docker, "version", "--format", "{{.Server.Version}}"), timeout=15)
    if result.returncode != 0 or not result.stdout.strip():
        checks.append(
            DoctorCheck(
                "Docker",
                CheckStatus.FAIL,
                "Docker is installed, but its service is not reachable.",
                "Start Docker, then run fraeno doctor again.",
            )
        )
        return
    checks.append(
        DoctorCheck(
            "Docker",
            CheckStatus.PASS,
            f"Docker service {result.stdout.strip()} is reachable.",
        )
    )


def _check_observer(
    root: Path,
    config: FraenoConfig,
    checks: list[DoctorCheck],
    *,
    run_observer: bool,
) -> None:
    observer = config.validation.ros2_observer
    if observer is None:
        checks.append(
            DoctorCheck(
                "Observer",
                CheckStatus.FAIL,
                "validation.observe.ros2 is missing.",
                "Run fraeno init to configure the generic ROS 2 observer.",
            )
        )
        return
    if not run_observer:
        checks.append(
            DoctorCheck(
                "Observer",
                CheckStatus.WARNING,
                "The observer contract is valid. Fraeno did not launch the robot.",
                "Run fraeno doctor --run-observer when it is safe to launch the target.",
            )
        )
        return
    environment = dict(os.environ)
    environment["FRAENO_TRUSTED_ROOT"] = root.as_posix()
    result = _run(
        config.validation.observation_command,
        cwd=root,
        timeout=config.validation.observation_timeout_seconds,
        env=environment,
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""
        checks.append(
            DoctorCheck(
                "Observer",
                CheckStatus.FAIL,
                "The live observer did not complete." + (f" {detail}" if detail else ""),
                "Fix the launch or observer error, then run this command again.",
            )
        )
        return
    try:
        raw = json.loads(result.stdout)
        if not isinstance(raw, dict):
            raise ObservationError("observer output must be an object")
        observation = SystemObservation.from_dict(raw)
    except (json.JSONDecodeError, ObservationError) as error:
        checks.append(
            DoctorCheck(
                "Observer",
                CheckStatus.FAIL,
                f"The live observer returned invalid evidence. {error}",
                "Run the configured observation command and correct its JSON output.",
            )
        )
        return
    if not observation.healthy or observation.infrastructure_errors:
        errors = "; ".join(observation.infrastructure_errors)
        checks.append(
            DoctorCheck(
                "Observer",
                CheckStatus.FAIL,
                "The live observer reported an unhealthy target."
                + (f" {errors}" if errors else ""),
                (
                    "Resolve the reported robot or evidence problem before the "
                    "first test pull request."
                ),
            )
        )
        return
    checks.append(
        DoctorCheck(
            "Observer",
            CheckStatus.PASS,
            (
                "The live observer completed with "
                f"{len(observation.nodes)} nodes and {len(observation.topics)} topics."
            ),
        )
    )


def _check_github(
    root: Path,
    checks: list[DoctorCheck],
    *,
    github_repository: str | None,
    pull_request: int | None,
) -> None:
    gh = shutil.which("gh")
    if gh is None:
        checks.append(
            DoctorCheck(
                "GitHub",
                CheckStatus.FAIL,
                "GitHub CLI is not installed.",
                "Install GitHub CLI and sign in, then run fraeno doctor again.",
            )
        )
        return
    auth = _run((gh, "auth", "status"), cwd=root)
    if auth.returncode != 0:
        checks.append(
            DoctorCheck(
                "GitHub",
                CheckStatus.FAIL,
                "GitHub CLI is not signed in.",
                "Run gh auth login, then run fraeno doctor again.",
            )
        )
        return
    repository = github_repository
    if repository is None:
        resolved = _run(
            (gh, "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"),
            cwd=root,
        )
        if resolved.returncode == 0:
            repository = resolved.stdout.strip()
    if not repository or repository.count("/") != 1:
        checks.append(
            DoctorCheck(
                "GitHub repository",
                CheckStatus.FAIL,
                "Fraeno could not identify an owner/repository name.",
                "Pass --github-repository OWNER/REPOSITORY.",
            )
        )
        return
    metadata = _gh_api_json(gh, f"repos/{repository}", root)
    if metadata is None:
        checks.append(
            DoctorCheck(
                "GitHub repository",
                CheckStatus.FAIL,
                f"GitHub repository {repository} is not accessible.",
                "Confirm the repository name and GitHub CLI access.",
            )
        )
        return
    default_branch = metadata.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        checks.append(
            DoctorCheck(
                "GitHub repository",
                CheckStatus.FAIL,
                f"GitHub did not return the default branch for {repository}.",
            )
        )
        return
    checks.append(
        DoctorCheck(
            "GitHub repository",
            CheckStatus.PASS,
            f"{repository} is accessible and uses {default_branch} as its default branch.",
        )
    )
    _check_remote_files(gh, root, repository, default_branch, checks)
    _check_runner_variable(gh, root, repository, checks)
    _check_app(gh, root, repository, pull_request, checks)


def _check_remote_files(
    gh: str,
    root: Path,
    repository: str,
    default_branch: str,
    checks: list[DoctorCheck],
) -> None:
    missing: list[str] = []
    for path in GENERATED_PATHS:
        payload = _gh_api_json(
            gh,
            f"repos/{repository}/contents/{path.as_posix()}?ref={default_branch}",
            root,
        )
        if payload is None:
            missing.append(path.as_posix())
    if missing:
        checks.append(
            DoctorCheck(
                "Trusted files on GitHub",
                CheckStatus.FAIL,
                "The default branch is missing " + ", ".join(missing) + ".",
                "Review and merge the Fraeno onboarding pull request first.",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "Trusted files on GitHub",
                CheckStatus.PASS,
                "The default branch contains every trusted Fraeno file.",
            )
        )


def _check_runner_variable(
    gh: str,
    root: Path,
    repository: str,
    checks: list[DoctorCheck],
) -> None:
    payload = _gh_api_json(
        gh,
        f"repos/{repository}/actions/variables/FRAENO_RUNNER_IMAGE",
        root,
    )
    value = payload.get("value") if payload is not None else None
    if not isinstance(value, str):
        checks.append(
            DoctorCheck(
                "Runner image",
                CheckStatus.FAIL,
                "The FRAENO_RUNNER_IMAGE repository variable is missing.",
                "Set it to the published Fraeno runner with its full @sha256 digest.",
            )
        )
    elif RUNNER_IMAGE_PATTERN.fullmatch(value) is None:
        checks.append(
            DoctorCheck(
                "Runner image",
                CheckStatus.FAIL,
                "FRAENO_RUNNER_IMAGE is not pinned by a full @sha256 digest.",
                "Replace the variable value with an immutable Fraeno runner digest.",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "Runner image",
                CheckStatus.PASS,
                f"FRAENO_RUNNER_IMAGE is pinned to {value.rsplit('@', maxsplit=1)[1]}.",
            )
        )


def _check_app(
    gh: str,
    root: Path,
    repository: str,
    pull_request: int | None,
    checks: list[DoctorCheck],
) -> None:
    selected_pull_request = pull_request
    if selected_pull_request is None:
        listed = _run(
            (
                gh,
                "pr",
                "list",
                "--repo",
                repository,
                "--state",
                "open",
                "--limit",
                "10",
                "--json",
                "number,headRefOid",
            ),
            cwd=root,
        )
        if listed.returncode == 0:
            try:
                pull_requests = json.loads(listed.stdout)
            except json.JSONDecodeError:
                pull_requests = []
            if isinstance(pull_requests, list) and pull_requests:
                number = pull_requests[0].get("number")
                if isinstance(number, int):
                    selected_pull_request = number
    if selected_pull_request is None:
        checks.append(
            DoctorCheck(
                "Fraeno App installation",
                CheckStatus.FAIL,
                "There is no open pull request that can prove the Fraeno App installation.",
                (
                    f"Install Fraeno from {APP_URL}, open the first test pull request, "
                    "then run doctor again."
                ),
            )
        )
        return
    pull = _gh_api_json(
        gh,
        f"repos/{repository}/pulls/{selected_pull_request}",
        root,
    )
    head = pull.get("head") if pull is not None else None
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or not sha:
        checks.append(
            DoctorCheck(
                "Fraeno App installation",
                CheckStatus.FAIL,
                f"Pull request #{selected_pull_request} could not be read.",
                "Confirm the pull request number and GitHub access.",
            )
        )
        return
    payload = _gh_api_json(
        gh,
        f"repos/{repository}/commits/{sha}/check-runs?filter=all&per_page=100",
        root,
    )
    raw_runs = payload.get("check_runs") if payload is not None else None
    runs = raw_runs if isinstance(raw_runs, list) else []
    app_run = next(
        (
            run
            for run in runs
            if isinstance(run, dict)
            and run.get("name") == CHECK_NAME
            and isinstance(run.get("app"), dict)
            and run["app"].get("slug") == APP_SLUG
        ),
        None,
    )
    if app_run is None:
        checks.append(
            DoctorCheck(
                "Fraeno App installation",
                CheckStatus.FAIL,
                (
                    f"Pull request #{selected_pull_request} has no {CHECK_NAME} check "
                    f"from {APP_SLUG}."
                ),
                (
                    f"Install or grant this repository to Fraeno at {APP_URL}, then "
                    "reopen the pull request."
                ),
            )
        )
        return
    checks.append(
        DoctorCheck(
            "Fraeno App installation",
            CheckStatus.PASS,
            f"Fraeno received pull request #{selected_pull_request} and created its check.",
        )
    )
    status = app_run.get("status")
    conclusion = app_run.get("conclusion")
    details_url = app_run.get("details_url")
    expected_run_path = f"/{repository}/actions/runs/"
    completed_round_trip = (
        status == "completed"
        and isinstance(conclusion, str)
        and bool(conclusion)
        and isinstance(details_url, str)
        and details_url.startswith("https://")
        and expected_run_path in details_url
    )
    if completed_round_trip:
        checks.append(
            DoctorCheck(
                "Fraeno App round trip",
                CheckStatus.PASS,
                (
                    "Fraeno dispatched the trusted workflow and received its result "
                    f"for pull request #{selected_pull_request}."
                ),
            )
        )
        if conclusion == "success":
            checks.append(
                DoctorCheck(
                    "Robot integration result",
                    CheckStatus.PASS,
                    f"The {CHECK_NAME} check passed.",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    "Robot integration result",
                    CheckStatus.FAIL,
                    f"The {CHECK_NAME} check completed with {conclusion}.",
                    "Open the workflow result and fix the reported robot regression.",
                )
            )
    else:
        checks.append(
            DoctorCheck(
                "Fraeno App round trip",
                CheckStatus.FAIL,
                (
                    f"The {CHECK_NAME} check on pull request "
                    f"#{selected_pull_request} has not completed successfully through "
                    "the trusted workflow."
                ),
                (
                    "Open the check, fix the workflow or installation access it names, "
                    "then run doctor again."
                ),
            )
        )

    app = app_run["app"]
    permissions = app.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    missing_permissions = [
        f"{name}: {level}"
        for name, level in REQUIRED_APP_PERMISSIONS.items()
        if not _permission_satisfies(permissions.get(name), level)
    ]
    events = app.get("events")
    actual_events = set(events) if isinstance(events, list) else set()
    missing_events = sorted(REQUIRED_APP_EVENTS - actual_events)
    if missing_permissions or missing_events:
        details: list[str] = []
        if missing_permissions:
            details.append("permissions " + ", ".join(missing_permissions))
        if missing_events:
            details.append("events " + ", ".join(missing_events))
        checks.append(
            DoctorCheck(
                "Fraeno App registration",
                CheckStatus.FAIL,
                "The Fraeno App registration is missing " + "; ".join(details) + ".",
                (
                    "Update the Fraeno App registration, then have every installation "
                    "owner accept its requested access."
                ),
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "Fraeno App registration",
                CheckStatus.PASS,
                (
                    "The Fraeno App registration requests every required permission "
                    "and event. Registration metadata does not prove that this "
                    "repository installation accepted that access."
                ),
            )
        )


def _permission_satisfies(actual: Any, required: str) -> bool:
    levels = {"none": 0, "read": 1, "write": 2}
    if not isinstance(actual, str):
        return False
    return levels.get(actual, -1) >= levels[required]


def _gh_api_json(gh: str, endpoint: str, root: Path) -> dict[str, Any] | None:
    result = _run(
        (
            gh,
            "api",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            endpoint,
        ),
        cwd=root,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
