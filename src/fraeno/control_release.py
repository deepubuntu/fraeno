from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from fraeno.release_safety import (
    COMMIT_SHA,
    DIGEST,
    REQUIRED_CHECKS,
    SEMVER,
    ReleaseSafetyError,
)

CLOUD_RUN_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")
EXPECTED_REVISION_PREFIXES = {
    "previous_webhook_revision": "fraeno-github-webhook-",
    "previous_worker_revision": "fraeno-github-worker-",
}
UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z"
)


def validate_control_release_inputs(
    version: str,
    commit_sha: str,
    previous_webhook_revision: str,
    previous_worker_revision: str,
) -> None:
    if SEMVER.fullmatch(version) is None:
        raise ReleaseSafetyError(
            "version must be SemVer without a leading v or build metadata"
        )
    if COMMIT_SHA.fullmatch(commit_sha) is None:
        raise ReleaseSafetyError("commit_sha must be a lowercase full commit SHA")
    for field, revision in (
        ("previous_webhook_revision", previous_webhook_revision),
        ("previous_worker_revision", previous_worker_revision),
    ):
        if (
            CLOUD_RUN_NAME.fullmatch(revision) is None
            or not revision.startswith(EXPECTED_REVISION_PREFIXES[field])
        ):
            raise ReleaseSafetyError(f"{field} must be a full Cloud Run revision name")


def inspect_service(
    service_payload: Any,
    revision_payload: Any,
    *,
    expected_service: str,
    expected_revision: str,
    expected_service_account: str,
) -> dict[str, str]:
    if not isinstance(service_payload, dict):
        raise ReleaseSafetyError("Cloud Run service payload must be an object")
    if not isinstance(revision_payload, dict):
        raise ReleaseSafetyError("Cloud Run revision payload must be an object")
    metadata = _object(service_payload, "metadata")
    status = _object(service_payload, "status")

    if metadata.get("name") != expected_service:
        raise ReleaseSafetyError(
            f"expected Cloud Run service {expected_service}, "
            f"got {metadata.get('name')!r}"
        )
    traffic = status.get("traffic")
    if not isinstance(traffic, list):
        raise ReleaseSafetyError(f"{expected_service} has no traffic state")
    active = [
        item
        for item in traffic
        if isinstance(item, dict) and int(item.get("percent") or 0) > 0
    ]
    if (
        len(active) != 1
        or active[0].get("revisionName") != expected_revision
        or active[0].get("percent") != 100
    ):
        raise ReleaseSafetyError(
            f"{expected_service} must send 100 percent of traffic to "
            f"{expected_revision}"
        )

    revision_metadata = _object(revision_payload, "metadata")
    if revision_metadata.get("name") != expected_revision:
        raise ReleaseSafetyError(
            f"expected Cloud Run revision {expected_revision}, "
            f"got {revision_metadata.get('name')!r}"
        )
    revision_labels = _object(revision_metadata, "labels")
    if revision_labels.get("serving.knative.dev/service") != expected_service:
        raise ReleaseSafetyError(
            f"{expected_revision} does not belong to {expected_service}"
        )
    revision_spec = _object(revision_payload, "spec")
    service_account = revision_spec.get("serviceAccountName")
    if service_account != expected_service_account:
        raise ReleaseSafetyError(
            f"{expected_revision} must use service account "
            f"{expected_service_account}"
        )
    revision_status = _object(revision_payload, "status")
    conditions = revision_status.get("conditions")
    if not isinstance(conditions, list) or not any(
        isinstance(condition, dict)
        and condition.get("type") == "Ready"
        and condition.get("status") == "True"
        for condition in conditions
    ):
        raise ReleaseSafetyError(f"{expected_revision} is not ready")

    containers = revision_spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise ReleaseSafetyError(
            f"{expected_revision} must have exactly one container"
        )
    container = containers[0]
    if not isinstance(container, dict):
        raise ReleaseSafetyError(f"{expected_revision} container is invalid")
    image = container.get("image")
    if (
        not isinstance(image, str)
        or "@sha256:" not in image
        or DIGEST.fullmatch(image.rsplit("@", maxsplit=1)[-1]) is None
    ):
        raise ReleaseSafetyError(
            f"{expected_revision} image must be pinned by sha256 digest"
        )
    if revision_status.get("imageDigest") not in (None, image):
        raise ReleaseSafetyError(
            f"{expected_revision} status digest does not match its container"
        )
    url = status.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ReleaseSafetyError(f"{expected_service} has no HTTPS service URL")
    return {
        "service": expected_service,
        "revision": expected_revision,
        "image": image,
        "url": url,
        "service_account": expected_service_account,
    }


