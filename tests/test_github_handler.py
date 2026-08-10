from dataclasses import replace
from typing import Any

import pytest

from fraeno.github_app.client import (
    CheckRun,
    GitHubApiError,
    PullRequestState,
    WorkflowRun,
)
from fraeno.github_app.handler import EventHandler
from fraeno.github_app.settings import AppSettings
from fraeno.github_app.store import MemoryEventStore


def passing_report() -> dict[str, Any]:
    return {
        "outcome": "pass",
        "baseline": {"succeeded": True, "error": None},
        "candidate": {"succeeded": True, "error": None},
        "comparison": {
            "outcome": "pass",
            "validation_level": "L2",
            "findings": [],
        },
    }


class FakeGitHubClient:
    def __init__(self) -> None:
        self.settings = AppSettings("1", "key")
        self.updates: list[dict[str, Any]] = []
        self.dispatches: list[dict[str, Any]] = []
        self.cancellations: list[int] = []
        self.next_check_id = 200
        self.next_run_id = 300
        self.pull = PullRequestState(
            number=7,
            title="Update sensor driver from 1.0.0 to 1.0.1",
            state="open",
            draft=False,
            head_sha="candidate-sha",
            base_sha="baseline-sha",
            head_repository="contributor/fraeno-fork",
            base_repository="deepubuntu/fraeno",
        )
        self.reports: dict[int, dict[str, Any] | None] = {}
        self.report_error: GitHubApiError | None = None

    async def installation_token(
        self, installation_id: int, repository_id: int
    ) -> str:
        assert installation_id == 42
        assert repository_id == 100
        return "installation-token"

    async def pull_request(
        self, repository: str, pull_request_number: int, token: str
    ) -> PullRequestState:
        assert repository in {"deepubuntu/fraeno", "acme/warehouse-robot"}
        assert pull_request_number == 7
        assert token == "installation-token"
        return self.pull

    async def create_check_run(
        self, repository: str, head_sha: str, token: str, external_id: str
    ) -> CheckRun:
        assert repository in {"deepubuntu/fraeno", "acme/warehouse-robot"}
        assert head_sha == self.pull.head_sha
        assert token == "installation-token"
        assert external_id.startswith("fraeno:")
        check_id = self.next_check_id
        self.next_check_id += 1
        return CheckRun(check_id, f"https://github.test/check/{check_id}")

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
        run_id = self.next_run_id
        self.next_run_id += 1
        self.dispatches.append(
            {
                "repository": repository,
                "default_branch": default_branch,
                **inputs,
            }
        )
        self.reports.setdefault(run_id, passing_report())
        return WorkflowRun(run_id, f"https://github.test/actions/runs/{run_id}")

    async def find_workflow_run(
        self,
        repository: str,
        token: str,
        delivery_id: str,
    ) -> WorkflowRun | None:
        return None

    async def cancel_workflow_run(
        self, repository: str, workflow_run_id: int, token: str
    ) -> None:
        assert repository in {"deepubuntu/fraeno", "acme/warehouse-robot"}
        assert token == "installation-token"
        self.cancellations.append(workflow_run_id)

    async def validation_report(
        self,
        repository: str,
        workflow_run_id: int,
        check_run_id: int,
        token: str,
    ) -> dict[str, Any] | None:
        if self.report_error is not None:
            raise self.report_error
        return self.reports.get(workflow_run_id)

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


def pull_request_payload(action: str = "opened") -> dict[str, Any]:
    return {
        "action": action,
        "installation": {"id": 42},
        "repository": {
            "id": 100,
            "full_name": "deepubuntu/fraeno",
            "default_branch": "main",
        },
        "pull_request": {
            "number": 7,
            "draft": action == "converted_to_draft",
            "head": {"sha": "candidate-sha"},
            "base": {"sha": "baseline-sha"},
        },
    }


def workflow_completed(
    workflow_run_id: int,
    conclusion: str = "success",
) -> dict[str, Any]:
    return {
        "action": "completed",
        "repository": {"id": 100},
        "workflow_run": {
            "id": workflow_run_id,
            "path": ".github/workflows/fraeno-validation.yml",
            "conclusion": conclusion,
            "html_url": f"https://github.test/actions/runs/{workflow_run_id}",
        },
    }


@pytest.mark.anyio
async def test_pull_request_dispatches_and_completes_actionable_check() -> None:
    client = FakeGitHubClient()
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]

    await handler.process("pull_request", "delivery-1", pull_request_payload())

    record = await store.get_run(300)
    assert record is not None
    assert record.check_run_id == 200
    assert record.change == "Update sensor driver from 1.0.0 to 1.0.1"
    assert client.dispatches[0]["head_sha"] == "candidate-sha"
    assert client.dispatches[0]["head_repository"] == "contributor/fraeno-fork"
    assert client.dispatches[0]["base_repository"] == "deepubuntu/fraeno"
    assert client.updates[0]["status"] == "in_progress"
    assert "**Change** Update sensor driver" in client.updates[0]["summary"]

    await handler.process("workflow_run", "delivery-2", workflow_completed(300))

    assert client.updates[-1]["status"] == "completed"
    assert client.updates[-1]["conclusion"] == "success"
    assert "**Validation** L2" in client.updates[-1]["summary"]
    assert await store.get_active_run(100, 7) is None


