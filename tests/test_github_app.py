import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from fraeno.github_app.app import (
    MAX_REQUEST_BYTES,
    create_webhook_app,
    create_worker_app,
    limited_request_body,
)
from fraeno.github_app.settings import (
    AppSettings,
    CredentialRotationWindow,
    SettingsError,
    WebhookSettings,
)
from fraeno.github_app.store import MemoryEventStore


class RecordingEnqueuer:
    def __init__(self) -> None:
        self.events: dict[str, tuple[str, dict[str, Any]]] = {}

    async def enqueue(
        self, event: str, delivery_id: str, payload: dict[str, Any]
    ) -> bool:
        if delivery_id in self.events:
            return False
        self.events[delivery_id] = (event, payload)
        return True


class RecordingHandler:
    def __init__(self) -> None:
        self.store = MemoryEventStore()
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def process(
        self, event: str, delivery_id: str, payload: dict[str, Any]
    ) -> None:
        self.events.append((event, delivery_id, payload))
        await self.store.complete_delivery(delivery_id)


class FailingOnceHandler(RecordingHandler):
    async def process(
        self, event: str, delivery_id: str, payload: dict[str, Any]
    ) -> None:
        self.events.append((event, delivery_id, payload))
        if len(self.events) == 1:
            await self.store.fail_delivery(delivery_id)
            raise RuntimeError("temporary failure")
        await self.store.complete_delivery(delivery_id)


def webhook_settings() -> WebhookSettings:
    return WebhookSettings(
        webhook_secret="test-secret",
        gcp_project="project",
        gcp_location="us-central1",
        queue_name="queue",
        worker_url="https://worker.example",
        task_service_account="tasks@project.iam.gserviceaccount.com",
    )


def signed_headers(body: bytes, secret: str, delivery: str) -> dict[str, str]:
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "x-github-event": "pull_request",
        "x-github-delivery": delivery,
        "x-hub-signature-256": f"sha256={signature}",
    }


def task_headers(retry_count: int = 0) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "x-cloudtasks-taskname": "projects/project/tasks/test",
        "x-cloudtasks-taskretrycount": str(retry_count),
    }


def task_body(delivery_id: str = "delivery") -> dict[str, Any]:
    return {
        "event": "pull_request",
        "delivery_id": delivery_id,
        "payload": {"action": "opened"},
    }


def test_webhook_is_verified_and_deduplicated_by_queue() -> None:
    settings = webhook_settings()
    enqueuer = RecordingEnqueuer()
    app = create_webhook_app(settings, enqueuer)
    body = json.dumps({"action": "opened"}).encode()

    with TestClient(app) as client:
        first = client.post(
            "/webhooks/github",
            content=body,
            headers=signed_headers(body, settings.webhook_secret, "same-delivery"),
        )
        second = client.post(
            "/webhooks/github",
            content=body,
            headers=signed_headers(body, settings.webhook_secret, "same-delivery"),
        )

    assert first.status_code == 202
    assert second.status_code == 202
    assert len(enqueuer.events) == 1


def test_webhook_secret_ignores_file_ending_newline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRAENO_GITHUB_WEBHOOK_SECRET", "test-secret\n")
    monkeypatch.setenv("FRAENO_GCP_PROJECT", "project")
    monkeypatch.setenv("FRAENO_GCP_LOCATION", "us-central1")
    monkeypatch.setenv("FRAENO_TASK_QUEUE", "queue")
    monkeypatch.setenv("FRAENO_WORKER_URL", "https://worker.example")
    monkeypatch.setenv(
        "FRAENO_TASK_SERVICE_ACCOUNT",
        "tasks@project.iam.gserviceaccount.com",
    )
    settings = WebhookSettings.from_environment()
    enqueuer = RecordingEnqueuer()
    app = create_webhook_app(settings, enqueuer)
    body = json.dumps({"zen": "Keep it logically awesome."}).encode()

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/github",
            content=body,
            headers=signed_headers(body, "test-secret", "newline-delivery"),
        )

    assert settings.webhook_secret == "test-secret"
    assert response.status_code == 202
    assert "newline-delivery" in enqueuer.events