def build_control_release_manifest(
    *,
    version: str,
    commit_sha: str,
    image: str,
    digest: str,
    runner_image: str,
    runner_digest: str,
    runner_run_url: str,
    run_url: str,
    checks: dict[str, Any],
    previous_state: dict[str, Any],
    final_state: dict[str, Any],
) -> dict[str, Any]:
    validate_control_release_inputs(
        version,
        commit_sha,
        _revision(previous_state, "webhook"),
        _revision(previous_state, "worker"),
    )
    if DIGEST.fullmatch(digest) is None:
        raise ReleaseSafetyError("published digest is not a sha256 digest")
    if DIGEST.fullmatch(runner_digest) is None:
        raise ReleaseSafetyError("runner digest is not a sha256 digest")
    if "@sha256:" in image or ":" in image.rsplit("/", maxsplit=1)[-1]:
        raise ReleaseSafetyError("image must not include a tag or digest")
    if (
        "@sha256:" in runner_image
        or ":" in runner_image.rsplit("/", maxsplit=1)[-1]
    ):
        raise ReleaseSafetyError("runner image must not include a tag or digest")
    missing_checks = set(REQUIRED_CHECKS) - set(checks)
    if missing_checks:
        raise ReleaseSafetyError(
            "release manifest is missing checks: "
            + ", ".join(sorted(missing_checks))
        )
    candidate_reference = f"{image}@{digest}"
    for role in ("webhook", "worker"):
        previous = _service(previous_state, role)
        final = _service(final_state, role)
        if previous.get("revision") == final.get("revision"):
            raise ReleaseSafetyError(f"{role} did not receive a new revision")
        if final.get("image") != candidate_reference:
            raise ReleaseSafetyError(
                f"{role} final image does not match the published digest"
            )

    rollback = _object(final_state, "rollback")
    for field in (
        "candidate_smoke_tested_at",
        "previous_smoke_tested_at",
        "candidate_restored_at",
    ):
        value = rollback.get(field)
        if not isinstance(value, str) or UTC_TIMESTAMP.fullmatch(value) is None:
            raise ReleaseSafetyError(f"rollback.{field} must be a UTC timestamp")
    if rollback.get("candidate_restored") is not True:
        raise ReleaseSafetyError("candidate traffic was not restored")

    return {
        "schema_version": 1,
        "release": {
            "version": version,
            "commit": commit_sha,
            "run_url": run_url,
        },
        "control_plane": {
            "image": image,
            "digest": digest,
            "reference": candidate_reference,
            "semantic_tag": f"{image}:v{version}",
            "commit_tag": f"{image}:{commit_sha}",
            "services": {
                role: {
                    "previous_revision": _revision(previous_state, role),
                    "previous_image": _service(previous_state, role).get("image"),
                    "deployed_revision": _revision(final_state, role),
                    "service_url": _service(final_state, role).get("url"),
                }
                for role in ("webhook", "worker")
            },
        },
        "runner": {
            "image": runner_image,
            "digest": runner_digest,
            "reference": f"{runner_image}@{runner_digest}",
            "semantic_tag": f"{runner_image}:v{version}",
            "commit_tag": f"{runner_image}:{commit_sha}",
            "release_run_url": runner_run_url,
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
            if isinstance(run, dict)
        },
        "rollback": rollback,
    }


def _object(payload: dict[str, Any], field: str) -> dict[str, Any]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise ReleaseSafetyError(f"{field} must be an object")
    return value


def _service(state: dict[str, Any], role: str) -> dict[str, Any]:
    value = state.get(role)
    if not isinstance(value, dict):
        raise ReleaseSafetyError(f"{role} state must be an object")
    return value


def _revision(state: dict[str, Any], role: str) -> str:
    revision = _service(state, role).get("revision")
    if not isinstance(revision, str):
        raise ReleaseSafetyError(f"{role} revision must be a string")
    return revision


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseSafetyError(f"could not read JSON from {path}: {error}") from error


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Fraeno control-plane releases."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inputs = commands.add_parser("validate-inputs")
    inputs.add_argument("--version", required=True)
    inputs.add_argument("--commit-sha", required=True)
    inputs.add_argument("--previous-webhook-revision", required=True)
    inputs.add_argument("--previous-worker-revision", required=True)

    inspect = commands.add_parser("inspect-service")
    inspect.add_argument("--input", type=Path, required=True)
    inspect.add_argument("--revision-input", type=Path, required=True)
    inspect.add_argument("--service", required=True)
    inspect.add_argument("--revision", required=True)
    inspect.add_argument("--service-account", required=True)
    inspect.add_argument("--output", type=Path, required=True)

    manifest = commands.add_parser("write-manifest")
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--commit-sha", required=True)
    manifest.add_argument("--image", required=True)
    manifest.add_argument("--digest", required=True)
    manifest.add_argument("--runner-image", required=True)
    manifest.add_argument("--runner-digest", required=True)
    manifest.add_argument("--runner-run-url", required=True)
    manifest.add_argument("--run-url", required=True)
    manifest.add_argument("--checks", type=Path, required=True)
    manifest.add_argument("--previous-state", type=Path, required=True)
    manifest.add_argument("--final-state", type=Path, required=True)
    manifest.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-inputs":
            validate_control_release_inputs(
                args.version,
                args.commit_sha,
                args.previous_webhook_revision,
                args.previous_worker_revision,
            )
        elif args.command == "inspect-service":
            snapshot = inspect_service(
                _load_json(args.input),
                _load_json(args.revision_input),
                expected_service=args.service,
                expected_revision=args.revision,
                expected_service_account=args.service_account,
            )
            _write_json(args.output, snapshot)
        elif args.command == "write-manifest":
            checks = _load_json(args.checks)
            previous_state = _load_json(args.previous_state)
            final_state = _load_json(args.final_state)
            if not isinstance(checks, dict):
                raise ReleaseSafetyError("selected checks must be an object")
            if not isinstance(previous_state, dict):
                raise ReleaseSafetyError("previous state must be an object")
            if not isinstance(final_state, dict):
                raise ReleaseSafetyError("final state must be an object")
            payload = build_control_release_manifest(
                version=args.version,
                commit_sha=args.commit_sha,
                image=args.image,
                digest=args.digest,
                runner_image=args.runner_image,
                runner_digest=args.runner_digest,
                runner_run_url=args.runner_run_url,
                run_url=args.run_url,
                checks=checks,
                previous_state=previous_state,
                final_state=final_state,
            )
            _write_json(args.output, payload)
    except ReleaseSafetyError as error:
        parser = _parser()
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
