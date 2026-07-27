from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fraeno import __version__
from fraeno.config import ConfigError, load_config
from fraeno.dependency_graph import TargetPlatform, infer_target_platform
from fraeno.lockfile import (
    build_lockfile,
    compare_lockfiles,
    read_lockfile,
    write_lockfile,
)
from fraeno.models import ScanReport
from fraeno.onboarding import (
    CheckStatus,
    doctor_repository,
    initialize_repository,
)
from fraeno.scanner import RepositoryScanner
from fraeno.update_discovery import FixtureUpdateCatalog, discover_updates
from fraeno.update_policy import OpenUpdatePullRequest, plan_updates
from fraeno.updates import (
    apply_next_update,
    apply_update,
    apply_updates,
)
from fraeno.validation.compare import Outcome, compare_systems
from fraeno.validation.contract import CapturedWorkspace, assemble_validation
from fraeno.validation.observation import ObservationError, SystemObservation
from fraeno.validation.ros2_observer import Ros2ObservationError, observe_ros2
from fraeno.validation.runner import run_validation, run_workspace
from fraeno.validation.sandbox import disposable_workspace, require_protected_output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fraeno",
        description="Manage robot dependencies and validate complete-system behavior.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="Scan a repository dependency graph.")
    scan.add_argument("repository", nargs="?", default=".")
    scan.add_argument("--output", "-o", type=Path)
    scan.add_argument("--lock", type=Path)
    _add_target_options(scan)

    lock = commands.add_parser("lock", help="Write a dependency lock snapshot.")
    lock.add_argument("repository", nargs="?", default=".")
    lock.add_argument("--output", "-o", type=Path, default=Path("fraeno.lock.json"))
    _add_target_options(lock)

    compare_locks = commands.add_parser(
        "compare-locks",
        help="Explain dependency changes between two Fraeno locks.",
    )
    compare_locks.add_argument("--baseline", type=Path, required=True)
    compare_locks.add_argument("--candidate", type=Path, required=True)
    compare_locks.add_argument("--output", "-o", type=Path)

    outdated = commands.add_parser(
        "outdated", help="Find supported dependency updates."
    )
    outdated.add_argument("repository", nargs="?", default=".")
    outdated.add_argument("--output", "-o", type=Path)
    outdated.add_argument("--catalog", type=Path)
    _add_target_options(outdated)

    update = commands.add_parser(
        "update", help="Apply one supported dependency update."
    )
    update.add_argument("repository", nargs="?", default=".")
    update.add_argument("--dependency", required=True)
    update.add_argument("--to", required=True)
    update.add_argument("--dry-run", action="store_true")
    update.add_argument("--output", "-o", type=Path)

    update_next = commands.add_parser(
        "update-next",
        help="Find and apply the next supported dependency update.",
    )
    update_next.add_argument("repository", nargs="?", default=".")
    update_next.add_argument("--dry-run", action="store_true")
    update_next.add_argument("--output", "-o", type=Path)
    update_next.add_argument("--catalog", type=Path)
    _add_target_options(update_next)

    propose_update = commands.add_parser(
        "propose-update",
        help="Apply the next policy-approved update proposal.",
    )
    propose_update.add_argument("repository", nargs="?", default=".")
    propose_update.add_argument(
        "--config",
        type=Path,
        default=Path(".fraeno.yml"),
    )
    propose_update.add_argument("--open-pull-requests", type=Path)
    propose_update.add_argument("--catalog", type=Path)
    propose_update.add_argument("--now")
    propose_update.add_argument("--ignore-schedule", action="store_true")
    propose_update.add_argument("--dry-run", action="store_true")
    propose_update.add_argument("--output", "-o", type=Path)
    _add_target_options(propose_update)

    compare = commands.add_parser(
        "compare", help="Compare two robot-system observation snapshots."
    )
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--config", type=Path, required=True)
    compare.add_argument("--output", "-o", type=Path)

    validate = commands.add_parser(
        "validate", help="Run and compare baseline and candidate systems."
    )
    validate.add_argument("--baseline", type=Path, required=True)
    validate.add_argument("--candidate", type=Path, required=True)
    validate.add_argument("--config", type=Path, required=True)
    validate.add_argument(
        "--output", "-o", type=Path, default=Path("fraeno-report.json")
    )

    capture = commands.add_parser(
        "capture-workspace",
        help="Capture one workspace in an isolated runner container.",
    )
    capture.add_argument("--source", type=Path, required=True)
    capture.add_argument("--config", type=Path, required=True)
    capture.add_argument("--phase", choices=("baseline", "candidate"), required=True)
    capture.add_argument("--output", "-o", type=Path, required=True)
    capture.add_argument("--run-as-uid", type=int, default=65532)
    capture.add_argument("--run-as-gid", type=int, default=65532)
    capture.add_argument("--trusted-root", type=Path)

    assemble = commands.add_parser(
        "assemble-report",
        help="Compare protected baseline and candidate evidence.",
    )
    assemble.add_argument("--baseline", type=Path, required=True)
    assemble.add_argument("--candidate", type=Path, required=True)
    assemble.add_argument("--config", type=Path, required=True)
    assemble.add_argument(
        "--output", "-o", type=Path, default=Path("fraeno-report.json")
    )

    observe_ros2_command = commands.add_parser(
        "observe-ros2",
        help="Launch and observe a configured ROS 2 system.",
    )
    observe_ros2_command.add_argument(
        "--config",
        type=Path,
        default=Path(".fraeno.yml"),
    )
    observe_ros2_command.add_argument("--output", "-o", type=Path)

    init = commands.add_parser(
        "init",
        help="Create the trusted files needed to add Fraeno to a robot repository.",
    )
    init.add_argument("repository", nargs="?", default=".", type=Path)
    init.add_argument("--project-name")
    init.add_argument(
        "--build-command",
        default="colcon build --event-handlers console_direct+",
    )
    init.add_argument("--setup-script", default="install/setup.bash")
    init.add_argument("--launch-command", required=True)
    init.add_argument("--required-node", action="append", default=[])
    init.add_argument("--required-topic", action="append", default=[])
    init.add_argument("--required-service", action="append", default=[])
    init.add_argument("--required-action", action="append", default=[])
    init.add_argument("--required-transform", action="append", default=[])
    init.add_argument("--required-diagnostic", action="append", default=[])
    init.add_argument("--rate-topic", action="append", default=[])
    init.add_argument("--diagnostics-topic", action="append", default=[])
    init.add_argument("--transform-topic", action="append", default=[])
    init.add_argument(
        "--open-pr",
        action="store_true",
        help="Commit and push a dedicated branch, then open a draft pull request.",
    )
    init.add_argument("--branch", default="fraeno/onboarding")
    init.add_argument("--runner-image")

    doctor = commands.add_parser(
        "doctor",
        help="Check local and GitHub requirements before the first Fraeno run.",
    )
    doctor.add_argument("repository", nargs="?", default=".", type=Path)
    doctor.add_argument("--github-repository")
    doctor.add_argument("--pull-request", type=int)
    doctor.add_argument(
        "--run-observer",
        action="store_true",
        help="Launch the configured target and require healthy observer evidence.",
    )
    doctor.add_argument(
        "--local-only",
        action="store_true",
        help="Skip GitHub checks while preparing files locally.",
    )
    doctor.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            return _scan(args)
        if args.command == "lock":
            return _lock(args)
        if args.command == "compare-locks":
            return _compare_locks(args)
        if args.command == "outdated":
            return _outdated(args)
        if args.command == "update":
            return _update(args)
        if args.command == "update-next":
            return _update_next(args)
        if args.command == "propose-update":
            return _propose_update(args)
        if args.command == "compare":
            return _compare(args)
        if args.command == "validate":
            return _validate(args)
        if args.command == "capture-workspace":
            return _capture_workspace(args)
        if args.command == "assemble-report":
            return _assemble_report(args)
        if args.command == "observe-ros2":
            return _observe_ros2(args)
        if args.command == "init":
            return _init(args)
        if args.command == "doctor":
            return _doctor(args)
    except (
        ConfigError,
        ObservationError,
        Ros2ObservationError,
        OSError,
        ValueError,
    ) as error:
        print(f"fraeno: {error}", file=sys.stderr)
        return 2
    return 2


