from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from fraeno.github_app.client import GitHubApiError, GitHubClient
from fraeno.github_app.handler import EventHandler
from fraeno.github_app.metrics import MetricSink, NullMetricSink
from fraeno.github_app.settings import AppSettings
from fraeno.github_app.store import EventStore


@dataclass(frozen=True)
class ReconciliationResult:
    deliveries_dead_lettered: int = 0
    checks_seen: int = 0
    checks_completed: int = 0
    checks_failed: int = 0
    checks_pending: int = 0
    github_errors: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class Reconciler:
    def __init__(
        self,
        client: GitHubClient,
        store: EventStore,
        handler: EventHandler,
        settings: AppSettings,
        metrics: MetricSink | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.handler = handler
        self.settings = settings
        self.metrics = metrics or NullMetricSink()

    async def reconcile(
        self, *, now: datetime | None = None
    ) -> ReconciliationResult:
        active_now = now or datetime.now(timezone.utc)
        stale_deliveries = await self.store.list_stale_deliveries(
            active_now
            - timedelta(seconds=self.settings.delivery_stale_seconds)
        )
        for delivery in stale_deliveries:
            await self.store.dead_letter_delivery(
                delivery.delivery_id,
                error_kind="processing_timeout",
            )

        stale_runs = await self.store.list_stale_runs(
            active_now - timedelta(seconds=self.settings.run_stale_seconds)
        )
        completed = 0
        failed = 0
        pending = 0
        github_errors = 0
        for record in stale_runs:
            self.metrics.emit("stale_check")
            try:
                token = await self.client.installation_token(
                    record.installation_id, record.repository_id
                )
                workflow = await self.client.workflow_run(
                    record.repository, record.workflow_run_id, token
                )
                age_seconds = (
                    active_now - datetime.fromisoformat(record.created_at)
                ).total_seconds()
                if workflow.status == "completed":
                    await self.handler.complete_workflow(record, workflow)
                    completed += 1
                elif age_seconds >= self.settings.max_run_seconds:
                    await self.handler.fail_stale_run(
                        record,
                        reason=(
                            "The GitHub workflow did not finish within Fraeno's "
                            "configured time limit. You can rerun the check after "
                            "confirming the repository workflow is healthy."
                        ),
                    )
                    failed += 1
                else:
                    pending += 1
            except GitHubApiError as error:
                github_errors += 1
                if error.retryable:
                    pending += 1
                    continue
                await self.handler.fail_stale_run(
                    record,
                    reason=(
                        "GitHub no longer has a recoverable record of this workflow. "
                        "Rerun the Fraeno check to start a fresh validation."
                    ),
                )
                failed += 1

        return ReconciliationResult(
            deliveries_dead_lettered=len(stale_deliveries),
            checks_seen=len(stale_runs),
            checks_completed=completed,
            checks_failed=failed,
            checks_pending=pending,
            github_errors=github_errors,
        )
