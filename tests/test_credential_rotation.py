from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from fraeno.credential_rotation import (
    CredentialRotationError,
    authorize_promotion,
    authorize_retirement,
    inspect_staged_rotation,
    main,
    probe_staged_rotation,
    validate_final_cleanup,
    validate_release_credential_source,
    validate_restored_revision,
    verify_previous_credential_versions,
)

STARTED_AT = "2026-07-27T20:00:00Z"
VALID_UNTIL = "2026-07-27T21:00:00Z"
IMAGE = "us-central1-docker.pkg.dev/project/repository/control-plane@sha256:" + (
    "a" * 64
)
WEBHOOK_ACCOUNT = "webhook@project.iam.gserviceaccount.com"
WORKER_ACCOUNT = "worker@project.iam.gserviceaccount.com"


def service_payload(service: str, revision: str) -> dict[str, object]:
    return {
        "metadata": {"name": service},
        "status": {
            "traffic": [
                {
                    "revisionName": revision,
                    "percent": 0,
                    "tag": "credential-stage",
                    "url": f"https://credential-stage---{service}.example.test",
                },
                {
                    "revisionName": f"{service}-current",
                    "percent": 100,
                },
            ]
        },
    }


def revision_payload(
    service: str,
    revision: str,
    *,
    active_name: str,
    active_secret: str,
    active_version: str,
    previous_name: str,
    previous_secret: str,
    previous_version: str,
    image: str = IMAGE,
    service_account: str = WEBHOOK_ACCOUNT,
) -> dict[str, object]:
    return {
        "metadata": {
            "name": revision,
            "labels": {"serving.knative.dev/service": service},
        },
        "spec": {
            "serviceAccountName": service_account,
            "containers": [
                {
                    "image": image,
                    "env": [
                        {"name": "FRAENO_SERVICE_MODE", "value": active_name},
                        {
                            "name": "FRAENO_CREDENTIAL_ROTATION_STARTED_AT",
                            "value": STARTED_AT,
                        },
                        {
                            "name": "FRAENO_PREVIOUS_CREDENTIALS_VALID_UNTIL",
                            "value": VALID_UNTIL,
                        },
                        {
                            "name": active_secret,
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": active_name
                                    and (
                                        "fraeno-github-webhook-secret"
                                        if active_name == "webhook"
                                        else "fraeno-github-private-key"
                                    ),
                                    "key": active_version,
                                }
                            },
                        },
                        {
                            "name": previous_secret,
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": active_name
                                    and (
                                        "fraeno-github-webhook-secret"
                                        if active_name == "webhook"
                                        else "fraeno-github-private-key"
                                    ),
                                    "key": previous_version,
                                }
                            },
                        },
                    ]
                }
            ]
        },
    }


def active_revision_payload(
    service: str,
    revision: str,
    *,
    mode: str,
    active_secret: str,
    active_version: str,
    service_account: str,
) -> dict[str, object]:
    payload = revision_payload(
        service,
        revision,
        active_name=mode,
        active_secret=active_secret,
        active_version=active_version,
        previous_name=mode,
        previous_secret=(
            "FRAENO_GITHUB_WEBHOOK_SECRET_PREVIOUS"
            if mode == "webhook"
            else "FRAENO_GITHUB_PRIVATE_KEY_PREVIOUS"
        ),
        previous_version="999",
        service_account=service_account,
    )
    container = payload["spec"]["containers"][0]  # type: ignore[index]
    container["env"] = [  # type: ignore[index]
        entry
        for entry in container["env"]  # type: ignore[index]
        if entry["name"]  # type: ignore[index]
        not in {
            "FRAENO_CREDENTIAL_ROTATION_STARTED_AT",
            "FRAENO_PREVIOUS_CREDENTIALS_VALID_UNTIL",
            "FRAENO_GITHUB_WEBHOOK_SECRET_PREVIOUS",
            "FRAENO_GITHUB_PRIVATE_KEY_PREVIOUS",
        }
    ]
    return payload