def test_rotation_environment_requires_a_bounded_complete_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRAENO_GITHUB_WEBHOOK_SECRET", "active-secret")
    monkeypatch.setenv(
        "FRAENO_GITHUB_WEBHOOK_SECRET_PREVIOUS", "previous-secret"
    )
    monkeypatch.setenv(
        "FRAENO_CREDENTIAL_ROTATION_STARTED_AT", "2026-07-27T20:00:00Z"
    )
    monkeypatch.setenv(
        "FRAENO_PREVIOUS_CREDENTIALS_VALID_UNTIL", "2026-07-27T21:00:00Z"
    )
    monkeypatch.setenv("FRAENO_GCP_PROJECT", "project")
    monkeypatch.setenv("FRAENO_GCP_LOCATION", "us-central1")
    monkeypatch.setenv("FRAENO_TASK_QUEUE", "queue")
    monkeypatch.setenv("FRAENO_WORKER_URL", "https://worker.example")
    monkeypatch.setenv(
        "FRAENO_TASK_SERVICE_ACCOUNT",
        "tasks@project.iam.gserviceaccount.com",
    )

    settings = WebhookSettings.from_environment()

    assert settings.previous_webhook_secret == "previous-secret"
    assert settings.credential_rotation is not None

    monkeypatch.setenv(
        "FRAENO_PREVIOUS_CREDENTIALS_VALID_UNTIL", "2026-07-27T21:00:01Z"
    )
    with pytest.raises(SettingsError, match="cannot exceed one hour"):
        WebhookSettings.from_environment()


def test_invalid_webhook_signature_is_rejected() -> None:
    settings = webhook_settings()
    app = create_webhook_app(settings, RecordingEnqueuer())

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/github",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "x-github-event": "ping",
                "x-github-delivery": "delivery",
                "x-hub-signature-256": "sha256=wrong",
            },
        )

    assert response.status_code == 401


def test_previous_webhook_secret_is_accepted_only_during_rotation() -> None:
    now = datetime.now(timezone.utc)
    monkeypatch_settings = WebhookSettings(
        webhook_secret="active-secret",
        gcp_project="project",
        gcp_location="us-central1",
        queue_name="queue",
        worker_url="https://worker.example",
        task_service_account="tasks@project.iam.gserviceaccount.com",
        previous_webhook_secret="previous-secret",
        credential_rotation=CredentialRotationWindow(
            started_at=now - timedelta(minutes=1),
            previous_valid_until=now + timedelta(minutes=1),
        ),
    )
    app = create_webhook_app(monkeypatch_settings, RecordingEnqueuer())
    body = b'{"action":"opened"}'

    with TestClient(app) as client:
        accepted = client.post(
            "/webhooks/github",
            content=body,
            headers=signed_headers(
                body, "previous-secret", "previous-secret-delivery"
            ),
        )

    assert accepted.status_code == 202


def test_expired_previous_webhook_secret_is_rejected() -> None:
    now = datetime.now(timezone.utc)
    settings = WebhookSettings(
        webhook_secret="active-secret",
        gcp_project="project",
        gcp_location="us-central1",
        queue_name="queue",
        worker_url="https://worker.example",
        task_service_account="tasks@project.iam.gserviceaccount.com",
        previous_webhook_secret="previous-secret",
        credential_rotation=CredentialRotationWindow(
            started_at=now - timedelta(minutes=2),
            previous_valid_until=now - timedelta(minutes=1),
        ),
    )
    app = create_webhook_app(settings, RecordingEnqueuer())
    body = b'{"action":"opened"}'

    with TestClient(app) as client:
        rejected = client.post(
            "/webhooks/github",
            content=body,
            headers=signed_headers(
                body, "previous-secret", "expired-secret-delivery"
            ),
        )

    assert rejected.status_code == 401


def test_credential_readiness_verifies_both_secrets_without_enqueueing() -> None:
    now = datetime.now(timezone.utc)
    settings = WebhookSettings(
        webhook_secret="active-secret",
        gcp_project="project",
        gcp_location="us-central1",
        queue_name="queue",
        worker_url="https://worker.example",
        task_service_account="tasks@project.iam.gserviceaccount.com",
        previous_webhook_secret="previous-secret",
        credential_rotation=CredentialRotationWindow(
            started_at=now - timedelta(minutes=1),
            previous_valid_until=now + timedelta(minutes=1),
        ),
    )
    enqueuer = RecordingEnqueuer()
    app = create_webhook_app(settings, enqueuer)
    body = b'{"probe":"credential-readiness"}'

    with TestClient(app) as client:
        active = client.post(
            "/credential-readiness/webhook",
            content=body,
            headers=signed_headers(body, "active-secret", "unused-active"),
        )
        previous = client.post(
            "/credential-readiness/webhook",
            content=body,
            headers=signed_headers(body, "previous-secret", "unused-previous"),
        )
        invalid = client.post(
            "/credential-readiness/webhook",
            content=body,
            headers=signed_headers(body, "invalid-secret", "unused-invalid"),
        )

    assert active.status_code == 204
    assert previous.status_code == 204
    assert invalid.status_code == 401
    assert enqueuer.events == {}