def _scan(args: argparse.Namespace) -> int:
    root = Path(args.repository).resolve()
    report = RepositoryScanner(root).scan()
    payload = report.to_dict()
    _write_or_print(payload, args.output)
    if args.lock:
        write_lockfile(
            build_lockfile(report, root, target=_target_from_args(args, report)),
            args.lock,
        )
    return 0 if not report.warnings else 1


def _lock(args: argparse.Namespace) -> int:
    root = Path(args.repository).resolve()
    report = RepositoryScanner(root).scan()
    write_lockfile(
        build_lockfile(report, root, target=_target_from_args(args, report)),
        args.output,
    )
    print(args.output)
    return 0 if not report.warnings else 1


def _compare_locks(args: argparse.Namespace) -> int:
    baseline = read_lockfile(args.baseline)
    candidate = read_lockfile(args.candidate)
    comparison = compare_lockfiles(baseline, candidate)
    _write_or_print(comparison, args.output)
    return 0


def _outdated(args: argparse.Namespace) -> int:
    root = Path(args.repository).resolve()
    report = RepositoryScanner(root).scan()
    catalog = FixtureUpdateCatalog.from_path(args.catalog) if args.catalog else None
    discovery = discover_updates(
        report,
        target=_target_from_args(args, report),
        catalog=catalog,
    )
    payload = discovery.to_dict()
    payload["warnings"] = report.warnings + list(discovery.warnings)
    _write_or_print(payload, args.output)
    return 0


