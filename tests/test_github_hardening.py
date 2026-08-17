import argparse
import hashlib
import hmac
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fraeno.github_app.app import create_webhook_app, create_worker_app
from fraeno.github_app.client import CheckRun, PullRequestState, WorkflowRun
from fraeno.github_app.handler import EventHandler
from fraeno.github_app.operations import replay_delivery
from fraeno.github_app.recovery import Reconciler, ReconciliationResult
from fraeno.github_app.settings import AppSettings, WebhookSettings
from fraeno.github_app.store import MemoryEventStore, RunRecord


class RecordingMetrics:
    def __init__(self) -> None:
        self.values: list[tuple[str, float]] = []

    def emit(self, name: str, value: float = 1.0) -> None:
        self.values.append((name, value))


class MultiRepositoryGitHub:
    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or AppSettings(
            "1", "key", approved_installation_logins=("robotics",)
        )
        self.workflow_readiness: dict[str, bool] = {}
        self.workflow_states: dict[int, WorkflowRun] = {}
        self.reports: dict[int, dict[str, Any] | None] = {}
        self.updates: list[dict[str, Any]] = []
        self.cancellations: list[int] = []
        self.next_check_id = 200
        self.next_run_id = 300
        self.installations: list[dict[str, Any]] = []

    async def app_installations(self) -> list[dict[str, Any]]:
        return self.installations

    async def installation_token(
        self, installation_id: int, repository_id: int
    ) -> str:
        return f"token-{installation_id}-{repository_id}"

    async def workflow_available(self, repository: str, token: str) -> bool:
        assert token.startswith("token-")
        return self.workflow_readiness.get(repository, True)

    async def pull_request(
        self, repository: str, pull_request_number: int, token: str
    ) -> PullRequestState:
        assert token.startswith("token-")
        return PullRequestState(
            number=pull_request_number,
            title=f"Update {repository}",
            state="open",
            draft=False,
            head_sha=f"head-{repository}",
            base_sha=f"base-{repository}",
            head_repository=repository,
            base_repository=repository,
        )

    async def find_check_run(
        self,
        repository: str,
        head_sha: str,
        token: str,
        external_id: str,
    ) -> CheckRun | None:
        return None

    async def create_check_run(
        self,
        repository: str,
        head_sha: str,
        token: str,
        external_id: str,
    ) -> CheckRun:
        check = CheckRun(
            self.next_check_id,
            f"https://github.test/checks/{self.next_check_id}",
        )
        self.next_check_id += 1
        return check

    async def find_workflow_run(
        self, repository: str, token: str, delivery_id: str
    ) -> WorkflowRun | None:
        return None

    async def dispatch_workflow(
        self,
        repository: str,
        default_branch: str,
        token: str,
        **values: Any,
    ) -> WorkflowRun:
        del default_branch, token, values
        run = WorkflowRun(
            self.next_run_id,
            f"https://github.test/{repository}/runs/{self.next_run_id}",
        )
        self.next_run_id += 1
        return run

    async def workflow_run(
        self, repository: str, workflow_run_id: int, token: str
    ) -> WorkflowRun:
        del repository, token
        return self.workflow_states[workflow_run_id]

    async def validation_report(
        self,
        repository: str,
        workflow_run_id: int,
        check_run_id: int,
        token: str,
    ) -> dict[str, Any] | None:
        del repository, check_run_id, token
        return self.reports.get(workflow_run_id)

    async def update_check_run(
        self,
        repository: str,
        check_run_id: int,
        token: str,
        **values: Any,
    ) -> None:
        del token
        self.updates.append(
            {
                "repository": repository,
                "check_run_id": check_run_id,
                **values,
            }
        )

    async def cancel_workflow_run(
        self, repository: str, workflow_run_id: int, token: str
    ) -> None:
        del repository, token
        self.cancellations.append(workflow_run_id)


def repository(repository_id: int, name: str) -> dict[str, Any]:
    return {
        "id": repository_id,
        "full_name": name,
        "default_branch": "main",
    }