def test_credential_readiness_enforces_request_size_limit() -> None:
    app = create_webhook_app(webhook_settings(), RecordingEnqueuer())

    with TestClient(app) as client:
        response = client.post(
            "/credential-readiness/webhook",
            content=b"{}",
            headers={"content-length": str(1_000_001)},
        )

    assert response.status_code == 413


def test_chunked_request_stops_before_buffering_the_full_body() -> None:
    chunks = [b"x" * 65_536 for _ in range(32)]
    receive_calls = 0
    sent_messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal receive_calls
        receive_calls += 1
        chunk = chunks.pop(0)
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": bool(chunks),
        }

    async def send(message: dict[str, Any]) -> None:
        sent_messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/webhooks/github",
        "raw_path": b"/webhooks/github",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("fraeno.test", 443),
    }
    app = create_webhook_app(webhook_settings(), RecordingEnqueuer())
    asyncio.run(app(scope, receive, send))

    response_start = next(
        message
        for message in sent_messages
        if message["type"] == "http.response.start"
    )
    assert response_start["status"] == 413
    assert receive_calls == 16
    assert len(chunks) == 16


@pytest.mark.parametrize("declared_length", ["-1", "invalid", "1000001"])
def test_invalid_or_oversized_declared_length_rejects_without_reading(
    declared_length: str,
) -> None:
    receive_calls = 0

    async def receive() -> dict[str, Any]:
        nonlocal receive_calls
        receive_calls += 1
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "headers": [(b"content-length", declared_length.encode())],
        },
        receive,
    )
    with pytest.raises(HTTPException) as raised:
        asyncio.run(limited_request_body(request))

    assert raised.value.status_code == 413
    assert receive_calls == 0


def test_chunked_request_accepts_the_exact_limit() -> None:
    chunks = [b"x" * 400_000, b"y" * 600_000]

    async def receive() -> dict[str, Any]:
        chunk = chunks.pop(0)
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": bool(chunks),
        }

    request = Request({"type": "http", "headers": []}, receive)
    body = asyncio.run(limited_request_body(request))

    assert len(body) == MAX_REQUEST_BYTES
    assert body[:1] == b"x"
    assert body[-1:] == b"y"
    assert chunks == []


def test_worker_processes_and_deduplicates_a_delivery() -> None:
    handler = RecordingHandler()
    app = create_worker_app(
        AppSettings("1", "key"),
        handler,  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        first = client.post(
            "/internal/github-events",
            json=task_body("same-delivery"),
            headers=task_headers(),
        )
        second = client.post(
            "/internal/github-events",
            json=task_body("same-delivery"),
            headers=task_headers(),
        )

    assert first.status_code == 204
    assert second.status_code == 204
    assert len(handler.events) == 1


def test_worker_reclaims_a_failed_delivery_on_cloud_tasks_retry() -> None:
    handler = FailingOnceHandler()
    app = create_worker_app(
        AppSettings("1", "key"),
        handler,  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        first = client.post(
            "/internal/github-events",
            json=task_body(),
            headers=task_headers(),
        )
        second = client.post(
            "/internal/github-events",
            json=task_body(),
            headers=task_headers(retry_count=1),
        )

    assert first.status_code == 500
    assert second.status_code == 204
    assert len(handler.events) == 2


def test_public_and_private_routes_are_separated() -> None:
    webhook_app = create_webhook_app(webhook_settings(), RecordingEnqueuer())
    worker_app = create_worker_app(
        AppSettings("1", "key"),
        RecordingHandler(),  # type: ignore[arg-type]
    )

    with TestClient(webhook_app) as webhook_client:
        assert webhook_client.post("/internal/github-events").status_code == 404
    with TestClient(worker_app) as worker_client:
        assert worker_client.post("/webhooks/github").status_code == 404
