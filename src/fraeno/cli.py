from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from fraeno import __version__
from fraeno.config import ConfigError, load_config
from fraeno.lockfile import build_lockfile, write_lockfile
from fraeno.scanner import RepositoryScanner
from fraeno.updates import (
    apply_next_python_update,
    apply_update,
    find_python_updates,
)
from fraeno.validation.compare import Outcome, compare_systems
from fraeno.validation.observation import ObservationError, SystemObservation
from fraeno.validation.runner import run_validation


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

    lock = commands.add_parser("lock", help="Write a dependency lock snapshot.")
    lock.add_argument("repository", nargs="?", default=".")
    lock.add_argument("--output", "-o", type=Path, default=Path("fraeno.lock.json"))

    outdated = commands.add_parser(
        "outdated", help="Find supported dependency updates."
    )
    outdated.add_argument("repository", nargs="?", default=".")
    outdated.add_argument("--output", "-o", type=Path)

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            return _scan(args)
        if args.command == "lock":
            return _lock(args)
        if args.command == "outdated":
            return _outdated(args)
        if args.command == "update":
            return _update(args)
        if args.command == "update-next":
            return _update_next(args)
        if args.command == "compare":
            return _compare(args)
        if args.command == "validate":
            return _validate(args)
    except (ConfigError, ObservationError, OSError, ValueError) as error:
        print(f"fraeno: {error}", file=sys.stderr)
        return 2
    return 2


def _scan(args: argparse.Namespace) -> int:
    root = Path(args.repository).resolve()
    report = RepositoryScanner(root).scan()
    payload = report.to_dict()
    _write_or_print(payload, args.output)
    if args.lock:
        write_lockfile(build_lockfile(report, root), args.lock)
    return 0 if not report.warnings else 1


def _lock(args: argparse.Namespace) -> int:
    root = Path(args.repository).resolve()
    report = RepositoryScanner(root).scan()
    write_lockfile(build_lockfile(report, root), args.output)
    print(args.output)
    return 0 if not report.warnings else 1


def _outdated(args: argparse.Namespace) -> int:
    root = Path(args.repository).resolve()
    report = RepositoryScanner(root).scan()
    candidates, warnings = find_python_updates(report)
    _write_or_print(
        {
            "schema_version": 1,
            "updates": [candidate.to_dict() for candidate in candidates],
            "warnings": report.warnings + warnings,
        },
        args.output,
    )
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
    result, warnings = apply_next_python_update(
        root, report, dry_run=args.dry_run
    )
    _write_or_print(
        {
            "schema_version": 1,
            "updated": result is not None,
            "update": result.to_dict() if result else None,
            "warnings": report.warnings + warnings,
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


def _outcome_code(outcome: Outcome) -> int:
    if outcome is Outcome.PASS:
        return 0
    if outcome is Outcome.BLOCK:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
