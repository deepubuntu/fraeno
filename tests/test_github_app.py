import hashlib
import hmac
import json
from typing import Any

from fastapi.testclient import TestClient

from fraeno.github_app.app import create_webhook_app, create_worker_app
from fraeno.github_app.settings import AppSettings, WebhookSettings
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
