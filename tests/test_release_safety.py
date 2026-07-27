from __future__ import annotations

import pytest

from fraeno.release_safety import (
    REQUIRED_CHECKS,
    ReleaseSafetyError,
    build_release_manifest,
    extract_build_digest,
    require_release_checks,
    require_successful_checks,
    select_reviewed_pull_request,
    validate_release_inputs,
)

COMMIT = "a" * 40
REVIEWED_COMMIT = "d" * 40
DIGEST = "sha256:" + ("b" * 64)
PREVIOUS_DIGEST = "sha256:" + ("c" * 64)


def check_run(
    name: str,
    *,
    run_id: int,
    status: str = "completed",
    conclusion: str | None = "success",
    head_sha: str = COMMIT,
) -> dict[str, object]:
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "head_sha": head_sha,
        "html_url": f"https://github.test/checks/{run_id}",
        "completed_at": "2026-07-27T18:00:00Z",
    }


def green_payload() -> dict[str, object]:
    return {
        "check_runs": [
            check_run(name, run_id=index)
            for index, name in enumerate(REQUIRED_CHECKS, start=1)
        ]
    }


@pytest.mark.parametrize(
    "version",
    ["0.1.0", "2.0.0-rc.1", "12.34.56"],
)
def test_release_inputs_accept_semver(version: str) -> None:
    validate_release_inputs(version, COMMIT, PREVIOUS_DIGEST)


@pytest.mark.parametrize(
    "version",
    ["v0.1.0", "01.2.3", "1.2", "1.2.3+mutable"],
)
def test_release_inputs_refuse_unsafe_versions(version: str) -> None:
    with pytest.raises(ReleaseSafetyError, match="SemVer"):
        validate_release_inputs(version, COMMIT, PREVIOUS_DIGEST)


def test_release_inputs_require_full_sha_and_previous_digest() -> None:
    with pytest.raises(ReleaseSafetyError, match="full commit"):
        validate_release_inputs("1.2.3", "abc123", PREVIOUS_DIGEST)
    with pytest.raises(ReleaseSafetyError, match="previous_digest"):
        validate_release_inputs("1.2.3", COMMIT, "runner:v1")
    validate_release_inputs("1.2.3", COMMIT, "none")


def test_release_gate_selects_latest_green_checks() -> None:
    payload = green_payload()
    runs = payload["check_runs"]
    assert isinstance(runs, list)
    runs.insert(0, check_run("test", run_id=0, conclusion="failure"))

    selected = require_successful_checks(payload, commit_sha=COMMIT)

    assert set(selected) == set(REQUIRED_CHECKS)
    assert selected["test"]["id"] == 4


def test_release_gate_refuses_missing_or_non_green_checks() -> None:
    missing = green_payload()
    runs = missing["check_runs"]
    assert isinstance(runs, list)
    runs.pop()
    with pytest.raises(ReleaseSafetyError, match="test: missing"):
        require_successful_checks(missing, commit_sha=COMMIT)

    pending = green_payload()
    pending_runs = pending["check_runs"]
    assert isinstance(pending_runs, list)
    pending_runs[-1] = check_run(
        "test",
        run_id=99,
        status="in_progress",
        conclusion=None,
    )
    with pytest.raises(ReleaseSafetyError, match="in_progress/None"):
        require_successful_checks(pending, commit_sha=COMMIT)


def test_release_gate_combines_exact_commit_and_reviewed_tree_checks() -> None:
    release = {
        "check_runs": [
            check_run(name, run_id=index)
            for index, name in enumerate(
                ("container", "ros-integration", "test"),
                start=1,
            )
        ]
    }
    reviewed = {
        "check_runs": [
            check_run(
                "Fraeno / robot integration",
                run_id=4,
                head_sha=REVIEWED_COMMIT,
            )
        ]
    }

    selected = require_release_checks(
        release,
        reviewed,
        commit_sha=COMMIT,
        reviewed_sha=REVIEWED_COMMIT,
    )

    assert set(selected) == set(REQUIRED_CHECKS)
    assert selected["test"]["validation_scope"] == "release_commit"
    assert (
        selected["Fraeno / robot integration"]["validation_scope"]
        == "reviewed_tree"
    )


def test_select_reviewed_pull_request_requires_exact_merge_commit() -> None:
    pull = {
        "number": 12,
        "state": "closed",
        "merged_at": "2026-07-27T18:00:00Z",
        "merge_commit_sha": COMMIT,
        "html_url": "https://github.test/pull/12",
        "head": {"sha": REVIEWED_COMMIT},
    }

    selected = select_reviewed_pull_request([pull], commit_sha=COMMIT)

    assert selected["number"] == 12
    assert selected["reviewed_sha"] == REVIEWED_COMMIT
    with pytest.raises(ReleaseSafetyError, match="exactly one"):
        select_reviewed_pull_request(
            [{**pull, "merge_commit_sha": "e" * 40}],
            commit_sha=COMMIT,
        )


def test_extract_build_digest_refuses_missing_metadata() -> None:
    assert extract_build_digest({"containerimage.digest": DIGEST}) == DIGEST
    with pytest.raises(ReleaseSafetyError, match=r"containerimage\.digest"):
        extract_build_digest({"containerimage.digest": "runner:v1"})


def test_release_manifest_is_digest_only_and_rollback_ready() -> None:
    checks = require_successful_checks(green_payload(), commit_sha=COMMIT)

    manifest = build_release_manifest(
        version="0.1.0",
        commit_sha=COMMIT,
        image="us-central1-docker.pkg.dev/project/repository/runner",
        digest=DIGEST,
        previous_digest=PREVIOUS_DIGEST,
        run_url="https://github.test/actions/runs/1",
        checks=checks,
    )

    runner = manifest["runner"]
    rollback = manifest["rollback"]
    assert isinstance(runner, dict)
    assert isinstance(rollback, dict)
    assert runner["reference"].endswith(f"@{DIGEST}")
    assert runner["semantic_tag"].endswith(":v0.1.0")
    assert runner["commit_tag"].endswith(f":{COMMIT}")
    assert rollback["reference"].endswith(f"@{PREVIOUS_DIGEST}")
    assert rollback["smoke_tested"] is True
    assert manifest["release_gate"]["test"]["head_sha"] == COMMIT
    assert "latest" not in str(manifest).lower()
