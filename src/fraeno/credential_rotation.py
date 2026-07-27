from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fraeno.github_app.settings import CredentialRotationWindow, SettingsError

SECRET_VERSION = re.compile(r"[1-9][0-9]*")
REVISION_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")
DELIVERY_GUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
WEBHOOK_SERVICE = "fraeno-github-webhook"
WORKER_SERVICE = "fraeno-github-worker"
WEBHOOK_SECRET = "fraeno-github-webhook-secret"
PRIVATE_KEY_SECRET = "fraeno-github-private-key"
ROTATION_ENV_NAMES = {
    "FRAENO_CREDENTIAL_ROTATION_STARTED_AT",
    "FRAENO_PREVIOUS_CREDENTIALS_VALID_UNTIL",
    "FRAENO_GITHUB_WEBHOOK_SECRET_PREVIOUS",
    "FRAENO_GITHUB_PRIVATE_KEY_PREVIOUS",
}
NON_RUNTIME_ANNOTATIONS = {
    "run.googleapis.com/client-name",
    "run.googleapis.com/client-version",
    "run.googleapis.com/operation-id",
    "serving.knative.dev/creator",
    "serving.knative.dev/lastModifier",
}
SERVER_REVISION_LABELS = {
    "client.knative.dev/nonce",
    "cloud.googleapis.com/location",
    "serving.knative.dev/configuration",
    "serving.knative.dev/configurationGeneration",
    "serving.knative.dev/route",
    "serving.knative.dev/service",
    "serving.knative.dev/serviceUid",
}
RELEASE_LABELS = {
    "fraeno-release-commit",
    "fraeno-release-version",
}


class CredentialRotationError(ValueError):
    pass


REQUIRED_STAGE_CHECKS = {
    "active_webhook_secret": "accepted",
    "previous_webhook_secret": "accepted",
    "invalid_webhook_secret": "rejected",
    "active_private_key": "accepted",
    "previous_private_key": "accepted",
}


def verify_previous_credential_versions(
    webhook_revision: Any,
    worker_revision: Any,
    *,
    expected_webhook_version: str,
    expected_private_key_version: str,
) -> dict[str, str]:
    for name, value in (
        ("expected_webhook_version", expected_webhook_version),
        ("expected_private_key_version", expected_private_key_version),
    ):
        if SECRET_VERSION.fullmatch(value) is None:
            raise CredentialRotationError(f"{name} must be an exact numeric version")
    actual_webhook = _secret_reference_version(
        webhook_revision,
        env_name="FRAENO_GITHUB_WEBHOOK_SECRET",
        secret_name=WEBHOOK_SECRET,
    )
    actual_private_key = _secret_reference_version(
        worker_revision,
        env_name="FRAENO_GITHUB_PRIVATE_KEY",
        secret_name=PRIVATE_KEY_SECRET,
    )
    if actual_webhook != expected_webhook_version:
        raise CredentialRotationError(
            "previous webhook version is not the active production version"
        )
    if actual_private_key != expected_private_key_version:
        raise CredentialRotationError(
            "previous private-key version is not the active production version"
        )
    return {
        "webhook_previous_version": actual_webhook,
        "private_key_previous_version": actual_private_key,
    }


def _secret_reference_version(
    revision: Any, *, env_name: str, secret_name: str
) -> str:
    if not isinstance(revision, dict):
        raise CredentialRotationError("active revision must be an object")
    spec = revision.get("spec")
    containers = spec.get("containers") if isinstance(spec, dict) else None
    if not isinstance(containers, list) or len(containers) != 1:
        raise CredentialRotationError("active revision must have one container")
    container = containers[0]
    env = container.get("env") if isinstance(container, dict) else None
    if not isinstance(env, list):
        raise CredentialRotationError("active revision has no environment")
    matches = [
        entry
        for entry in env
        if isinstance(entry, dict) and entry.get("name") == env_name
    ]
    if len(matches) != 1:
        raise CredentialRotationError(f"active revision must define {env_name}")
    source = matches[0].get("valueFrom")
    reference = source.get("secretKeyRef") if isinstance(source, dict) else None
    if (
        not isinstance(reference, dict)
        or reference.get("name") != secret_name
        or SECRET_VERSION.fullmatch(str(reference.get("key"))) is None
    ):
        raise CredentialRotationError(
            f"active revision must pin {env_name} to an exact {secret_name} version"
        )
    return str(reference["key"])


