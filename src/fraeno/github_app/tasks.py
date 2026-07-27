from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Protocol

from google.api_core.exceptions import AlreadyExists

from fraeno.github_app.settings import WebhookSettings


class TaskEnqueuer(Protocol):
    async def enqueue(
        self,
        event: str,
        delivery_id: str,
        payload: dict[str, Any],
    ) -> bool: ...


class CloudTasksEnqueuer:
    def __init__(
        self,
        settings: WebhookSettings,
        client: Any | None = None,
    ) -> None:
        if client is None:
            from google.cloud import tasks_v2

            client = tasks_v2.CloudTasksAsyncClient()
        self.settings = settings
        self._client = client

    async def close(self) -> None:
        await self._client.transport.close()

    async def enqueue(
        self,
        event: str,
        delivery_id: str,
        payload: dict[str, Any],
    ) -> bool:
        from google.cloud import tasks_v2

        parent = self._client.queue_path(
            self.settings.gcp_project,
            self.settings.gcp_location,
            self.settings.queue_name,
        )
        task = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{self.settings.worker_url}/internal/github-events",
                "headers": {"Content-Type": "application/json"},
                "oidc_token": {
                    "service_account_email": self.settings.task_service_account,
                    "audience": self.settings.worker_url,
                },
                "body": json.dumps(
                    {
                        "event": event,
                        "delivery_id": delivery_id,
                        "enqueued_at": datetime.now(timezone.utc).isoformat(),
                        "payload": payload,
                    },
                    separators=(",", ":"),
                ).encode(),
            },
        }
        try:
            await self._client.create_task(request={"parent": parent, "task": task})
        except AlreadyExists:
            return False
        return True