def _update(args: argparse.Namespace) -> int:
    root = Path(args.repository).resolve()
    report = RepositoryScanner(root).scan()
    result = apply_update(
        root,
        report,
        args.dependency,
        args.to,
        dry_run=args.dry_run,
    )
    _write_or_print(result.to_dict(), args.output)
    return 0


def _update_next(args: argparse.Namespace) -> int:
    root = Path(args.repository).resolve()
    report = RepositoryScanner(root).scan()
    catalog = FixtureUpdateCatalog.from_path(args.catalog) if args.catalog else None
    result, discovery = apply_next_update(
        root,
        report,
        target=_target_from_args(args, report),
        catalog=catalog,
        dry_run=args.dry_run,
    )
    _write_or_print(
        {
            "schema_version": 2,
            "updated": result is not None,
            "update": result.to_dict() if result else None,
            "candidates": [item.to_dict() for item in discovery.candidates],
            "refusals": [item.to_dict() for item in discovery.refusals],
            "warnings": report.warnings + list(discovery.warnings),
        },
        args.output,
    )
    return 0


def _propose_update(args: argparse.Namespace) -> int:
    root = Path(args.repository).resolve()
    config_path = args.config
    if not config_path.is_absolute():
        config_path = root / config_path
    config = load_config(config_path)
    report = RepositoryScanner(root).scan()
    catalog = FixtureUpdateCatalog.from_path(args.catalog) if args.catalog else None
    discovery = discover_updates(
        report,
        target=_target_from_args(args, report),
        catalog=catalog,
    )
    now = _parse_now(args.now)
    open_pull_requests = _open_pull_requests(args.open_pull_requests)
    plan = plan_updates(
        discovery.candidates,
        config.updates,
        now=now,
        open_pull_requests=open_pull_requests,
        ignore_schedule=args.ignore_schedule,
    )
    proposal = plan.proposals[0] if plan.proposals else None
    results = (
        apply_updates(
            root,
            report,
            tuple(
                (candidate.identity, candidate.target)
                for candidate in proposal.candidates
            ),
            dry_run=args.dry_run,
        )
        if proposal
        else ()
    )
    proposal_payload = proposal.to_dict(config.validation) if proposal else None
    if proposal_payload is not None:
        proposal_payload["results"] = [result.to_dict() for result in results]
    _write_or_print(
        {
            "schema_version": 3,
            "updated": bool(results),
            "dry_run": bool(args.dry_run),
            "proposal": proposal_payload,
            "plan": plan.to_dict(config.validation),
            "refusals": [item.to_dict() for item in discovery.refusals],
            "warnings": report.warnings + list(discovery.warnings),
        },
        args.output,
    )
    return 0


