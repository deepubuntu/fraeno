import json
from typing import Any

import pytest
from google.api_core.exceptions import AlreadyExists

from fraeno.github_app.settings import WebhookSettings
from fraeno.github_app.tasks import CloudTasksEnqueuer


class FakeTasksClient:
    def __init__(self, *, duplicate: bool = False) -> None:
        self.duplicate = duplicate
        self.requests: list[dict[str, Any]] = []

    def queue_path(self, project: str, location: str, queue: str) -> str:
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def task_path(
        self, project: str, location: str, queue: str, task: str
    ) -> str:
        return f"{self.queue_path(project, location, queue)}/tasks/{task}"

    async def create_task(self, request: dict[str, Any]) -> None:
        if self.duplicate:
            raise AlreadyExists("duplicate")
        self.requests.append(request)


def settings() -> WebhookSettings:
    return WebhookSettings(
        webhook_secret="secret",
        gcp_project="deepubuntu",
        gcp_location="us-central1",
        queue_name="events",
        worker_url="https://worker.example",
        task_service_account="tasks@deepubuntu.iam.gserviceaccount.com",
    )


@pytest.mark.anyio
async def test_cloud_task_is_oidc_authenticated_and_replayable() -> None:
    client = FakeTasksClient()
    enqueuer = CloudTasksEnqueuer(settings(), client)

    created = await enqueuer.enqueue(
        "pull_request",
        "delivery-1",
        {"action": "opened"},
    )

    assert created is True
    task = client.requests[0]["task"]
    assert "name" not in task
    request = task["http_request"]
    assert request["url"] == "https://worker.example/internal/github-events"
    assert request["oidc_token"]["audience"] == "https://worker.example"
    body = json.loads(request["body"])
    assert body["event"] == "pull_request"
    assert body["delivery_id"] == "delivery-1"
    assert body["payload"] == {"action": "opened"}
    assert body["enqueued_at"].endswith("+00:00")


@pytest.mark.anyio
async def test_duplicate_cloud_task_is_accepted_without_requeueing() -> None:
    enqueuer = CloudTasksEnqueuer(settings(), FakeTasksClient(duplicate=True))

    created = await enqueuer.enqueue("ping", "same-delivery", {})

    assert created is False
