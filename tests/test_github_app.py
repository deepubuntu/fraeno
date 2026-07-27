import hashlib
import hmac
import json
from typing import Any

from fastapi.testclient import TestClient

from fraeno.github_app.app import create_app
from fraeno.github_app.settings import AppSettings
from fraeno.github_app.store import MemoryEventStore


class RecordingHandler:
    def __init__(self) -> None:
        self.store = MemoryEventStore()
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def process(
        self, event: str, delivery_id: str, payload: dict[str, Any]
    ) -> None:
        self.events.append((event, delivery_id, payload))
        await self.store.complete_delivery(delivery_id)


def signed_headers(body: bytes, secret: str, delivery: str) -> dict[str, str]:
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "x-github-event": "pull_request",
        "x-github-delivery": delivery,
        "x-hub-signature-256": f"sha256={signature}",
    }


def test_webhook_is_verified_and_deduplicated() -> None:
    settings = AppSettings("1", "key", "test-secret")
    handler = RecordingHandler()
    app = create_app(settings, handler)  # type: ignore[arg-type]
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
    assert len(handler.events) == 1


def test_invalid_webhook_signature_is_rejected() -> None:
    settings = AppSettings("1", "key", "test-secret")
    handler = RecordingHandler()
    app = create_app(settings, handler)  # type: ignore[arg-type]

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
