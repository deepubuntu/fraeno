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
async def test_cloud_task_is_deterministic_and_oidc_authenticated() -> None:
    client = FakeTasksClient()
    enqueuer = CloudTasksEnqueuer(settings(), client)

    created = await enqueuer.enqueue(
        "pull_request",
        "delivery-1",
        {"action": "opened"},
    )

    assert created is True
    task = client.requests[0]["task"]
    assert task["name"].startswith(
        "projects/deepubuntu/locations/us-central1/queues/events/tasks/github-"
    )
    request = task["http_request"]
    assert request["url"] == "https://worker.example/internal/github-events"
    assert request["oidc_token"]["audience"] == "https://worker.example"
    assert json.loads(request["body"]) == {
        "event": "pull_request",
        "delivery_id": "delivery-1",
        "payload": {"action": "opened"},
    }


@pytest.mark.anyio
async def test_duplicate_cloud_task_is_accepted_without_requeueing() -> None:
    enqueuer = CloudTasksEnqueuer(settings(), FakeTasksClient(duplicate=True))

    created = await enqueuer.enqueue("ping", "same-delivery", {})

    assert created is False
