from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone

from fraeno.github_app.client import GitHubApiError, GitHubClient
from fraeno.github_app.handler import EventHandler
from fraeno.github_app.metrics import MetricSink, NullMetricSink
from fraeno.github_app.settings import AppSettings
from fraeno.github_app.store import EventStore, InstallationRecord


@dataclass(frozen=True)
class ReconciliationResult:
    installations_seen: int = 0
    installations_synced: int = 0
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

    async def reconcile(self, *, now: datetime | None = None) -> ReconciliationResult:
        active_now = now or datetime.now(timezone.utc)
        installations_seen = 0
        installations_synced = 0
        github_errors = 0
        try:
            installations = await self.client.app_installations()
            installations_seen = len(installations)
            for installation in installations:
                if await self._sync_installation(installation, active_now):
                    installations_synced += 1
        except GitHubApiError:
            github_errors += 1

        stale_deliveries = await self.store.list_stale_deliveries(
            active_now - timedelta(seconds=self.settings.delivery_stale_seconds)
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
            installations_seen=installations_seen,
            installations_synced=installations_synced,
            deliveries_dead_lettered=len(stale_deliveries),
            checks_seen=len(stale_runs),
            checks_completed=completed,
            checks_failed=failed,
            checks_pending=pending,
            github_errors=github_errors,
        )

    async def _sync_installation(
        self,
        raw_installation: dict[str, object],
        observed_at: datetime,
    ) -> bool:
        try:
            raw_installation_id = raw_installation["id"]
            if not isinstance(raw_installation_id, (int, str)) or isinstance(
                raw_installation_id, bool
            ):
                return False
            installation_id = int(raw_installation_id)
            raw_account = raw_installation["account"]
            if not isinstance(raw_account, dict):
                return False
            account_id = int(raw_account["id"])
            account_login = str(raw_account["login"]).strip().lower()
            account_type = str(raw_account.get("type") or "Unknown")
        except (KeyError, TypeError, ValueError):
            return False
        if not account_login:
            return False

        current = await self.store.get_installation(installation_id)
        status = "suspended" if raw_installation.get("suspended_at") is not None else "installed"
        if current is None:
            record = InstallationRecord.create(
                installation_id=installation_id,
                account_id=account_id,
                account_login=account_login,
                account_type=account_type,
                status=status,
            )
            created_at = self._github_timestamp(raw_installation.get("created_at"))
            if created_at is not None:
                record = replace(
                    record,
                    installed_at=created_at,
                    updated_at=observed_at,
                )
        else:
            record = replace(
                current,
                account_id=account_id,
                account_login=account_login,
                account_type=account_type,
                status=status,
                updated_at=observed_at,
            )
        await self.store.upsert_installation(record)
        return True

    @staticmethod
    def _github_timestamp(raw_value: object) -> datetime | None:
        if not isinstance(raw_value, str) or not raw_value:
            return None
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (
            parsed.replace(tzinfo=timezone.utc)
            if parsed.tzinfo is None
            else parsed.astimezone(timezone.utc)
        )