def _timestamp(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise CredentialRotationError(
            "timestamps must use RFC 3339 UTC"
        ) from error
    offset = parsed.utcoffset()
    if (
        parsed.tzinfo is None
        or offset is None
        or offset.total_seconds() != 0
    ):
        raise CredentialRotationError("timestamps must use RFC 3339 UTC")
    return parsed.astimezone(timezone.utc)


def validate_rotation_window(
    started_at: str, previous_valid_until: str
) -> CredentialRotationWindow:
    try:
        window = CredentialRotationWindow(
            started_at=_timestamp(started_at),
            previous_valid_until=_timestamp(previous_valid_until),
        )
    except SettingsError as error:
        raise CredentialRotationError(str(error)) from error
    duration = window.previous_valid_until - window.started_at
    if duration.total_seconds() <= 0:
        raise CredentialRotationError(
            "previous_valid_until must be after started_at"
        )
    if duration.total_seconds() > 3600:
        raise CredentialRotationError("credential overlap cannot exceed one hour")
    return window


def inspect_staged_rotation(
    *,
    webhook_service: Any,
    webhook_revision: Any,
    active_webhook_revision: Any,
    worker_service: Any,
    worker_revision: Any,
    active_worker_revision: Any,
    webhook_revision_name: str,
    worker_revision_name: str,
    webhook_active_version: str,
    webhook_previous_version: str,
    private_key_active_version: str,
    private_key_previous_version: str,
    webhook_image: str,
    worker_image: str,
    webhook_service_account: str,
    worker_service_account: str,
    started_at: str,
    previous_valid_until: str,
) -> dict[str, Any]:
    window = validate_rotation_window(started_at, previous_valid_until)
    for name, version in (
        ("webhook_active_version", webhook_active_version),
        ("webhook_previous_version", webhook_previous_version),
        ("private_key_active_version", private_key_active_version),
        ("private_key_previous_version", private_key_previous_version),
    ):
        if SECRET_VERSION.fullmatch(version) is None:
            raise CredentialRotationError(f"{name} must be an exact numeric version")
    if webhook_active_version == webhook_previous_version:
        raise CredentialRotationError("webhook versions must be different")
    if private_key_active_version == private_key_previous_version:
        raise CredentialRotationError("private-key versions must be different")

    webhook = _inspect_revision(
        service_payload=webhook_service,
        revision_payload=webhook_revision,
        active_revision_payload=active_webhook_revision,
        expected_service=WEBHOOK_SERVICE,
        expected_revision=webhook_revision_name,
        expected_env={
            "FRAENO_SERVICE_MODE": "webhook",
            "FRAENO_CREDENTIAL_ROTATION_STARTED_AT": started_at,
            "FRAENO_PREVIOUS_CREDENTIALS_VALID_UNTIL": previous_valid_until,
        },
        expected_secrets={
            "FRAENO_GITHUB_WEBHOOK_SECRET": (
                WEBHOOK_SECRET,
                webhook_active_version,
            ),
            "FRAENO_GITHUB_WEBHOOK_SECRET_PREVIOUS": (
                WEBHOOK_SECRET,
                webhook_previous_version,
            ),
        },
        expected_image=webhook_image,
        expected_service_account=webhook_service_account,
        allowed_rotation_env={
            "FRAENO_GITHUB_WEBHOOK_SECRET",
            "FRAENO_GITHUB_WEBHOOK_SECRET_PREVIOUS",
            "FRAENO_CREDENTIAL_ROTATION_STARTED_AT",
            "FRAENO_PREVIOUS_CREDENTIALS_VALID_UNTIL",
        },
    )
    worker = _inspect_revision(
        service_payload=worker_service,
        revision_payload=worker_revision,
        active_revision_payload=active_worker_revision,
        expected_service=WORKER_SERVICE,
        expected_revision=worker_revision_name,
        expected_env={
            "FRAENO_SERVICE_MODE": "worker",
            "FRAENO_CREDENTIAL_ROTATION_STARTED_AT": started_at,
            "FRAENO_PREVIOUS_CREDENTIALS_VALID_UNTIL": previous_valid_until,
        },
        expected_secrets={
            "FRAENO_GITHUB_PRIVATE_KEY": (
                PRIVATE_KEY_SECRET,
                private_key_active_version,
            ),
            "FRAENO_GITHUB_PRIVATE_KEY_PREVIOUS": (
                PRIVATE_KEY_SECRET,
                private_key_previous_version,
            ),
        },
        expected_image=worker_image,
        expected_service_account=worker_service_account,
        allowed_rotation_env={
            "FRAENO_GITHUB_PRIVATE_KEY",
            "FRAENO_GITHUB_PRIVATE_KEY_PREVIOUS",
            "FRAENO_CREDENTIAL_ROTATION_STARTED_AT",
            "FRAENO_PREVIOUS_CREDENTIALS_VALID_UNTIL",
        },
    )
    return {
        "schema_version": 1,
        "window": {
            "started_at": window.started_at.isoformat().replace("+00:00", "Z"),
            "previous_valid_until": (
                window.previous_valid_until.isoformat().replace("+00:00", "Z")
            ),
        },
        "secret_versions": {
            "webhook": {
                "active": webhook_active_version,
                "previous": webhook_previous_version,
            },
            "private_key": {
                "active": private_key_active_version,
                "previous": private_key_previous_version,
            },
        },
        "services": {"webhook": webhook, "worker": worker},
    }


def _inspect_revision(
    *,
    service_payload: Any,
    revision_payload: Any,
    active_revision_payload: Any,
    expected_service: str,
    expected_revision: str,
    expected_env: dict[str, str],
    expected_secrets: dict[str, tuple[str, str]],
    expected_image: str,
    expected_service_account: str,
    allowed_rotation_env: set[str],
) -> dict[str, str]:
    if REVISION_NAME.fullmatch(expected_revision) is None:
        raise CredentialRotationError("candidate revision name is invalid")
    if (
        not isinstance(service_payload, dict)
        or not isinstance(revision_payload, dict)
        or not isinstance(active_revision_payload, dict)
    ):
        raise CredentialRotationError("Cloud Run payloads must be objects")
    metadata = revision_payload.get("metadata")
    active_metadata = active_revision_payload.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("name") != expected_revision:
        raise CredentialRotationError(
            f"{expected_revision} does not match the revision payload"
        )
    if not isinstance(active_metadata, dict):
        raise CredentialRotationError("active revision has no metadata")
    labels = metadata.get("labels")
    if (
        not isinstance(labels, dict)
        or labels.get("serving.knative.dev/service") != expected_service
    ):
        raise CredentialRotationError(
            f"{expected_revision} does not belong to {expected_service}"
        )
    spec = revision_payload.get("spec")
    active_spec = active_revision_payload.get("spec")
    if not isinstance(spec, dict):
        raise CredentialRotationError(f"{expected_revision} has no specification")
    if not isinstance(active_spec, dict):
        raise CredentialRotationError("active revision has no specification")
    containers = spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise CredentialRotationError(
            f"{expected_revision} must have exactly one container"
        )
    container = containers[0]
    if not isinstance(container, dict):
        raise CredentialRotationError(f"{expected_revision} container is invalid")
    if container.get("image") != expected_image:
        raise CredentialRotationError(
            f"{expected_revision} does not use the reviewed active image"
        )
    if spec.get("serviceAccountName") != expected_service_account:
        raise CredentialRotationError(
            f"{expected_revision} does not use the reviewed runtime service account"
        )
    if _canonical_runtime_spec(spec, allowed_rotation_env) != (
        _canonical_runtime_spec(active_spec, allowed_rotation_env)
    ):
        raise CredentialRotationError(
            f"{expected_revision} runtime drifted from the active reviewed revision"
        )
    if _runtime_annotations(metadata) != _runtime_annotations(active_metadata):
        raise CredentialRotationError(
            f"{expected_revision} runtime annotations drifted from "
            "the active reviewed revision"
        )
    raw_env = container.get("env")
    if not isinstance(raw_env, list):
        raise CredentialRotationError(f"{expected_revision} has no environment")
    env = {
        entry.get("name"): entry
        for entry in raw_env
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    for name, expected_value in expected_env.items():
        entry = env.get(name)
        if not isinstance(entry, dict) or entry.get("value") != expected_value:
            raise CredentialRotationError(
                f"{expected_revision} has an unexpected {name}"
            )
    for name, (secret_name, secret_version) in expected_secrets.items():
        entry = env.get(name)
        source = entry.get("valueFrom") if isinstance(entry, dict) else None
        reference = (
            source.get("secretKeyRef") if isinstance(source, dict) else None
        )
        if (
            not isinstance(reference, dict)
            or reference.get("name") != secret_name
            or str(reference.get("key")) != secret_version
        ):
            raise CredentialRotationError(
                f"{expected_revision} does not pin {name} to the expected version"
            )

    status = service_payload.get("status")
    if not isinstance(status, dict):
        raise CredentialRotationError(f"{expected_service} has no status")
    traffic = status.get("traffic")
    if not isinstance(traffic, list):
        raise CredentialRotationError(f"{expected_service} has no traffic records")
    matches = [
        entry
        for entry in traffic
        if isinstance(entry, dict)
        and entry.get("revisionName") == expected_revision
        and entry.get("tag")
        and entry.get("url")
    ]
    if len(matches) != 1:
        raise CredentialRotationError(
            f"{expected_revision} must have one tagged no-traffic URL"
        )
    if int(matches[0].get("percent") or 0) != 0:
        raise CredentialRotationError(
            f"{expected_revision} must receive no production traffic"
        )
    url = matches[0]["url"]
    if not isinstance(url, str) or not url.startswith("https://"):
        raise CredentialRotationError(f"{expected_revision} has no HTTPS URL")
    return {
        "service": expected_service,
        "revision": expected_revision,
        "url": url,
        "image": expected_image,
        "service_account": expected_service_account,
    }


def _canonical_runtime_spec(
    spec: dict[str, Any], allowed_rotation_env: set[str]
) -> dict[str, Any]:
    normalized = copy.deepcopy(spec)
    containers = normalized.get("containers")
    if not isinstance(containers, list):
        return normalized
    for container in containers:
        if not isinstance(container, dict):
            continue
        env = container.get("env")
        if not isinstance(env, list):
            continue
        container["env"] = sorted(
            (
                entry
                for entry in env
                if isinstance(entry, dict)
                and entry.get("name") not in allowed_rotation_env
            ),
            key=lambda entry: str(entry.get("name")),
        )
    return normalized


def _runtime_annotations(metadata: dict[str, Any]) -> dict[str, str]:
    annotations = metadata.get("annotations", {})
    if not isinstance(annotations, dict):
        raise CredentialRotationError("revision annotations must be an object")
    return {
        str(name): str(value)
        for name, value in annotations.items()
        if name not in NON_RUNTIME_ANNOTATIONS
    }


@dataclass(frozen=True)
class _StdlibResponse:
    status_code: int
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


class _StdlibAsyncClient:
    async def post(
        self,
        url: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> _StdlibResponse:
        def send() -> _StdlibResponse:
            request = urllib.request.Request(
                url,
                data=content or b"",
                headers=headers or {},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    return _StdlibResponse(response.status, response.read())
            except urllib.error.HTTPError as error:
                return _StdlibResponse(error.code, error.read())

        return await asyncio.to_thread(send)

    async def aclose(self) -> None:
        return None


async def probe_staged_rotation(
    stage: dict[str, Any],
    *,
    active_webhook_secret: bytes,
    previous_webhook_secret: bytes,
    worker_identity_token: str,
    client: Any = None,
) -> dict[str, Any]:
    own_client = client is None
    active_client = client or _StdlibAsyncClient()
    try:
        services = stage.get("services", {})
        webhook_url = services.get("webhook", {}).get("url")
        worker_url = services.get("worker", {}).get("url")
        if not isinstance(webhook_url, str) or not isinstance(worker_url, str):
            raise CredentialRotationError("stage evidence has no candidate URLs")
        if not worker_identity_token:
            raise CredentialRotationError("worker identity token is required")
        body = b'{"zen":"Fraeno credential rotation staging probe"}'

        async def post_webhook(secret: bytes) -> int:
            signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
            response = await active_client.post(
                f"{webhook_url}/credential-readiness/webhook",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-hub-signature-256": f"sha256={signature}",
                },
            )
            return response.status_code

        active_status = await post_webhook(active_webhook_secret)
        previous_status = await post_webhook(previous_webhook_secret)
        invalid_response = await active_client.post(
            f"{webhook_url}/credential-readiness/webhook",
            content=body,
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": "sha256=" + ("0" * 64),
            },
        )
        worker_response = await active_client.post(
            f"{worker_url}/internal/credential-readiness",
            headers={
                "authorization": f"Bearer {worker_identity_token}",
                "x-fraeno-credential-check": "true",
            },
        )
        try:
            worker = worker_response.json()
        except json.JSONDecodeError as error:
            raise CredentialRotationError(
                "worker readiness response was not JSON"
            ) from error
        expected_worker = {
            "status": "ok",
            "active_key_valid": True,
            "previous_key_configured": True,
            "overlap_active": True,
            "previous_key_valid": True,
        }
        if (
            active_status != 204
            or previous_status != 204
            or invalid_response.status_code != 401
            or worker_response.status_code != 200
            or not isinstance(worker, dict)
            or any(worker.get(key) != value for key, value in expected_worker.items())
        ):
            raise CredentialRotationError(
                "staged credential verification did not pass every check"
            )
        return {
            "schema_version": 1,
            "verified_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "stage": stage,
            "checks": {
                "active_webhook_secret": "accepted",
                "previous_webhook_secret": "accepted",
                "invalid_webhook_secret": "rejected",
                "active_private_key": "accepted",
                "previous_private_key": "accepted",
            },
        }
    finally:
        if own_client:
            await active_client.aclose()


def authorize_retirement(
    evidence: Any,
    live_verification: Any,
    *,
    now: str,
) -> dict[str, Any]:
    if not isinstance(evidence, dict) or not isinstance(live_verification, dict):
        raise CredentialRotationError("rotation evidence must be JSON objects")
    stage = evidence.get("stage")
    if not isinstance(stage, dict):
        raise CredentialRotationError("staged verification evidence is missing")
    window = stage.get("window")
    if not isinstance(window, dict):
        raise CredentialRotationError("rotation window is missing")
    validate_rotation_window(
        str(window.get("started_at", "")),
        str(window.get("previous_valid_until", "")),
    )
    rotation_identity = _rotation_identity(stage)
    checks = evidence.get("checks")
    if not isinstance(checks, dict) or any(
        checks.get(name) != result for name, result in REQUIRED_STAGE_CHECKS.items()
    ):
        raise CredentialRotationError("staged verification is incomplete")
    verified_at = _timestamp(str(evidence.get("verified_at", "")))
    live_verified_at = _timestamp(
        str(live_verification.get("verified_at", ""))
    )
    observed_at = _timestamp(now)
    if not verified_at <= live_verified_at <= observed_at:
        raise CredentialRotationError(
            "live verification must follow staging and cannot be in the future"
        )
    delivery_id = live_verification.get("github_delivery_guid")
    if (
        not isinstance(delivery_id, str)
        or DELIVERY_GUID.fullmatch(delivery_id) is None
    ):
        raise CredentialRotationError(
            "live verification requires the GitHub delivery GUID"
        )
    installations = live_verification.get("installation_ids")
    check_runs = live_verification.get("check_run_ids")
    if (
        live_verification.get("active_webhook_secret_accepted") is not True
        or not isinstance(installations, list)
        or not installations
        or not all(isinstance(value, int) and value > 0 for value in installations)
        or not isinstance(check_runs, list)
        or not check_runs
        or not all(isinstance(value, int) and value > 0 for value in check_runs)
    ):
        raise CredentialRotationError(
            "live verification must prove a signed delivery and installation checks"
        )
    return {
        "schema_version": 1,
        "authorized_at": observed_at.isoformat().replace("+00:00", "Z"),
        "github_delivery_guid": delivery_id,
        "installation_ids": sorted(set(installations)),
        "check_run_ids": sorted(set(check_runs)),
        "rotation_identity": rotation_identity,
        "retirement_allowed": True,
    }


def _rotation_identity(stage: dict[str, Any]) -> dict[str, Any]:
    versions = stage.get("secret_versions")
    services = stage.get("services")
    if not isinstance(versions, dict) or not isinstance(services, dict):
        raise CredentialRotationError("staged rotation identity is incomplete")
    exact_versions: dict[str, dict[str, str]] = {}
    for credential in ("webhook", "private_key"):
        pair = versions.get(credential)
        if not isinstance(pair, dict):
            raise CredentialRotationError("staged rotation identity is incomplete")
        active = pair.get("active")
        previous = pair.get("previous")
        if (
            not isinstance(active, str)
            or not isinstance(previous, str)
            or SECRET_VERSION.fullmatch(active) is None
            or SECRET_VERSION.fullmatch(previous) is None
            or active == previous
        ):
            raise CredentialRotationError("staged credential versions are invalid")
        exact_versions[credential] = {
            "active": active,
            "previous": previous,
        }
    revisions: dict[str, str] = {}
    for role in ("webhook", "worker"):
        service = services.get(role)
        revision = service.get("revision") if isinstance(service, dict) else None
        if not isinstance(revision, str) or REVISION_NAME.fullmatch(revision) is None:
            raise CredentialRotationError("staged candidate revision is invalid")
        revisions[role] = revision
    return {
        "candidate_revisions": revisions,
        "secret_versions": exact_versions,
    }


def authorize_promotion(
    evidence: Any,
    *,
    now: str,
    minimum_remaining_seconds: int = 600,
) -> dict[str, Any]:
    if minimum_remaining_seconds <= 0:
        raise CredentialRotationError(
            "minimum remaining overlap must be positive"
        )
    if not isinstance(evidence, dict):
        raise CredentialRotationError("staged evidence must be a JSON object")
    stage = evidence.get("stage")
    checks = evidence.get("checks")
    if not isinstance(stage, dict) or not isinstance(checks, dict):
        raise CredentialRotationError("staged verification evidence is incomplete")
    if any(
        checks.get(name) != result for name, result in REQUIRED_STAGE_CHECKS.items()
    ):
        raise CredentialRotationError("staged verification evidence is incomplete")
    window = stage.get("window")
    services = stage.get("services")
    if not isinstance(window, dict) or not isinstance(services, dict):
        raise CredentialRotationError("staged verification evidence is incomplete")
    rotation = validate_rotation_window(
        str(window.get("started_at", "")),
        str(window.get("previous_valid_until", "")),
    )
    observed_at = _timestamp(now)
    verified_at = _timestamp(str(evidence.get("verified_at", "")))
    if not (
        rotation.started_at
        <= verified_at
        < rotation.previous_valid_until
    ):
        raise CredentialRotationError(
            "staged verification must occur during the overlap window"
        )
    if observed_at < verified_at:
        raise CredentialRotationError(
            "promotion time cannot precede staged verification"
        )
    remaining = (rotation.previous_valid_until - observed_at).total_seconds()
    if remaining < minimum_remaining_seconds:
        raise CredentialRotationError(
            "promotion requires at least ten minutes before overlap expiry; restage"
        )
    identity = _rotation_identity(stage)
    return {
        "schema_version": 1,
        "authorized_at": observed_at.isoformat().replace("+00:00", "Z"),
        "previous_valid_until": rotation.previous_valid_until.isoformat().replace(
            "+00:00", "Z"
        ),
        "minimum_remaining_seconds": minimum_remaining_seconds,
        "rotation_identity": identity,
        "promotion_allowed": True,
    }


def verify_checksum(path: Path, checksum_path: Path) -> str:
    try:
        fields = checksum_path.read_text().strip().split()
        expected = fields[0]
        named_file = fields[-1]
    except (OSError, IndexError) as error:
        raise CredentialRotationError("could not read evidence checksum") from error
    if (
        len(fields) != 2
        or re.fullmatch(r"[0-9a-f]{64}", expected) is None
        or Path(named_file).name != path.name
    ):
        raise CredentialRotationError("evidence checksum file is invalid")
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise CredentialRotationError("could not read staged evidence") from error
    if not hmac.compare_digest(actual, expected):
        raise CredentialRotationError("staged evidence checksum does not match")
    return actual


def validate_final_cleanup(
    service: Any,
    *,
    role: str,
    expected_revision: str,
    candidate_tag: str,
    expected_latest_revision: str = "",
) -> dict[str, Any]:
    if role not in {"webhook", "worker"}:
        raise CredentialRotationError("cleanup role is invalid")
    if (
        REVISION_NAME.fullmatch(expected_revision) is None
        or re.fullmatch(r"credential-[1-9][0-9]*", candidate_tag) is None
    ):
        raise CredentialRotationError("cleanup identity is invalid")
    if not isinstance(service, dict):
        raise CredentialRotationError("cleanup service response is invalid")
    status = service.get("status")
    if not isinstance(status, dict):
        raise CredentialRotationError("cleanup service status is missing")
    traffic = status.get("traffic")
    if not isinstance(traffic, list):
        raise CredentialRotationError("cleanup service traffic is missing")
    if expected_latest_revision and (
        REVISION_NAME.fullmatch(expected_latest_revision) is None
        or status.get("latestReadyRevisionName") != expected_latest_revision
    ):
        raise CredentialRotationError(
            f"{role} latest template was not restored"
        )
    active = [
        item
        for item in traffic
        if isinstance(item, dict) and int(item.get("percent") or 0) > 0
    ]
    if (
        len(active) != 1
        or active[0].get("revisionName") != expected_revision
        or int(active[0].get("percent") or 0) != 100
    ):
        raise CredentialRotationError(f"{role} production traffic changed")
    if any(
        isinstance(item, dict) and item.get("tag") == candidate_tag
        for item in traffic
    ):
        raise CredentialRotationError(f"{role} candidate tag was not removed")
    return {
        "role": role,
        "production_revision": expected_revision,
        "candidate_tag_removed": True,
        "production_traffic_unchanged": True,
        "latest_template_restored": bool(expected_latest_revision),
    }


def validate_restored_revision(
    restored_revision: Any,
    active_revision: Any,
    *,
    role: str,
    expected_revision: str,
) -> dict[str, Any]:
    if role not in {"webhook", "worker"}:
        raise CredentialRotationError("restore role is invalid")
    if REVISION_NAME.fullmatch(expected_revision) is None:
        raise CredentialRotationError("restored revision identity is invalid")
    if not isinstance(restored_revision, dict) or not isinstance(
        active_revision, dict
    ):
        raise CredentialRotationError("restore revision payload is invalid")
    restored_metadata = restored_revision.get("metadata")
    active_metadata = active_revision.get("metadata")
    if (
        not isinstance(restored_metadata, dict)
        or restored_metadata.get("name") != expected_revision
        or not isinstance(active_metadata, dict)
    ):
        raise CredentialRotationError("restored revision identity is invalid")
    restored_spec = restored_revision.get("spec")
    active_spec = active_revision.get("spec")
    if (
        not isinstance(restored_spec, dict)
        or not isinstance(active_spec, dict)
        or _canonical_runtime_spec(restored_spec, set())
        != _canonical_runtime_spec(active_spec, set())
        or _runtime_annotations(restored_metadata)
        != _runtime_annotations(active_metadata)
    ):
        raise CredentialRotationError(
            f"{role} latest template was not restored to the active configuration"
        )
    return {
        "role": role,
        "restored_revision": expected_revision,
        "active_configuration_restored": True,
    }


def validate_release_credential_source(
    service: Any,
    active_revision: Any,
    *,
    role: str,
) -> dict[str, Any]:
    active_name = (
        "FRAENO_GITHUB_WEBHOOK_SECRET"
        if role == "webhook"
        else "FRAENO_GITHUB_PRIVATE_KEY"
    )
    if role not in {"webhook", "worker"}:
        raise CredentialRotationError("release role is invalid")
    if not isinstance(service, dict) or not isinstance(active_revision, dict):
        raise CredentialRotationError("release source payload is invalid")
    template = service.get("spec", {}).get("template")
    template_spec = template.get("spec") if isinstance(template, dict) else None
    template_metadata = (
        template.get("metadata") if isinstance(template, dict) else None
    )
    active_spec = active_revision.get("spec")
    active_metadata = active_revision.get("metadata")
    if (
        not isinstance(template_spec, dict)
        or not isinstance(template_metadata, dict)
        or not isinstance(active_spec, dict)
        or not isinstance(active_metadata, dict)
    ):
        raise CredentialRotationError("release source specification is missing")

    def credential_env(spec: dict[str, Any]) -> dict[str, Any]:
        containers = spec.get("containers")
        if not isinstance(containers, list) or len(containers) != 1:
            raise CredentialRotationError("release source must have one container")
        container = containers[0]
        env = container.get("env") if isinstance(container, dict) else None
        if not isinstance(env, list):
            raise CredentialRotationError("release source environment is missing")
        return {
            str(entry.get("name")): entry
            for entry in env
            if isinstance(entry, dict)
            and entry.get("name") in ROTATION_ENV_NAMES | {active_name}
        }

    latest_credentials = credential_env(template_spec)
    active_credentials = credential_env(active_spec)
    if set(active_credentials) != {active_name}:
        raise CredentialRotationError(
            f"{role} production credential configuration is invalid"
        )
    if any(name in latest_credentials for name in ROTATION_ENV_NAMES):
        raise CredentialRotationError(
            f"{role} has a pending credential rotation"
        )
    if latest_credentials != active_credentials:
        raise CredentialRotationError(
            f"{role} latest template credentials drifted from production"
        )
    if _canonical_release_spec(template_spec) != _canonical_release_spec(
        active_spec
    ):
        raise CredentialRotationError(
            f"{role} latest template runtime drifted from production"
        )
    if _runtime_annotations(template_metadata) != _runtime_annotations(
        active_metadata
    ):
        raise CredentialRotationError(
            f"{role} latest template annotations drifted from production"
        )
    if _runtime_labels(template_metadata) != _runtime_labels(active_metadata):
        raise CredentialRotationError(
            f"{role} latest template labels drifted from production"
        )
    return {
        "role": role,
        "release_source_safe": True,
        "active_credential": active_credentials[active_name],
    }


def _canonical_release_spec(spec: dict[str, Any]) -> dict[str, Any]:
    normalized = _canonical_runtime_spec(spec, set())
    containers = normalized.get("containers")
    if isinstance(containers, list):
        for container in containers:
            if isinstance(container, dict):
                container.pop("image", None)
                container.pop("name", None)
    return normalized


def _runtime_labels(metadata: dict[str, Any]) -> dict[str, str]:
    labels = metadata.get("labels", {})
    if not isinstance(labels, dict):
        raise CredentialRotationError("revision labels must be an object")
    return {
        str(name): str(value)
        for name, value in labels.items()
        if name not in SERVER_REVISION_LABELS | RELEASE_LABELS
    }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise CredentialRotationError(f"could not read {path}") from error


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify Fraeno credential rotations without exposing secrets."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect-stage")
    inspect.add_argument("--webhook-service", type=Path, required=True)
    inspect.add_argument("--webhook-revision", type=Path, required=True)
    inspect.add_argument("--active-webhook-revision", type=Path, required=True)
    inspect.add_argument("--worker-service", type=Path, required=True)
    inspect.add_argument("--worker-revision", type=Path, required=True)
    inspect.add_argument("--active-worker-revision", type=Path, required=True)
    inspect.add_argument("--webhook-revision-name", required=True)
    inspect.add_argument("--worker-revision-name", required=True)
    inspect.add_argument("--webhook-active-version", required=True)
    inspect.add_argument("--webhook-previous-version", required=True)
    inspect.add_argument("--private-key-active-version", required=True)
    inspect.add_argument("--private-key-previous-version", required=True)
    inspect.add_argument("--webhook-image", required=True)
    inspect.add_argument("--worker-image", required=True)
    inspect.add_argument("--webhook-service-account", required=True)
    inspect.add_argument("--worker-service-account", required=True)
    inspect.add_argument("--started-at", required=True)
    inspect.add_argument("--previous-valid-until", required=True)
    inspect.add_argument("--output", type=Path, required=True)

    source = commands.add_parser("inspect-source")
    source.add_argument("--webhook-revision", type=Path, required=True)
    source.add_argument("--worker-revision", type=Path, required=True)
    source.add_argument("--webhook-previous-version", required=True)
    source.add_argument("--private-key-previous-version", required=True)
    source.add_argument("--output", type=Path, required=True)

    probe = commands.add_parser("probe-stage")
    probe.add_argument("--stage", type=Path, required=True)
    probe.add_argument("--active-webhook-secret-file", type=Path, required=True)
    probe.add_argument("--previous-webhook-secret-file", type=Path, required=True)
    probe.add_argument("--output", type=Path, required=True)

    retire = commands.add_parser("authorize-retirement")
    retire.add_argument("--evidence", type=Path, required=True)
    retire.add_argument("--live-verification", type=Path, required=True)
    retire.add_argument("--now", required=True)
    retire.add_argument("--output", type=Path, required=True)

    promote = commands.add_parser("authorize-promotion")
    promote.add_argument("--evidence", type=Path, required=True)
    promote.add_argument("--checksum", type=Path, required=True)
    promote.add_argument("--now", required=True)
    promote.add_argument("--output", type=Path, required=True)

    cleanup = commands.add_parser("validate-cleanup")
    cleanup.add_argument("--service", type=Path, required=True)
    cleanup.add_argument("--role", required=True)
    cleanup.add_argument("--expected-revision", required=True)
    cleanup.add_argument("--candidate-tag", required=True)
    cleanup.add_argument("--expected-latest-revision", default="")
    cleanup.add_argument("--output", type=Path, required=True)

    restore = commands.add_parser("validate-restore")
    restore.add_argument("--revision", type=Path, required=True)
    restore.add_argument("--active-revision", type=Path, required=True)
    restore.add_argument("--role", required=True)
    restore.add_argument("--expected-revision", required=True)
    restore.add_argument("--output", type=Path, required=True)

    release = commands.add_parser("validate-release-source")
    release.add_argument("--service", type=Path, required=True)
    release.add_argument("--active-revision", type=Path, required=True)
    release.add_argument("--role", required=True)
    release.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect-stage":
            payload = inspect_staged_rotation(
                webhook_service=_read_json(args.webhook_service),
                webhook_revision=_read_json(args.webhook_revision),
                active_webhook_revision=_read_json(
                    args.active_webhook_revision
                ),
                worker_service=_read_json(args.worker_service),
                worker_revision=_read_json(args.worker_revision),
                active_worker_revision=_read_json(args.active_worker_revision),
                webhook_revision_name=args.webhook_revision_name,
                worker_revision_name=args.worker_revision_name,
                webhook_active_version=args.webhook_active_version,
                webhook_previous_version=args.webhook_previous_version,
                private_key_active_version=args.private_key_active_version,
                private_key_previous_version=args.private_key_previous_version,
                webhook_image=args.webhook_image,
                worker_image=args.worker_image,
                webhook_service_account=args.webhook_service_account,
                worker_service_account=args.worker_service_account,
                started_at=args.started_at,
                previous_valid_until=args.previous_valid_until,
            )
        elif args.command == "inspect-source":
            payload = verify_previous_credential_versions(
                _read_json(args.webhook_revision),
                _read_json(args.worker_revision),
                expected_webhook_version=args.webhook_previous_version,
                expected_private_key_version=args.private_key_previous_version,
            )
        elif args.command == "probe-stage":
            payload = asyncio.run(
                probe_staged_rotation(
                    _read_json(args.stage),
                    active_webhook_secret=(
                        args.active_webhook_secret_file.read_bytes().strip()
                    ),
                    previous_webhook_secret=(
                        args.previous_webhook_secret_file.read_bytes().strip()
                    ),
                    worker_identity_token=os.environ.get(
                        "FRAENO_WORKER_ID_TOKEN", ""
                    ),
                )
            )
        elif args.command == "authorize-retirement":
            payload = authorize_retirement(
                _read_json(args.evidence),
                _read_json(args.live_verification),
                now=args.now,
            )
        elif args.command == "authorize-promotion":
            evidence_sha256 = verify_checksum(args.evidence, args.checksum)
            payload = authorize_promotion(
                _read_json(args.evidence),
                now=args.now,
            )
            payload["evidence_sha256"] = evidence_sha256
        elif args.command == "validate-cleanup":
            payload = validate_final_cleanup(
                _read_json(args.service),
                role=args.role,
                expected_revision=args.expected_revision,
                candidate_tag=args.candidate_tag,
                expected_latest_revision=args.expected_latest_revision,
            )
        elif args.command == "validate-restore":
            payload = validate_restored_revision(
                _read_json(args.revision),
                _read_json(args.active_revision),
                role=args.role,
                expected_revision=args.expected_revision,
            )
        else:
            payload = validate_release_credential_source(
                _read_json(args.service),
                _read_json(args.active_revision),
                role=args.role,
            )
        _write_json(args.output, payload)
    except CredentialRotationError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