def pull_request_payload(
    repository_id: int, name: str, pull_request_number: int
) -> dict[str, Any]:
    return {
        "action": "opened",
        "installation": {"id": 42},
        "repository": repository(repository_id, name),
        "pull_request": {
            "number": pull_request_number,
            "draft": False,
        },
    }


@pytest.mark.anyio
async def test_installation_tracks_repository_readiness_without_source() -> None:
    client = MultiRepositoryGitHub()
    client.workflow_readiness = {
        "robotics/ready": True,
        "robotics/setup-needed": False,
    }
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]

    await handler.process(
        "installation",
        "delivery-install",
        {
            "action": "created",
            "installation": {"id": 42},
            "repositories": [
                repository(100, "robotics/ready"),
                repository(101, "robotics/setup-needed"),
            ],
        },
    )

    ready = await store.get_repository(100)
    missing = await store.get_repository(101)
    assert ready is not None and ready.status == "ready"
    assert missing is not None and missing.status == "workflow_missing"
    assert not hasattr(ready, "token")
    assert not hasattr(ready, "source")


@pytest.mark.anyio
async def test_two_repositories_can_hold_active_runs_at_the_same_time() -> None:
    client = MultiRepositoryGitHub()
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]

    await handler.process(
        "pull_request",
        "delivery-one",
        pull_request_payload(100, "robotics/navigation", 7),
    )
    await handler.process(
        "pull_request",
        "delivery-two",
        pull_request_payload(101, "robotics/manipulation", 9),
    )

    first = await store.get_active_run(100, 7)
    second = await store.get_active_run(101, 9)
    assert first is not None and first.repository == "robotics/navigation"
    assert second is not None and second.repository == "robotics/manipulation"
    assert first.workflow_run_id != second.workflow_run_id


@pytest.mark.anyio
async def test_repository_removal_clears_only_its_active_runs() -> None:
    client = MultiRepositoryGitHub()
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]
    await handler.process(
        "pull_request",
        "delivery-one",
        pull_request_payload(100, "robotics/navigation", 7),
    )
    await handler.process(
        "pull_request",
        "delivery-two",
        pull_request_payload(101, "robotics/manipulation", 9),
    )

    await handler.process(
        "installation_repositories",
        "delivery-remove",
        {
            "action": "removed",
            "installation": {"id": 42},
            "repositories_added": [],
            "repositories_removed": [
                repository(100, "robotics/navigation")
            ],
        },
    )

    assert await store.get_active_run(100, 7) is None
    assert await store.get_active_run(101, 9) is not None
    removed = await store.get_repository(100)
    assert removed is not None and removed.status == "removed"
    assert removed.expires_at is not None


@pytest.mark.anyio
async def test_reconciler_recovers_lost_completion_and_stale_delivery() -> None:
    settings = replace(
        AppSettings("1", "key"),
        delivery_stale_seconds=60,
        run_stale_seconds=60,
        max_run_seconds=3600,
    )
    client = MultiRepositoryGitHub(settings)
    store = MemoryEventStore()
    metrics = RecordingMetrics()
    handler = EventHandler(client, store, metrics)  # type: ignore[arg-type]
    await store.claim_delivery("stuck-delivery", event="pull_request")
    record = RunRecord.create(
        workflow_run_id=300,
        check_run_id=200,
        installation_id=42,
        repository_id=100,
        repository="robotics/navigation",
        pull_request_number=7,
        head_sha="head-robotics/navigation",
        base_sha="base-robotics/navigation",
        change="Update robotics/navigation",
        head_repository="robotics/navigation",
        details_url="https://github.test/runs/300",
    )
    await store.save_run(record)
    client.workflow_states[300] = WorkflowRun(
        300,
        "https://github.test/runs/300",
        status="completed",
        conclusion="success",
        path=".github/workflows/fraeno-validation.yml",
    )
    client.reports[300] = {
        "outcome": "pass",
        "baseline": {"succeeded": True, "error": None},
        "candidate": {"succeeded": True, "error": None},
        "comparison": {
            "outcome": "pass",
            "validation_level": "L2",
            "findings": [],
        },
    }
    reconciler = Reconciler(
        client,  # type: ignore[arg-type]
        store,
        handler,
        settings,
        metrics,
    )

    result = await reconciler.reconcile(
        now=datetime.now(timezone.utc) + timedelta(minutes=2)
    )

    delivery = await store.get_delivery("stuck-delivery")
    assert delivery is not None and delivery.status == "dead_letter"
    assert await store.get_active_run(100, 7) is None
    assert client.updates[-1]["conclusion"] == "success"
    assert result.deliveries_dead_lettered == 1
    assert result.checks_completed == 1
    assert ("stale_check", 1.0) in metrics.values
    assert any(name == "run_duration_seconds" for name, _ in metrics.values)


