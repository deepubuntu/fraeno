from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

EXACT_COMMIT_CHECKS = (
    "container",
    "ros-integration",
    "test",
)
REVIEWED_TREE_CHECKS = ("Fraeno / robot integration",)
REQUIRED_CHECKS = REVIEWED_TREE_CHECKS + EXACT_COMMIT_CHECKS

SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class ReleaseSafetyError(ValueError):
    pass


def validate_release_inputs(
    version: str,
    commit_sha: str,
    previous_digest: str,
) -> None:
    if SEMVER.fullmatch(version) is None:
        raise ReleaseSafetyError(
            "version must be SemVer without a leading v or build metadata"
        )
    if COMMIT_SHA.fullmatch(commit_sha) is None:
        raise ReleaseSafetyError("commit_sha must be a lowercase full commit SHA")
    if previous_digest != "none" and DIGEST.fullmatch(previous_digest) is None:
        raise ReleaseSafetyError(
            "previous_digest must be none or a lowercase sha256 digest"
        )


def require_successful_checks(
    payload: Any,
    *,
    commit_sha: str,
    required: Iterable[str] = REQUIRED_CHECKS,
) -> dict[str, dict[str, Any]]:
    runs = _check_runs(payload)
    newest: dict[str, dict[str, Any]] = {}
    for run in runs:
        name = run.get("name")
        if not isinstance(name, str):
            continue
        run_sha = run.get("head_sha")
        if run_sha != commit_sha:
            continue
        current = newest.get(name)
        if current is None or _run_id(run) > _run_id(current):
            newest[name] = run

    failures: list[str] = []
    selected: dict[str, dict[str, Any]] = {}
    for name in required:
        selected_run = newest.get(name)
        if selected_run is None:
            failures.append(f"{name}: missing")
            continue
        status = selected_run.get("status")
        conclusion = selected_run.get("conclusion")
        if status != "completed" or conclusion != "success":
            failures.append(f"{name}: {status}/{conclusion}")
            continue
        selected[name] = selected_run
    if failures:
        raise ReleaseSafetyError(
            "release checks are not all green: " + ", ".join(failures)
        )
    return selected


def select_reviewed_pull_request(
    payload: Any,
    *,
    commit_sha: str,
) -> dict[str, Any]:
    if not isinstance(payload, list):
        raise ReleaseSafetyError("associated pull requests must be a JSON list")
    matches: list[dict[str, Any]] = []
    for pull in payload:
        if not isinstance(pull, dict):
            continue
        head = pull.get("head")
        if (
            pull.get("state") != "closed"
            or not isinstance(pull.get("merged_at"), str)
            or pull.get("merge_commit_sha") != commit_sha
            or not isinstance(head, dict)
            or not isinstance(head.get("sha"), str)
            or COMMIT_SHA.fullmatch(head["sha"]) is None
        ):
            continue
        matches.append(pull)
    if len(matches) != 1:
        raise ReleaseSafetyError(
            "release commit must identify exactly one merged pull request"
        )
    pull = matches[0]
    head = pull["head"]
    return {
        "number": pull.get("number"),
        "url": pull.get("html_url"),
        "merged_at": pull["merged_at"],
        "merge_commit_sha": commit_sha,
        "reviewed_sha": head["sha"],
    }


def require_release_checks(
    release_payload: Any,
    reviewed_payload: Any,
    *,
    commit_sha: str,
    reviewed_sha: str,
) -> dict[str, dict[str, Any]]:
    exact = require_successful_checks(
        release_payload,
        commit_sha=commit_sha,
        required=EXACT_COMMIT_CHECKS,
    )
    reviewed = require_successful_checks(
        reviewed_payload,
        commit_sha=reviewed_sha,
        required=REVIEWED_TREE_CHECKS,
    )
    return {
        **{
            name: {**run, "validation_scope": "release_commit"}
            for name, run in exact.items()
        },
        **{
            name: {**run, "validation_scope": "reviewed_tree"}
            for name, run in reviewed.items()
        },
    }