@pytest.mark.anyio
async def test_new_commit_neutralizes_and_cancels_superseded_run() -> None:
    client = FakeGitHubClient()
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]
    await handler.process("pull_request", "delivery-1", pull_request_payload())

    client.pull = replace(client.pull, head_sha="new-candidate-sha")
    await handler.process(
        "pull_request",
        "delivery-2",
        pull_request_payload("synchronize"),
    )

    assert client.cancellations == [300]
    neutralized = [
        update for update in client.updates if update.get("conclusion") == "neutral"
    ]
    assert len(neutralized) == 1
    assert neutralized[0]["check_run_id"] == 200
    assert client.dispatches[-1]["head_sha"] == "new-candidate-sha"
    active = await store.get_active_run(100, 7)
    assert active is not None
    assert active.workflow_run_id == 301


@pytest.mark.anyio
async def test_superseded_completion_cannot_update_newer_check() -> None:
    client = FakeGitHubClient()
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]
    await handler.process("pull_request", "delivery-1", pull_request_payload())
    client.pull = replace(client.pull, head_sha="new-candidate-sha")
    await handler.process(
        "pull_request",
        "delivery-2",
        pull_request_payload("synchronize"),
    )
    updates_before_stale_completion = list(client.updates)

    await handler.process("workflow_run", "delivery-3", workflow_completed(300))

    assert client.updates == updates_before_stale_completion
    active = await store.get_active_run(100, 7)
    assert active is not None
    assert active.workflow_run_id == 301


@pytest.mark.anyio
async def test_unannounced_head_change_neutralizes_stale_completion() -> None:
    client = FakeGitHubClient()
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]
    await handler.process("pull_request", "delivery-1", pull_request_payload())
    client.pull = replace(client.pull, head_sha="new-candidate-sha")

    await handler.process("workflow_run", "delivery-2", workflow_completed(300))

    assert client.cancellations == [300]
    assert client.updates[-1]["conclusion"] == "neutral"
    assert "no longer current" in client.updates[-1]["title"]
    assert await store.get_active_run(100, 7) is None


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["closed", "converted_to_draft"])
async def test_non_mergeable_pull_request_stops_active_validation(
    action: str,
) -> None:
    client = FakeGitHubClient()
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]
    await handler.process("pull_request", "delivery-1", pull_request_payload())

    await handler.process(
        "pull_request",
        f"delivery-{action}",
        pull_request_payload(action),
    )

    assert client.cancellations == [300]
    assert client.updates[-1]["conclusion"] == "neutral"
    assert await store.get_active_run(100, 7) is None


@pytest.mark.anyio
async def test_rerequest_creates_exactly_one_clear_attempt() -> None:
    client = FakeGitHubClient()
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]
    await handler.process("pull_request", "delivery-1", pull_request_payload())
    await handler.process("workflow_run", "delivery-2", workflow_completed(300))

    await handler.process(
        "check_run",
        "delivery-rerun",
        {
            "action": "rerequested",
            "installation": {"id": 42},
            "repository": {
                "id": 100,
                "full_name": "deepubuntu/fraeno",
                "default_branch": "main",
            },
            "check_run": {
                "head_sha": "candidate-sha",
                "pull_requests": [{"number": 7}],
            },
        },
    )

    assert len(client.dispatches) == 2
    assert client.cancellations == []
    active = await store.get_active_run(100, 7)
    assert active is not None
    assert active.workflow_run_id == 301


@pytest.mark.anyio
async def test_regression_check_lists_failed_entity_concisely() -> None:
    client = FakeGitHubClient()
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]
    await handler.process("pull_request", "delivery-1", pull_request_payload())
    client.reports[300] = {
        "outcome": "block",
        "baseline": {"succeeded": True, "error": None},
        "candidate": {"succeeded": True, "error": None},
        "comparison": {
            "outcome": "block",
            "validation_level": "L2",
            "findings": [
                {
                    "code": "topic-rate-regressed",
                    "entity": "/robot/command",
                    "message": "Rate fell from 10 Hz to 0 Hz.",
                }
            ],
        },
    }

    await handler.process(
        "workflow_run",
        "delivery-2",
        workflow_completed(300, "failure"),
    )

    update = client.updates[-1]
    assert update["title"] == "Robot regression blocked this change"
    assert "`/robot/command` Rate fell from 10 Hz to 0 Hz." in update["summary"]


@pytest.mark.anyio
async def test_unreadable_report_is_shown_as_infrastructure_failure() -> None:
    client = FakeGitHubClient()
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]
    await handler.process("pull_request", "delivery-1", pull_request_payload())
    client.report_error = GitHubApiError("artifact is malformed")

    await handler.process(
        "workflow_run",
        "delivery-2",
        workflow_completed(300, "failure"),
    )

    update = client.updates[-1]
    assert update["title"] == "Infrastructure failure blocked this change"
    assert update["conclusion"] == "failure"
    assert await store.get_active_run(100, 7) is None


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


@pytest.mark.anyio
async def test_unapproved_installation_gets_a_private_beta_check() -> None:
    client = FakeGitHubClient()
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]

    payload = pull_request_payload()
    payload["repository"]["full_name"] = "acme/warehouse-robot"

    await handler.process("pull_request", "delivery-1", payload)

    assert client.dispatches == []
    final = client.updates[-1]
    assert final["status"] == "completed"
    assert final["conclusion"] == "neutral"
    assert final["title"] == "Fraeno is in private beta"
    assert "https://fraeno.com/" in final["summary"]
    record = await store.get_repository(100)
    assert record is not None and record.status == "not_approved"


@pytest.mark.anyio
async def test_wildcard_allowlist_admits_any_installation() -> None:
    client = FakeGitHubClient()
    client.settings = replace(
        client.settings, approved_installation_logins=("*",)
    )
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]

    payload = pull_request_payload()
    payload["repository"]["full_name"] = "acme/warehouse-robot"

    await handler.process("pull_request", "delivery-1", payload)

    assert len(client.dispatches) == 1