@pytest.mark.anyio
async def test_reconciler_backfills_existing_github_installations() -> None:
    settings = AppSettings("1", "key")
    client = MultiRepositoryGitHub(settings)
    client.installations = [
        {
            "id": 42,
            "created_at": "2026-08-01T12:00:00Z",
            "account": {
                "id": 224500479,
                "login": "DeepUbuntu",
                "type": "Organization",
            },
        },
        {
            "id": 43,
            "created_at": "invalid",
            "suspended_at": "2026-08-16T12:00:00Z",
            "account": {
                "id": 99,
                "login": "Customer-Robotics",
                "type": "Organization",
            },
        },
        {"id": "bad", "account": {}},
    ]
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]
    reconciler = Reconciler(client, store, handler, settings)  # type: ignore[arg-type]
    observed_at = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)

    result = await reconciler.reconcile(now=observed_at)

    deepubuntu = await store.get_installation(42)
    suspended = await store.get_installation(43)
    assert result.installations_seen == 3
    assert result.installations_synced == 2
    assert deepubuntu is not None
    assert deepubuntu.account_login == "deepubuntu"
    assert deepubuntu.installed_at == datetime(
        2026, 8, 1, 12, tzinfo=timezone.utc
    )
    assert suspended is not None and suspended.status == "suspended"


@pytest.mark.anyio
async def test_reconciler_fails_a_workflow_that_exceeds_time_limit() -> None:
    settings = replace(
        AppSettings("1", "key"),
        run_stale_seconds=60,
        max_run_seconds=90,
    )
    client = MultiRepositoryGitHub(settings)
    store = MemoryEventStore()
    handler = EventHandler(client, store)  # type: ignore[arg-type]
    record = RunRecord.create(
        workflow_run_id=300,
        check_run_id=200,
        installation_id=42,
        repository_id=100,
        repository="robotics/navigation",
        pull_request_number=7,
        head_sha="head-robotics/navigation",
        base_sha="base-robotics/navigation",
        change="Update robotics/navigation",
        head_repository="robotics/navigation",
        details_url="https://github.test/runs/300",
    )
    await store.save_run(record)
    client.workflow_states[300] = WorkflowRun(
        300,
        record.details_url,
        status="in_progress",
    )
    reconciler = Reconciler(
        client,  # type: ignore[arg-type]
        store,
        handler,
        settings,
    )

    result = await reconciler.reconcile(
        now=datetime.now(timezone.utc) + timedelta(minutes=2)
    )

    assert result.checks_failed == 1
    assert client.cancellations == [300]
    assert client.updates[-1]["conclusion"] == "failure"
    assert await store.get_active_run(100, 7) is None


class ReplayGitHub:
    def __init__(self, guid: str) -> None:
        self.guid = guid
        self.redeliveries: list[int] = []

    async def app_delivery(self, github_delivery_id: int) -> dict[str, Any]:
        return {"id": github_delivery_id, "guid": self.guid}

    async def redeliver_app_delivery(self, github_delivery_id: int) -> None:
        self.redeliveries.append(github_delivery_id)