def staged() -> dict[str, object]:
    webhook_revision = "fraeno-github-webhook-rotation"
    worker_revision = "fraeno-github-worker-rotation"
    return inspect_staged_rotation(
        webhook_service=service_payload(
            "fraeno-github-webhook", webhook_revision
        ),
        webhook_revision=revision_payload(
            "fraeno-github-webhook",
            webhook_revision,
            active_name="webhook",
            active_secret="FRAENO_GITHUB_WEBHOOK_SECRET",
            active_version="5",
            previous_name="webhook",
            previous_secret="FRAENO_GITHUB_WEBHOOK_SECRET_PREVIOUS",
            previous_version="4",
            service_account=WEBHOOK_ACCOUNT,
        ),
        active_webhook_revision=active_revision_payload(
            "fraeno-github-webhook",
            "fraeno-github-webhook-active",
            mode="webhook",
            active_secret="FRAENO_GITHUB_WEBHOOK_SECRET",
            active_version="4",
            service_account=WEBHOOK_ACCOUNT,
        ),
        worker_service=service_payload("fraeno-github-worker", worker_revision),
        worker_revision=revision_payload(
            "fraeno-github-worker",
            worker_revision,
            active_name="worker",
            active_secret="FRAENO_GITHUB_PRIVATE_KEY",
            active_version="2",
            previous_name="worker",
            previous_secret="FRAENO_GITHUB_PRIVATE_KEY_PREVIOUS",
            previous_version="1",
            service_account=WORKER_ACCOUNT,
        ),
        active_worker_revision=active_revision_payload(
            "fraeno-github-worker",
            "fraeno-github-worker-active",
            mode="worker",
            active_secret="FRAENO_GITHUB_PRIVATE_KEY",
            active_version="1",
            service_account=WORKER_ACCOUNT,
        ),
        webhook_revision_name=webhook_revision,
        worker_revision_name=worker_revision,
        webhook_active_version="5",
        webhook_previous_version="4",
        private_key_active_version="2",
        private_key_previous_version="1",
        webhook_image=IMAGE,
        worker_image=IMAGE,
        webhook_service_account=WEBHOOK_ACCOUNT,
        worker_service_account=WORKER_ACCOUNT,
        started_at=STARTED_AT,
        previous_valid_until=VALID_UNTIL,
    )


def test_source_requires_previous_versions_currently_used_in_production() -> None:
    webhook = active_revision_payload(
        "fraeno-github-webhook",
        "fraeno-github-webhook-active",
        mode="webhook",
        active_secret="FRAENO_GITHUB_WEBHOOK_SECRET",
        active_version="4",
        service_account=WEBHOOK_ACCOUNT,
    )
    worker = active_revision_payload(
        "fraeno-github-worker",
        "fraeno-github-worker-active",
        mode="worker",
        active_secret="FRAENO_GITHUB_PRIVATE_KEY",
        active_version="1",
        service_account=WORKER_ACCOUNT,
    )

    assert verify_previous_credential_versions(
        webhook,
        worker,
        expected_webhook_version="4",
        expected_private_key_version="1",
    ) == {
        "webhook_previous_version": "4",
        "private_key_previous_version": "1",
    }

    with pytest.raises(
        CredentialRotationError, match="not the active production version"
    ):
        verify_previous_credential_versions(
            webhook,
            worker,
            expected_webhook_version="3",
            expected_private_key_version="1",
        )