def extract_build_digest(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ReleaseSafetyError("BuildKit metadata must be a JSON object")
    digest = payload.get("containerimage.digest")
    if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
        raise ReleaseSafetyError(
            "BuildKit metadata does not contain a valid containerimage.digest"
        )
    return digest


def build_release_manifest(
    *,
    version: str,
    commit_sha: str,
    image: str,
    digest: str,
    previous_digest: str,
    run_url: str,
    checks: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    validate_release_inputs(version, commit_sha, previous_digest)
    if DIGEST.fullmatch(digest) is None:
        raise ReleaseSafetyError("published digest is not a sha256 digest")
    if "@sha256:" in image or ":" in image.rsplit("/", maxsplit=1)[-1]:
        raise ReleaseSafetyError("image must not include a tag or digest")
    rollback_reference = (
        None if previous_digest == "none" else f"{image}@{previous_digest}"
    )
    return {
        "schema_version": 1,
        "release": {
            "version": version,
            "commit": commit_sha,
            "run_url": run_url,
        },
        "runner": {
            "image": image,
            "digest": digest,
            "reference": f"{image}@{digest}",
            "semantic_tag": f"{image}:v{version}",
            "commit_tag": f"{image}:{commit_sha}",
        },
        "attestations": {
            "sbom": "BuildKit SPDX attestation",
            "provenance": "BuildKit SLSA provenance attestation",
        },
        "release_gate": {
            name: {
                "id": run.get("id"),
                "url": run.get("html_url"),
                "completed_at": run.get("completed_at"),
                "head_sha": run.get("head_sha"),
                "validation_scope": run.get("validation_scope"),
            }
            for name, run in sorted(checks.items())
        },
        "rollback": {
            "previous_digest": (
                None if previous_digest == "none" else previous_digest
            ),
            "reference": rollback_reference,
            "smoke_tested": previous_digest != "none",
        },
    }


def _check_runs(payload: Any) -> list[dict[str, Any]]:
    pages = payload if isinstance(payload, list) else [payload]
    runs: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            raise ReleaseSafetyError("check-run payload contains a non-object page")
        page_runs = page.get("check_runs")
        if not isinstance(page_runs, list):
            raise ReleaseSafetyError("check-run payload has no check_runs list")
        for run in page_runs:
            if isinstance(run, dict):
                runs.append(run)
    return runs


def _run_id(run: dict[str, Any]) -> int:
    value = run.get("id")
    return value if isinstance(value, int) else -1


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseSafetyError(f"could not read JSON from {path}: {error}") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate immutable Fraeno releases.")
    commands = parser.add_subparsers(dest="command", required=True)

    inputs = commands.add_parser("validate-inputs")
    inputs.add_argument("--version", required=True)
    inputs.add_argument("--commit-sha", required=True)
    inputs.add_argument("--previous-digest", required=True)

    checks = commands.add_parser("verify-checks")
    checks.add_argument("--input", type=Path, required=True)
    checks.add_argument("--reviewed-input", type=Path, required=True)
    checks.add_argument("--commit-sha", required=True)
    checks.add_argument("--reviewed-sha", required=True)
    checks.add_argument("--output", type=Path, required=True)

    pull = commands.add_parser("select-reviewed-pr")
    pull.add_argument("--input", type=Path, required=True)
    pull.add_argument("--commit-sha", required=True)
    pull.add_argument("--output", type=Path, required=True)

    digest = commands.add_parser("extract-digest")
    digest.add_argument("--input", type=Path, required=True)

    manifest = commands.add_parser("write-manifest")
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--commit-sha", required=True)
    manifest.add_argument("--image", required=True)
    manifest.add_argument("--digest", required=True)
    manifest.add_argument("--previous-digest", required=True)
    manifest.add_argument("--run-url", required=True)
    manifest.add_argument("--checks", type=Path, required=True)
    manifest.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-inputs":
            validate_release_inputs(
                args.version,
                args.commit_sha,
                args.previous_digest,
            )
        elif args.command == "verify-checks":
            validate_release_inputs("0.0.0", args.commit_sha, "none")
            validate_release_inputs("0.0.0", args.reviewed_sha, "none")
            selected = require_release_checks(
                _load_json(args.input),
                _load_json(args.reviewed_input),
                commit_sha=args.commit_sha,
                reviewed_sha=args.reviewed_sha,
            )
            args.output.write_text(json.dumps(selected, indent=2, sort_keys=True) + "\n")
        elif args.command == "select-reviewed-pr":
            validate_release_inputs("0.0.0", args.commit_sha, "none")
            selected_pull = select_reviewed_pull_request(
                _load_json(args.input),
                commit_sha=args.commit_sha,
            )
            args.output.write_text(
                json.dumps(selected_pull, indent=2, sort_keys=True) + "\n"
            )
        elif args.command == "extract-digest":
            print(extract_build_digest(_load_json(args.input)))
        elif args.command == "write-manifest":
            checks = _load_json(args.checks)
            if not isinstance(checks, dict):
                raise ReleaseSafetyError("selected checks must be a JSON object")
            payload = build_release_manifest(
                version=args.version,
                commit_sha=args.commit_sha,
                image=args.image,
                digest=args.digest,
                previous_digest=args.previous_digest,
                run_url=args.run_url,
                checks=checks,
            )
            args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except ReleaseSafetyError as error:
        parser = _parser()
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