@pytest.mark.anyio
async def test_operator_replay_requires_exact_identity_and_is_reclaimable() -> None:
    guid = "91c2d46f-17b2-4d9d-aa0d-100e079c0c20"
    store = MemoryEventStore()
    await store.claim_delivery(guid, event="pull_request")
    await store.dead_letter_delivery(guid, error_kind="processing_timeout")
    client = ReplayGitHub(guid)
    arguments = argparse.Namespace(
        delivery_guid=guid,
        github_delivery_id=812,
        reason="Recover a delivery after the worker outage",
        actor="operator@example.com",
        confirm=guid,
        execute=True,
    )

    result = await replay_delivery(
        arguments,
        settings=AppSettings("1", "key"),
        client=client,  # type: ignore[arg-type]
        store=store,
    )

    assert result == 0
    assert client.redeliveries == [812]
    requested = await store.get_delivery(guid)
    assert requested is not None and requested.status == "replay_requested"
    assert await store.claim_delivery(guid, event="pull_request") is True


class StaticReconciler:
    async def reconcile(self) -> ReconciliationResult:
        return ReconciliationResult(checks_seen=2, checks_completed=1)


def test_reconcile_endpoint_requires_cloud_scheduler_identity() -> None:
    app = create_worker_app(
        AppSettings("1", "key"),
        object(),  # type: ignore[arg-type]
        reconciler=StaticReconciler(),  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        rejected = client.post("/internal/reconcile")
        accepted = client.post(
            "/internal/reconcile",
            headers={"x-cloudscheduler": "true"},
        )

    assert rejected.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["checks_seen"] == 2
    assert accepted.json()["checks_completed"] == 1


class AlwaysFailHandler:
    def __init__(self) -> None:
        self.store = MemoryEventStore()

    async def process(
        self, event: str, delivery_id: str, payload: dict[str, Any]
    ) -> None:
        del event, delivery_id, payload
        raise RuntimeError("worker unavailable")


@pytest.mark.anyio
async def test_final_cloud_tasks_attempt_moves_delivery_to_dead_letter() -> None:
    handler = AlwaysFailHandler()
    metrics = RecordingMetrics()
    settings = replace(AppSettings("1", "key"), max_delivery_attempts=5)
    app = create_worker_app(
        settings,
        handler,  # type: ignore[arg-type]
        metrics=metrics,
    )

    with TestClient(app) as client:
        response = client.post(
            "/internal/github-events",
            json={
                "event": "pull_request",
                "delivery_id": "delivery-final",
                "enqueued_at": (
                    datetime.now(timezone.utc) - timedelta(seconds=12)
                ).isoformat(),
                "payload": {"action": "opened"},
            },
            headers={
                "x-cloudtasks-taskname": "task-final",
                "x-cloudtasks-taskretrycount": "4",
            },
        )

    assert response.status_code == 204
    delivery = await handler.store.get_delivery("delivery-final")
    assert delivery is not None and delivery.status == "dead_letter"
    assert ("delivery_retry", 4.0) in metrics.values
    assert any(name == "queue_delay_seconds" for name, _ in metrics.values)


class RecordingEnqueuer:
    async def enqueue(
        self, event: str, delivery_id: str, payload: dict[str, Any]
    ) -> bool:
        del event, delivery_id, payload
        return True


def test_signature_rejection_emits_operational_metric() -> None:
    settings = WebhookSettings(
        webhook_secret="secret",
        gcp_project="project",
        gcp_location="us-central1",
        queue_name="queue",
        worker_url="https://worker.example",
        task_service_account="tasks@project.iam.gserviceaccount.com",
    )
    metrics = RecordingMetrics()
    app = create_webhook_app(settings, RecordingEnqueuer(), metrics)
    body = json.dumps({"zen": "hello"}).encode()
    wrong_signature = hmac.new(b"wrong", body, hashlib.sha256).hexdigest()

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "x-github-event": "ping",
                "x-github-delivery": "delivery",
                "x-hub-signature-256": f"sha256={wrong_signature}",
            },
        )

    assert response.status_code == 401
    assert metrics.values == [("signature_rejection", 1.0)]
