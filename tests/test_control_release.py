from __future__ import annotations

import pytest

from fraeno.control_release import (
    build_control_release_manifest,
    inspect_service,
    validate_control_release_inputs,
)
from fraeno.release_safety import ReleaseSafetyError

COMMIT = "a" * 40
DIGEST = "sha256:" + ("b" * 64)
OLD_DIGEST = "sha256:" + ("c" * 64)
IMAGE = "us-central1-docker.pkg.dev/project/repository/control-plane"
RUNNER_IMAGE = "us-central1-docker.pkg.dev/project/runner/runner"
WEBHOOK_ACCOUNT = "webhook@project.iam.gserviceaccount.com"


def service_payload(
    *,
    name: str = "fraeno-github-webhook",
    revision: str = "fraeno-github-webhook-00008-abc",
) -> dict[str, object]:
    return {
        "metadata": {"name": name},
        "status": {
            "latestReadyRevisionName": "fraeno-github-webhook-00009-failed",
            "url": f"https://{name}.example.test",
            "traffic": [
                {"revisionName": revision, "percent": 100},
                {
                    "revisionName": "fraeno-github-webhook-candidate",
                    "percent": 0,
                    "tag": "old-candidate",
                },
            ],
        },
    }


def revision_payload(
    *,
    name: str = "fraeno-github-webhook-00008-abc",
    service: str = "fraeno-github-webhook",
    image: str = f"old.example/control-plane@{OLD_DIGEST}",
    service_account: str = WEBHOOK_ACCOUNT,
) -> dict[str, object]:
    return {
        "metadata": {
            "name": name,
            "labels": {"serving.knative.dev/service": service},
        },
        "spec": {
            "containers": [{"image": image}],
            "serviceAccountName": service_account,
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "imageDigest": image,
        },
    }


def test_validate_control_release_inputs_require_exact_values() -> None:
    validate_control_release_inputs(
        "0.1.0",
        COMMIT,
        "fraeno-github-webhook-00008-abc",
        "fraeno-github-worker-00006-def",
    )
    with pytest.raises(ReleaseSafetyError, match="full Cloud Run revision"):
        validate_control_release_inputs(
            "0.1.0",
            COMMIT,
            "latest",
            "fraeno-github-worker-00006-def",
        )


def test_inspect_service_requires_one_digest_pinned_active_revision() -> None:
    snapshot = inspect_service(
        service_payload(),
        revision_payload(),
        expected_service="fraeno-github-webhook",
        expected_revision="fraeno-github-webhook-00008-abc",
        expected_service_account=WEBHOOK_ACCOUNT,
    )

    assert snapshot["revision"] == "fraeno-github-webhook-00008-abc"
    assert snapshot["image"].endswith(OLD_DIGEST)

    split_traffic = service_payload()
    status = split_traffic["status"]
    assert isinstance(status, dict)
    status["traffic"] = [
        {
            "revisionName": "fraeno-github-webhook-00008-abc",
            "percent": 50,
        },
        {
            "revisionName": "fraeno-github-webhook-00007-old",
            "percent": 50,
        },
    ]
    with pytest.raises(ReleaseSafetyError, match="100 percent"):
        inspect_service(
            split_traffic,
            revision_payload(),
            expected_service="fraeno-github-webhook",
            expected_revision="fraeno-github-webhook-00008-abc",
            expected_service_account=WEBHOOK_ACCOUNT,
        )

    tagged = revision_payload(image="old.example/control-plane:v1")
    with pytest.raises(ReleaseSafetyError, match="sha256"):
        inspect_service(
            service_payload(),
            tagged,
            expected_service="fraeno-github-webhook",
            expected_revision="fraeno-github-webhook-00008-abc",
            expected_service_account=WEBHOOK_ACCOUNT,
        )

    with pytest.raises(ReleaseSafetyError, match="expected Cloud Run revision"):
        inspect_service(
            service_payload(),
            revision_payload(name="fraeno-github-webhook-00009-failed"),
            expected_service="fraeno-github-webhook",
            expected_revision="fraeno-github-webhook-00008-abc",
            expected_service_account=WEBHOOK_ACCOUNT,
        )


def test_control_manifest_records_exact_deployment_and_rollback() -> None:
    previous = {
        "webhook": {
            "revision": "fraeno-github-webhook-00008-abc",
            "image": f"old.example/control-plane@{OLD_DIGEST}",
        },
        "worker": {
            "revision": "fraeno-github-worker-00006-def",
            "image": f"old.example/control-plane@{OLD_DIGEST}",
        },
    }
    final = {
        "webhook": {
            "revision": "fraeno-github-webhook-rel-a",
            "image": f"{IMAGE}@{DIGEST}",
            "url": "https://webhook.example.test",
        },
        "worker": {
            "revision": "fraeno-github-worker-rel-a",
            "image": f"{IMAGE}@{DIGEST}",
            "url": "https://worker.example.test",
        },
        "rollback": {
            "candidate_smoke_tested_at": "2026-07-27T20:00:00Z",
            "previous_smoke_tested_at": "2026-07-27T20:01:00Z",
            "candidate_restored_at": "2026-07-27T20:02:00Z",
            "candidate_restored": True,
        },
    }
    checks = {
        name: {
            "id": index,
            "html_url": f"https://github.test/check/{index}",
            "completed_at": "2026-07-27T19:00:00Z",
            "head_sha": COMMIT,
            "validation_scope": (
                "reviewed_tree"
                if name == "Fraeno / robot integration"
                else "release_commit"
            ),
        }
        for index, name in enumerate(
            ("Fraeno / robot integration", "container", "ros-integration", "test"),
            start=1,
        )
    }

    manifest = build_control_release_manifest(
        version="0.1.0",
        commit_sha=COMMIT,
        image=IMAGE,
        digest=DIGEST,
        runner_image=RUNNER_IMAGE,
        runner_digest=OLD_DIGEST,
        runner_run_url="https://github.test/run/1",
        run_url="https://github.test/run/2",
        checks=checks,
        previous_state=previous,
        final_state=final,
    )

    control_plane = manifest["control_plane"]
    assert isinstance(control_plane, dict)
    assert control_plane["reference"] == f"{IMAGE}@{DIGEST}"
    assert manifest["runner"]["reference"] == f"{RUNNER_IMAGE}@{OLD_DIGEST}"
    assert manifest["runner"]["release_run_url"] == "https://github.test/run/1"
    assert (
        control_plane["services"]["worker"]["previous_revision"]
        == "fraeno-github-worker-00006-def"
    )
    assert manifest["rollback"]["candidate_restored"] is True
