from typing import Any

import pytest

from fraeno.github_app.client import CheckRun, WorkflowRun
from fraeno.github_app.handler import EventHandler
from fraeno.github_app.settings import AppSettings
from fraeno.github_app.store import MemoryEventStore


class FakeGitHubClient:
    def __init__(self) -> None:
        self.settings = AppSettings("1", "key")
        self.updates: list[dict[str, Any]] = []
        self.dispatches: list[dict[str, Any]] = []

    async def installation_token(
        self, installation_id: int, repository_id: int
    ) -> str:
        assert installation_id == 42
        assert repository_id == 100
        return "installation-token"

    async def create_check_run(
        self, repository: str, head_sha: str, token: str, external_id: str
    ) -> CheckRun:
        assert repository == "deepubuntu/fraeno"
        assert token == "installation-token"
        assert external_id.startswith("fraeno:")
        return CheckRun(200, "https://github.test/check/200")

    async def find_check_run(
        self,
        repository: str,
        head_sha: str,
        token: str,
        external_id: str,
    ) -> CheckRun | None:
        return None

    async def dispatch_workflow(
        self,
        repository: str,
        default_branch: str,
        token: str,
        **inputs: Any,
    ) -> WorkflowRun:
        self.dispatches.append(
            {
                "repository": repository,
                "default_branch": default_branch,
                **inputs,
            }
        )
        return WorkflowRun(300, "https://github.test/actions/runs/300")

    async def find_workflow_run(
        self,
        repository: str,
        token: str,
        delivery_id: str,
    ) -> WorkflowRun | None:
        return None

    async def update_check_run(
        self,
        repository: str,
        check_run_id: int,
        token: str,
        **values: Any,
    ) -> None:
        self.updates.append(
            {
                "repository": repository,
                "check_run_id": check_run_id,
                **values,
            }
        )


def pull_request_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "installation": {"id": 42},
        "repository": {
            "id": 100,
            "full_name": "deepubuntu/fraeno",
            "default_branch": "main",
        },
        "pull_request": {
            "number": 7,
            "draft": False,
            "head": {"sha": "candidate-sha"},
            "base": {"sha": "baseline-sha"},
        },
    }


@pytest.mark.anyio
async def test_pull_request_dispatches_and_completes_check() -> None:
    client = FakeGitHubClient()
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]

    await handler.process("pull_request", "delivery-1", pull_request_payload())

    record = await store.get_run(300)
    assert record is not None
    assert record.check_run_id == 200
    assert client.dispatches[0]["head_sha"] == "candidate-sha"
    assert client.updates[0]["status"] == "in_progress"

    await handler.process(
        "workflow_run",
        "delivery-2",
        {
            "action": "completed",
            "repository": {"id": 100},
            "workflow_run": {
                "id": 300,
                "path": ".github/workflows/fraeno-validation.yml",
                "conclusion": "success",
                "html_url": "https://github.test/actions/runs/300",
            },
        },
    )

    assert client.updates[-1]["status"] == "completed"
    assert client.updates[-1]["conclusion"] == "success"


@pytest.mark.anyio
async def test_draft_pull_request_is_ignored() -> None:
    client = FakeGitHubClient()
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]
    payload = pull_request_payload()
    payload["pull_request"]["draft"] = True

    await handler.process("pull_request", "delivery-draft", payload)

    assert client.dispatches == []
    assert client.updates == []