def test_inspect_stage_cli_requires_both_active_revision_payloads(
    tmp_path: Path,
) -> None:
    webhook_candidate = "fraeno-github-webhook-rotation"
    worker_candidate = "fraeno-github-worker-rotation"
    payloads = {
        "webhook-service.json": service_payload(
            "fraeno-github-webhook", webhook_candidate
        ),
        "webhook-candidate.json": revision_payload(
            "fraeno-github-webhook",
            webhook_candidate,
            active_name="webhook",
            active_secret="FRAENO_GITHUB_WEBHOOK_SECRET",
            active_version="5",
            previous_name="webhook",
            previous_secret="FRAENO_GITHUB_WEBHOOK_SECRET_PREVIOUS",
            previous_version="4",
            service_account=WEBHOOK_ACCOUNT,
        ),
        "webhook-active.json": active_revision_payload(
            "fraeno-github-webhook",
            "fraeno-github-webhook-active",
            mode="webhook",
            active_secret="FRAENO_GITHUB_WEBHOOK_SECRET",
            active_version="4",
            service_account=WEBHOOK_ACCOUNT,
        ),
        "worker-service.json": service_payload(
            "fraeno-github-worker", worker_candidate
        ),
        "worker-candidate.json": revision_payload(
            "fraeno-github-worker",
            worker_candidate,
            active_name="worker",
            active_secret="FRAENO_GITHUB_PRIVATE_KEY",
            active_version="2",
            previous_name="worker",
            previous_secret="FRAENO_GITHUB_PRIVATE_KEY_PREVIOUS",
            previous_version="1",
            service_account=WORKER_ACCOUNT,
        ),
        "worker-active.json": active_revision_payload(
            "fraeno-github-worker",
            "fraeno-github-worker-active",
            mode="worker",
            active_secret="FRAENO_GITHUB_PRIVATE_KEY",
            active_version="1",
            service_account=WORKER_ACCOUNT,
        ),
    }
    for name, payload in payloads.items():
        (tmp_path / name).write_text(json.dumps(payload))
    output = tmp_path / "stage.json"

    result = main(
        [
            "inspect-stage",
            "--webhook-service",
            str(tmp_path / "webhook-service.json"),
            "--webhook-revision",
            str(tmp_path / "webhook-candidate.json"),
            "--active-webhook-revision",
            str(tmp_path / "webhook-active.json"),
            "--worker-service",
            str(tmp_path / "worker-service.json"),
            "--worker-revision",
            str(tmp_path / "worker-candidate.json"),
            "--active-worker-revision",
            str(tmp_path / "worker-active.json"),
            "--webhook-revision-name",
            webhook_candidate,
            "--worker-revision-name",
            worker_candidate,
            "--webhook-active-version",
            "5",
            "--webhook-previous-version",
            "4",
            "--private-key-active-version",
            "2",
            "--private-key-previous-version",
            "1",
            "--webhook-image",
            IMAGE,
            "--worker-image",
            IMAGE,
            "--webhook-service-account",
            WEBHOOK_ACCOUNT,
            "--worker-service-account",
            WORKER_ACCOUNT,
            "--started-at",
            STARTED_AT,
            "--previous-valid-until",
            VALID_UNTIL,
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert json.loads(output.read_text())["services"]["worker"][
        "service_account"
    ] == WORKER_ACCOUNT


def test_staged_rotation_requires_exact_secret_versions_and_no_traffic() -> None:
    stage = staged()

    assert stage["secret_versions"]["webhook"] == {
        "active": "5",
        "previous": "4",
    }
    assert stage["services"]["worker"]["revision"].endswith("-rotation")

    service = service_payload(
        "fraeno-github-webhook", "fraeno-github-webhook-rotation"
    )
    service["status"]["traffic"][0]["percent"] = 1  # type: ignore[index]
    with pytest.raises(CredentialRotationError, match="no production traffic"):
        inspect_staged_rotation(
            webhook_service=service,
            webhook_revision=revision_payload(
                "fraeno-github-webhook",
                "fraeno-github-webhook-rotation",
                active_name="webhook",
                active_secret="FRAENO_GITHUB_WEBHOOK_SECRET",
                active_version="5",
                previous_name="webhook",
                previous_secret="FRAENO_GITHUB_WEBHOOK_SECRET_PREVIOUS",
                previous_version="4",
                service_account=WEBHOOK_ACCOUNT,
            ),
            active_webhook_revision=active_revision_payload(
                "fraeno-github-webhook",
                "fraeno-github-webhook-active",
                mode="webhook",
                active_secret="FRAENO_GITHUB_WEBHOOK_SECRET",
                active_version="4",
                service_account=WEBHOOK_ACCOUNT,
            ),
            worker_service=service_payload(
                "fraeno-github-worker", "fraeno-github-worker-rotation"
            ),
            worker_revision=revision_payload(
                "fraeno-github-worker",
                "fraeno-github-worker-rotation",
                active_name="worker",
                active_secret="FRAENO_GITHUB_PRIVATE_KEY",
                active_version="2",
                previous_name="worker",
                previous_secret="FRAENO_GITHUB_PRIVATE_KEY_PREVIOUS",
                previous_version="1",
                service_account=WORKER_ACCOUNT,
            ),
            active_worker_revision=active_revision_payload(
                "fraeno-github-worker",
                "fraeno-github-worker-active",
                mode="worker",
                active_secret="FRAENO_GITHUB_PRIVATE_KEY",
                active_version="1",
                service_account=WORKER_ACCOUNT,
            ),
            webhook_revision_name="fraeno-github-webhook-rotation",
            worker_revision_name="fraeno-github-worker-rotation",
            webhook_active_version="5",
            webhook_previous_version="4",
            private_key_active_version="2",
            private_key_previous_version="1",
            webhook_image=IMAGE,
            worker_image=IMAGE,
            webhook_service_account=WEBHOOK_ACCOUNT,
            worker_service_account=WORKER_ACCOUNT,
            started_at=STARTED_AT,
            previous_valid_until=VALID_UNTIL,
        )


def test_stage_rejects_candidate_cloned_from_latest_failed_revision() -> None:
    with pytest.raises(
        CredentialRotationError, match="reviewed active image"
    ):
        inspect_staged_rotation(
            webhook_service=service_payload(
                "fraeno-github-webhook", "fraeno-github-webhook-rotation"
            ),
            webhook_revision=revision_payload(
                "fraeno-github-webhook",
                "fraeno-github-webhook-rotation",
                active_name="webhook",
                active_secret="FRAENO_GITHUB_WEBHOOK_SECRET",
                active_version="5",
                previous_name="webhook",
                previous_secret="FRAENO_GITHUB_WEBHOOK_SECRET_PREVIOUS",
                previous_version="4",
                image=(
                    "us-central1-docker.pkg.dev/project/repository/"
                    "control-plane@sha256:" + ("b" * 64)
                ),
                service_account=WEBHOOK_ACCOUNT,
            ),
            active_webhook_revision=active_revision_payload(
                "fraeno-github-webhook",
                "fraeno-github-webhook-active",
                mode="webhook",
                active_secret="FRAENO_GITHUB_WEBHOOK_SECRET",
                active_version="4",
                service_account=WEBHOOK_ACCOUNT,
            ),
            worker_service=service_payload(
                "fraeno-github-worker", "fraeno-github-worker-rotation"
            ),
            worker_revision=revision_payload(
                "fraeno-github-worker",
                "fraeno-github-worker-rotation",
                active_name="worker",
                active_secret="FRAENO_GITHUB_PRIVATE_KEY",
                active_version="2",
                previous_name="worker",
                previous_secret="FRAENO_GITHUB_PRIVATE_KEY_PREVIOUS",
                previous_version="1",
                service_account=WORKER_ACCOUNT,
            ),
            active_worker_revision=active_revision_payload(
                "fraeno-github-worker",
                "fraeno-github-worker-active",
                mode="worker",
                active_secret="FRAENO_GITHUB_PRIVATE_KEY",
                active_version="1",
                service_account=WORKER_ACCOUNT,
            ),
            webhook_revision_name="fraeno-github-webhook-rotation",
            worker_revision_name="fraeno-github-worker-rotation",
            webhook_active_version="5",
            webhook_previous_version="4",
            private_key_active_version="2",
            private_key_previous_version="1",
            webhook_image=IMAGE,
            worker_image=IMAGE,
            webhook_service_account=WEBHOOK_ACCOUNT,
            worker_service_account=WORKER_ACCOUNT,
            started_at=STARTED_AT,
            previous_valid_until=VALID_UNTIL,
        )


def test_stage_rejects_noncredential_runtime_drift_from_latest_template() -> None:
    active_webhook = active_revision_payload(
        "fraeno-github-webhook",
        "fraeno-github-webhook-active",
        mode="webhook",
        active_secret="FRAENO_GITHUB_WEBHOOK_SECRET",
        active_version="4",
        service_account=WEBHOOK_ACCOUNT,
    )
    active_container = active_webhook["spec"]["containers"][0]  # type: ignore[index]
    active_container["command"] = ["python", "-m", "fraeno.github_app.server"]  # type: ignore[index]
    active_container["env"].append(  # type: ignore[index]
        {"name": "FRAENO_TASK_QUEUE", "value": "reviewed-queue"}
    )
    candidate_webhook = revision_payload(
        "fraeno-github-webhook",
        "fraeno-github-webhook-rotation",
        active_name="webhook",
        active_secret="FRAENO_GITHUB_WEBHOOK_SECRET",
        active_version="5",
        previous_name="webhook",
        previous_secret="FRAENO_GITHUB_WEBHOOK_SECRET_PREVIOUS",
        previous_version="4",
        service_account=WEBHOOK_ACCOUNT,
    )
    candidate_container = candidate_webhook["spec"]["containers"][0]  # type: ignore[index]
    candidate_container["command"] = ["python", "-m", "unexpected.module"]  # type: ignore[index]
    candidate_container["env"].append(  # type: ignore[index]
        {"name": "FRAENO_TASK_QUEUE", "value": "failed-latest-queue"}
    )

    with pytest.raises(CredentialRotationError, match="runtime drifted"):
        inspect_staged_rotation(
            webhook_service=service_payload(
                "fraeno-github-webhook", "fraeno-github-webhook-rotation"
            ),
            webhook_revision=candidate_webhook,
            active_webhook_revision=active_webhook,
            worker_service=service_payload(
                "fraeno-github-worker", "fraeno-github-worker-rotation"
            ),
            worker_revision=revision_payload(
                "fraeno-github-worker",
                "fraeno-github-worker-rotation",
                active_name="worker",
                active_secret="FRAENO_GITHUB_PRIVATE_KEY",
                active_version="2",
                previous_name="worker",
                previous_secret="FRAENO_GITHUB_PRIVATE_KEY_PREVIOUS",
                previous_version="1",
                service_account=WORKER_ACCOUNT,
            ),
            active_worker_revision=active_revision_payload(
                "fraeno-github-worker",
                "fraeno-github-worker-active",
                mode="worker",
                active_secret="FRAENO_GITHUB_PRIVATE_KEY",
                active_version="1",
                service_account=WORKER_ACCOUNT,
            ),
            webhook_revision_name="fraeno-github-webhook-rotation",
            worker_revision_name="fraeno-github-worker-rotation",
            webhook_active_version="5",
            webhook_previous_version="4",
            private_key_active_version="2",
            private_key_previous_version="1",
            webhook_image=IMAGE,
            worker_image=IMAGE,
            webhook_service_account=WEBHOOK_ACCOUNT,
            worker_service_account=WORKER_ACCOUNT,
            started_at=STARTED_AT,
            previous_valid_until=VALID_UNTIL,
        )


def test_stage_rejects_runtime_annotation_drift() -> None:
    webhook_candidate = revision_payload(
        "fraeno-github-webhook",
        "fraeno-github-webhook-rotation",
        active_name="webhook",
        active_secret="FRAENO_GITHUB_WEBHOOK_SECRET",
        active_version="5",
        previous_name="webhook",
        previous_secret="FRAENO_GITHUB_WEBHOOK_SECRET_PREVIOUS",
        previous_version="4",
        service_account=WEBHOOK_ACCOUNT,
    )
    webhook_candidate["metadata"]["annotations"] = {  # type: ignore[index]
        "run.googleapis.com/vpc-access-egress": "all-traffic"
    }

    with pytest.raises(CredentialRotationError, match="annotations drifted"):
        inspect_staged_rotation(
            webhook_service=service_payload(
                "fraeno-github-webhook", "fraeno-github-webhook-rotation"
            ),
            webhook_revision=webhook_candidate,
            active_webhook_revision=active_revision_payload(
                "fraeno-github-webhook",
                "fraeno-github-webhook-active",
                mode="webhook",
                active_secret="FRAENO_GITHUB_WEBHOOK_SECRET",
                active_version="4",
                service_account=WEBHOOK_ACCOUNT,
            ),
            worker_service=service_payload(
                "fraeno-github-worker", "fraeno-github-worker-rotation"
            ),
            worker_revision=revision_payload(
                "fraeno-github-worker",
                "fraeno-github-worker-rotation",
                active_name="worker",
                active_secret="FRAENO_GITHUB_PRIVATE_KEY",
                active_version="2",
                previous_name="worker",
                previous_secret="FRAENO_GITHUB_PRIVATE_KEY_PREVIOUS",
                previous_version="1",
                service_account=WORKER_ACCOUNT,
            ),
            active_worker_revision=active_revision_payload(
                "fraeno-github-worker",
                "fraeno-github-worker-active",
                mode="worker",
                active_secret="FRAENO_GITHUB_PRIVATE_KEY",
                active_version="1",
                service_account=WORKER_ACCOUNT,
            ),
            webhook_revision_name="fraeno-github-webhook-rotation",
            worker_revision_name="fraeno-github-worker-rotation",
            webhook_active_version="5",
            webhook_previous_version="4",
            private_key_active_version="2",
            private_key_previous_version="1",
            webhook_image=IMAGE,
            worker_image=IMAGE,
            webhook_service_account=WEBHOOK_ACCOUNT,
            worker_service_account=WORKER_ACCOUNT,
            started_at=STARTED_AT,
            previous_valid_until=VALID_UNTIL,
        )


@pytest.mark.anyio
async def test_staged_probe_checks_both_credentials_without_returning_them() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/credential-readiness":
            assert request.headers["authorization"] == "Bearer identity-token"
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "active_key_valid": True,
                    "previous_key_configured": True,
                    "overlap_active": True,
                    "previous_key_valid": True,
                },
            )
        signature = request.headers["x-hub-signature-256"]
        if signature == "sha256=" + ("0" * 64):
            return httpx.Response(401)
        return httpx.Response(204)

    evidence = await probe_staged_rotation(
        staged(),
        active_webhook_secret=b"active-secret",
        previous_webhook_secret=b"previous-secret",
        worker_identity_token="identity-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )

    encoded = json.dumps(evidence)
    assert evidence["checks"]["previous_private_key"] == "accepted"
    assert "active-secret" not in encoded
    assert "previous-secret" not in encoded
    assert "identity-token" not in encoded


def test_retirement_requires_live_github_delivery_and_installation_proof() -> None:
    evidence = {
        "schema_version": 1,
        "verified_at": "2026-07-27T20:10:00Z",
        "stage": staged(),
        "checks": {
            "active_webhook_secret": "accepted",
            "previous_webhook_secret": "accepted",
            "invalid_webhook_secret": "rejected",
            "active_private_key": "accepted",
            "previous_private_key": "accepted",
        },
    }
    live = {
        "verified_at": "2026-07-27T20:20:00Z",
        "github_delivery_guid": "123e4567-e89b-42d3-a456-426614174000",
        "active_webhook_secret_accepted": True,
        "installation_ids": [149403236],
        "check_run_ids": [90100043690],
    }

    approval = authorize_retirement(
        evidence, live, now="2026-07-27T20:30:00Z"
    )

    assert approval["retirement_allowed"] is True
    assert approval["installation_ids"] == [149403236]
    assert approval["rotation_identity"]["candidate_revisions"] == {
        "webhook": "fraeno-github-webhook-rotation",
        "worker": "fraeno-github-worker-rotation",
    }
    assert approval["rotation_identity"]["secret_versions"]["webhook"] == {
        "active": "5",
        "previous": "4",
    }

    live["check_run_ids"] = []
    with pytest.raises(CredentialRotationError, match="signed delivery"):
        authorize_retirement(
            evidence, live, now="2026-07-27T20:30:00Z"
        )


def promotion_evidence() -> dict[str, object]:
    return {
        "schema_version": 1,
        "verified_at": "2026-07-27T20:10:00Z",
        "stage": staged(),
        "checks": {
            "active_webhook_secret": "accepted",
            "previous_webhook_secret": "accepted",
            "invalid_webhook_secret": "rejected",
            "active_private_key": "accepted",
            "previous_private_key": "accepted",
        },
    }


@pytest.mark.parametrize(
    ("now", "message"),
    [
        ("2026-07-27T21:00:00Z", "at least ten minutes"),
        ("2026-07-27T20:51:00Z", "at least ten minutes"),
        ("2026-07-27T20:09:59Z", "cannot precede"),
    ],
)
def test_promotion_rejects_expired_near_expiry_and_future_evidence(
    now: str, message: str
) -> None:
    with pytest.raises(CredentialRotationError, match=message):
        authorize_promotion(promotion_evidence(), now=now)


def test_promotion_rejects_verification_outside_overlap_window() -> None:
    evidence = promotion_evidence()
    evidence["verified_at"] = "2026-07-27T21:00:01Z"

    with pytest.raises(CredentialRotationError, match="during the overlap"):
        authorize_promotion(evidence, now="2026-07-27T21:00:02Z")


def test_promotion_command_verifies_checksum_and_allows_safe_window(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "credential-rotation-evidence.json"
    checksum_path = tmp_path / "credential-rotation-evidence.json.sha256"
    output_path = tmp_path / "promotion-authorization.json"
    evidence_path.write_text(
        json.dumps(promotion_evidence(), indent=2, sort_keys=True) + "\n"
    )
    digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    checksum_path.write_text(f"{digest}  {evidence_path.name}\n")

    result = main(
        [
            "authorize-promotion",
            "--evidence",
            str(evidence_path),
            "--checksum",
            str(checksum_path),
            "--now",
            "2026-07-27T20:30:00Z",
            "--output",
            str(output_path),
        ]
    )

    assert result == 0
    approval = json.loads(output_path.read_text())
    assert approval["promotion_allowed"] is True
    assert approval["minimum_remaining_seconds"] == 600
    assert approval["evidence_sha256"] == digest


@pytest.mark.parametrize(
    "traffic",
    [
        [
            {
                "revisionName": "fraeno-github-webhook-other",
                "percent": 100,
            }
        ],
        [
            {
                "revisionName": "fraeno-github-webhook-current",
                "percent": 100,
            },
            {
                "revisionName": "fraeno-github-webhook-candidate",
                "percent": 0,
                "tag": "credential-1234",
            },
        ],
    ],
)
def test_cleanup_rejects_bad_final_state_after_tag_removal_command_succeeds(
    traffic: list[dict[str, object]],
) -> None:
    with pytest.raises(
        CredentialRotationError,
        match=r"production traffic changed|candidate tag was not removed",
    ):
        validate_final_cleanup(
            {"status": {"traffic": traffic}},
            role="webhook",
            expected_revision="fraeno-github-webhook-current",
            candidate_tag="credential-1234",
        )


def test_stage_restores_safe_template_before_a_later_release() -> None:
    active = active_revision_payload(
        "fraeno-github-webhook",
        "fraeno-github-webhook-active",
        mode="webhook",
        active_secret="FRAENO_GITHUB_WEBHOOK_SECRET",
        active_version="4",
        service_account=WEBHOOK_ACCOUNT,
    )
    candidate = revision_payload(
        "fraeno-github-webhook",
        "fraeno-github-webhook-candidate",
        active_name="webhook",
        active_secret="FRAENO_GITHUB_WEBHOOK_SECRET",
        active_version="5",
        previous_name="webhook",
        previous_secret="FRAENO_GITHUB_WEBHOOK_SECRET_PREVIOUS",
        previous_version="4",
        service_account=WEBHOOK_ACCOUNT,
    )
    staged_service = {
        "spec": {
            "template": {
                "metadata": {"annotations": {}},
                "spec": copy.deepcopy(candidate["spec"]),
            }
        }
    }

    with pytest.raises(CredentialRotationError, match="pending"):
        validate_release_credential_source(
            staged_service,
            active,
            role="webhook",
        )

    restored = copy.deepcopy(active)
    restored["metadata"]["name"] = "fraeno-github-webhook-restore"  # type: ignore[index]
    validation = validate_restored_revision(
        restored,
        active,
        role="webhook",
        expected_revision="fraeno-github-webhook-restore",
    )
    assert validation["active_configuration_restored"] is True

    restored_service = {
        "spec": {
            "template": {
                "metadata": {"annotations": {}},
                "spec": copy.deepcopy(restored["spec"]),
            }
        }
    }
    release = validate_release_credential_source(
        restored_service,
        active,
        role="webhook",
    )
    assert release["release_source_safe"] is True


def test_release_source_rejects_unrelated_runtime_and_annotation_drift() -> None:
    active = active_revision_payload(
        "fraeno-github-webhook",
        "fraeno-github-webhook-active",
        mode="webhook",
        active_secret="FRAENO_GITHUB_WEBHOOK_SECRET",
        active_version="4",
        service_account=WEBHOOK_ACCOUNT,
    )
    template = {
        "metadata": {
            "annotations": {},
            "labels": {
                "fraeno-release-commit": "b" * 40,
                "fraeno-release-version": "1-0-0",
            },
        },
        "spec": copy.deepcopy(active["spec"]),
    }
    service = {"spec": {"template": template}}

    drifted_env = copy.deepcopy(service)
    drifted_env["spec"]["template"]["spec"]["containers"][0]["env"].append(  # type: ignore[index]
        {"name": "UNRELATED_SETTING", "value": "changed"}
    )
    with pytest.raises(CredentialRotationError, match="runtime drifted"):
        validate_release_credential_source(
            drifted_env,
            active,
            role="webhook",
        )

    drifted_command = copy.deepcopy(service)
    drifted_command["spec"]["template"]["spec"]["containers"][0]["command"] = [  # type: ignore[index]
        "unsafe"
    ]
    with pytest.raises(CredentialRotationError, match="runtime drifted"):
        validate_release_credential_source(
            drifted_command,
            active,
            role="webhook",
        )

    drifted_annotation = copy.deepcopy(service)
    drifted_annotation["spec"]["template"]["metadata"]["annotations"][  # type: ignore[index]
        "run.googleapis.com/cpu-throttling"
    ] = "false"
    with pytest.raises(CredentialRotationError, match="annotations drifted"):
        validate_release_credential_source(
            drifted_annotation,
            active,
            role="webhook",
        )

    drifted_label = copy.deepcopy(service)
    drifted_label["spec"]["template"]["metadata"]["labels"][  # type: ignore[index]
        "unreviewed"
    ] = "true"
    with pytest.raises(CredentialRotationError, match="labels drifted"):
        validate_release_credential_source(
            drifted_label,
            active,
            role="webhook",
        )