def _compare(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    baseline = _observation_from_file(args.baseline)
    candidate = _observation_from_file(args.candidate)
    report = compare_systems(baseline, candidate, config.validation)
    _write_or_print(report.to_dict(), args.output)
    return _outcome_code(report.outcome)


def _validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    result = run_validation(args.baseline, args.candidate, config)
    _write_or_print(result.to_dict(), args.output)
    print(f"Fraeno result: {result.outcome.value}")
    return _outcome_code(result.outcome)


def _capture_workspace(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    output = args.output.resolve()
    require_protected_output(
        output,
        command_uid=args.run_as_uid,
        command_gid=args.run_as_gid,
    )
    with disposable_workspace(
        args.source,
        command_uid=args.run_as_uid,
        command_gid=args.run_as_gid,
    ) as workspace:
        run = run_workspace(
            workspace,
            config,
            args.phase,
            command_uid=args.run_as_uid,
            command_gid=args.run_as_gid,
            trusted_workspace=args.trusted_root,
        )
    _write_json(
        CapturedWorkspace(engine_version=__version__, run=run).to_dict(),
        output,
    )
    os.chmod(output, 0o600)
    return 0


def _assemble_report(args: argparse.Namespace) -> int:
    baseline = CapturedWorkspace.from_dict(_read_object(args.baseline))
    candidate = CapturedWorkspace.from_dict(_read_object(args.candidate))
    result = assemble_validation(
        baseline,
        candidate,
        load_config(args.config),
    )
    _write_json(result.to_dict(), args.output)
    return _outcome_code(result.outcome)


def _observe_ros2(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.validation.ros2_observer is None:
        raise ConfigError("validation.observe.ros2 is required")
    observation = observe_ros2(config.validation.ros2_observer)
    _write_or_print(observation.to_dict(), args.output)
    return 0


def _init(args: argparse.Namespace) -> int:
    repository = args.repository.resolve()
    result = initialize_repository(
        repository,
        project_name=args.project_name or repository.name,
        build_command=args.build_command,
        setup_script=args.setup_script,
        launch_command=args.launch_command,
        required_nodes=tuple(args.required_node),
        required_topics=tuple(args.required_topic),
        required_services=tuple(args.required_service),
        required_actions=tuple(args.required_action),
        required_transforms=tuple(args.required_transform),
        required_diagnostics=tuple(args.required_diagnostic),
        rate_topics=tuple(args.rate_topic),
        diagnostics_topics=tuple(args.diagnostics_topic),
        transform_topics=tuple(args.transform_topic),
        open_pull_request=args.open_pr,
        branch=args.branch,
        runner_image=args.runner_image,
    )
    for path in result.created:
        print(f"created {path.relative_to(result.repository).as_posix()}")
    if result.pull_request_url is not None:
        print(f"pull request {result.pull_request_url}")
    else:
        print("Review the files, then run fraeno doctor --local-only.")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    report = doctor_repository(
        args.repository,
        github_repository=args.github_repository,
        pull_request=args.pull_request,
        run_observer=args.run_observer,
        local_only=args.local_only,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        labels = {
            CheckStatus.PASS: "pass",
            CheckStatus.WARNING: "note",
            CheckStatus.FAIL: "fail",
        }
        for check in report.checks:
            print(f"[{labels[check.status]}] {check.name}  {check.message}")
            if check.fix is not None:
                print(f"       Next  {check.fix}")
        print(
            "Live observer  "
            + ("verified" if report.live_observer_verified else "not verified")
        )
        print(
            "GitHub App  "
            + (
                "verified on a pull request"
                if report.github_app_verified
                else "not verified"
            )
        )
        if report.ready:
            print("Fraeno doctor found no blocking problems.")
        else:
            failed = sum(
                check.status is CheckStatus.FAIL for check in report.checks
            )
            print(f"Fraeno doctor found {failed} blocking problem(s).")
    return 0 if report.ready else 1


def _observation_from_file(path: Path) -> SystemObservation:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ObservationError("observation file must contain a JSON object")
    return SystemObservation.from_dict(raw)


def _write_or_print(payload: dict[str, Any], destination: Path | None) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if destination:
        destination.write_text(rendered)
    else:
        print(rendered, end="")


def _write_json(payload: dict[str, Any], destination: Path) -> None:
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _read_object(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return raw


def _open_pull_requests(
    path: Path | None,
) -> tuple[OpenUpdatePullRequest, ...]:
    if path is None:
        return ()
    raw = json.loads(path.read_text())
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ValueError("open pull requests must be a JSON list of objects")
    return tuple(
        OpenUpdatePullRequest.from_mapping(item) for item in raw
    )


def _parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("--now must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _outcome_code(outcome: Outcome) -> int:
    if outcome is Outcome.PASS:
        return 0
    if outcome is Outcome.BLOCK:
        return 1
    return 2


def _add_target_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ros-distro")
    parser.add_argument("--os")
    parser.add_argument("--os-version")
    parser.add_argument("--architecture")


def _target_from_args(
    args: argparse.Namespace,
    report: ScanReport,
) -> TargetPlatform:
    inferred = infer_target_platform(report)
    return TargetPlatform(
        ros_distribution=args.ros_distro or inferred.ros_distribution,
        operating_system=args.os or inferred.operating_system,
        operating_system_version=args.os_version or inferred.operating_system_version,
        architecture=args.architecture or inferred.architecture,
    )


if __name__ == "__main__":
    raise SystemExit(main())
