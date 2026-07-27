from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fraeno.github_app.client import GitHubApiError, GitHubClient, WorkflowRun
from fraeno.github_app.metrics import MetricSink, NullMetricSink
from fraeno.github_app.presentation import present_validation
from fraeno.github_app.store import EventStore, RepositoryRecord, RunRecord

LOGGER = logging.getLogger(__name__)

START_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}
STOP_ACTIONS = {
    "closed": "Validation stopped because this pull request was closed",
    "converted_to_draft": "Validation paused while this pull request is a draft",
}
ACTIVE_INSTALLATION_ACTIONS = {"created", "new_permissions_accepted", "unsuspend"}
INACTIVE_INSTALLATION_ACTIONS = {"deleted", "suspend"}


class EventHandler:
    def __init__(
        self,
        client: GitHubClient,
        store: EventStore,
        metrics: MetricSink | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.metrics = metrics or NullMetricSink()

    async def process(
        self,
        event: str,
        delivery_id: str,
        payload: dict[str, Any],
    ) -> None:
        try:
            if event == "pull_request":
                await self._pull_request(delivery_id, payload)
            elif event == "workflow_run":
                await self._workflow_run(payload)
            elif event == "check_run":
                await self._check_run(delivery_id, payload)
            elif event == "installation":
                await self._installation(payload)
            elif event == "installation_repositories":
                await self._installation_repositories(payload)
        except GitHubApiError as error:
            if event in {"pull_request", "check_run"}:
                self.metrics.emit("dispatch_failure")
            LOGGER.exception(
                "GitHub event processing failed",
                extra={"event": event, "delivery_id": delivery_id},
            )
            if error.retryable:
                await self.store.fail_delivery(
                    delivery_id, error_kind="github_retryable"
                )
                raise
            await self.store.complete_delivery(delivery_id)
        except (KeyError, TypeError, ValueError):
            LOGGER.exception(
                "GitHub event payload was invalid",
                extra={"event": event, "delivery_id": delivery_id},
            )
            await self.store.complete_delivery(delivery_id)
        else:
            await self.store.complete_delivery(delivery_id)

    async def _pull_request(
        self, delivery_id: str, payload: dict[str, Any]
    ) -> None:
        action = payload.get("action")
        if action in STOP_ACTIONS:
            await self._stop_pull_request(payload, STOP_ACTIONS[str(action)])
            return
        if action not in START_ACTIONS:
            return
        pull_request = payload["pull_request"]
        if pull_request.get("draft") and action != "ready_for_review":
            return
        await self._dispatch(
            delivery_id=delivery_id,
            payload=payload,
            pull_request_number=int(pull_request["number"]),
        )

    async def _check_run(
        self, delivery_id: str, payload: dict[str, Any]
    ) -> None:
        if payload.get("action") != "rerequested":
            return
        check_run = payload["check_run"]
        pull_requests = check_run.get("pull_requests", [])
        if not pull_requests:
            return
        pull_request = pull_requests[0]
        await self._dispatch(
            delivery_id=delivery_id,
            payload=payload,
            pull_request_number=int(pull_request["number"]),
        )

    async def _installation(self, payload: dict[str, Any]) -> None:
        action = str(payload["action"])
        installation_id = int(payload["installation"]["id"])
        if action in INACTIVE_INSTALLATION_ACTIONS:
            status = "removed" if action == "deleted" else "suspended"
            reason = (
                "The Fraeno installation was removed"
                if action == "deleted"
                else "The Fraeno installation was suspended"
            )
            known_repositories = {
                record.repository_id: record
                for record in await self.store.list_repositories(installation_id)
            }
            for repository_object in self._repository_objects(
                payload.get("repositories", [])
            ):
                repository_id = int(repository_object["id"])
                known_repositories.setdefault(
                    repository_id,
                    self._repository_record(
                        installation_id,
                        repository_object,
                        status=status,
                        reason=reason,
                    ),
                )
            for installed_repository in known_repositories.values():
                await self._deactivate_repository(
                    installed_repository, status=status, reason=reason
                )
            return
        if action not in ACTIVE_INSTALLATION_ACTIONS:
            return
        raw_repositories = payload.get("repositories")
        repository_objects = (
            self._repository_objects(raw_repositories)
            if isinstance(raw_repositories, list)
            else []
        )
        if not repository_objects:
            repository_objects = [
                {
                    "id": record.repository_id,
                    "full_name": record.full_name,
                    "default_branch": record.default_branch,
                }
                for record in await self.store.list_repositories(installation_id)
            ]
        for repository_object in repository_objects:
            await self._probe_repository(installation_id, repository_object)

    async def _installation_repositories(
        self, payload: dict[str, Any]
    ) -> None:
        installation_id = int(payload["installation"]["id"])
        for repository in self._repository_objects(
            payload.get("repositories_removed", [])
        ):
            existing = await self.store.get_repository(int(repository["id"]))
            if existing is None:
                existing = self._repository_record(
                    installation_id,
                    repository,
                    status="removed",
                    reason="Fraeno no longer has access to this repository",
                )
            await self._deactivate_repository(
                existing,
                status="removed",
                reason="Fraeno no longer has access to this repository",
            )
        for repository in self._repository_objects(
            payload.get("repositories_added", [])
        ):
            await self._probe_repository(installation_id, repository)

    async def _probe_repository(
        self, installation_id: int, repository: dict[str, Any]
    ) -> None:
        pending = self._repository_record(
            installation_id,
            repository,
            status="pending",
            reason="Checking for the trusted Fraeno workflow",
        )
        await self.store.upsert_repository(pending)
        try:
            token = await self.client.installation_token(
                installation_id, pending.repository_id
            )
            available = await self.client.workflow_available(
                pending.full_name, token
            )
        except GitHubApiError as error:
            await self.store.upsert_repository(
                self._repository_record(
                    installation_id,
                    repository,
                    status="unavailable",
                    reason="GitHub could not verify this repository",
                )
            )
            if error.retryable:
                raise
            return
        await self.store.upsert_repository(
            self._repository_record(
                installation_id,
                repository,
                status="ready" if available else "workflow_missing",
                reason=(
                    "The trusted Fraeno workflow is available"
                    if available
                    else (
                        "Add .github/workflows/"
                        f"{self.client.settings.workflow_file} to the default branch"
                    )
                ),
            )
        )

    async def _deactivate_repository(
        self,
        repository: RepositoryRecord,
        *,
        status: str,
        reason: str,
    ) -> None:
        await self.store.upsert_repository(
            RepositoryRecord.create(
                installation_id=repository.installation_id,
                repository_id=repository.repository_id,
                full_name=repository.full_name,
                default_branch=repository.default_branch,
                status=status,
                reason=reason,
                retention_days=self.client.settings.repository_retention_days,
            )
        )
        for active in await self.store.list_active_runs(repository.repository_id):
            await self.store.clear_active_run(active)

    async def _dispatch(
        self,
        *,
        delivery_id: str,
        payload: dict[str, Any],
        pull_request_number: int,
    ) -> None:
        installation_id = int(payload["installation"]["id"])
        repository = payload["repository"]
        repository_id = int(repository["id"])
        full_name = str(repository["full_name"])
        default_branch = str(repository["default_branch"])
        await self.store.upsert_repository(
            self._repository_record(
                installation_id,
                repository,
                status="pending",
                reason="Starting Fraeno validation",
            )
        )
        token = await self.client.installation_token(installation_id, repository_id)
        pull_request = await self.client.pull_request(
            full_name, pull_request_number, token
        )
        active = await self.store.get_active_run(
            repository_id, pull_request_number
        )
        if pull_request.state != "open" or pull_request.draft:
            if active is not None:
                await self._neutralize(
                    active,
                    token,
                    title="This pull request is not ready for validation",
                    summary=(
                        "Fraeno stopped this attempt because the pull request "
                        "is closed or is currently a draft."
                    ),
                )
            return

        if active is not None:
            await self._neutralize(
                active,
                token,
                title="Superseded by a newer Fraeno attempt",
                summary=(
                    "A newer commit or requested rerun replaced this validation. "
                    "This result no longer represents the pull request."
                ),
            )

        external_id = f"fraeno:{delivery_id}"
        check = await self.client.find_check_run(
            full_name, pull_request.head_sha, token, external_id
        )
        if check is None:
            check = await self.client.create_check_run(
                full_name, pull_request.head_sha, token, external_id
            )
        try:
            workflow = await self.client.find_workflow_run(
                full_name, token, delivery_id
            )
            if workflow is None:
                workflow = await self.client.dispatch_workflow(
                    full_name,
                    default_branch,
                    token,
                    base_sha=pull_request.base_sha,
                    head_sha=pull_request.head_sha,
                    base_repository=pull_request.base_repository,
                    head_repository=pull_request.head_repository,
                    pull_request_number=pull_request_number,
                    check_run_id=check.id,
                    external_id=external_id,
                )
        except GitHubApiError as error:
            await self.store.upsert_repository(
                self._repository_record(
                    installation_id,
                    repository,
                    status=(
                        "workflow_missing"
                        if error.status_code == 404
                        else "unavailable"
                    ),
                    reason=(
                        "The trusted Fraeno workflow is missing"
                        if error.status_code == 404
                        else "GitHub could not start Fraeno validation"
                    ),
                )
            )
            await self.client.update_check_run(
                full_name,
                check.id,
                token,
                status="completed",
                conclusion="failure",
                details_url=check.html_url,
                title="Fraeno could not start",
                summary=(
                    "The trusted Fraeno workflow could not be dispatched. "
                    "Confirm that `.github/workflows/"
                    f"{self.client.settings.workflow_file}` exists on the default branch."
                ),
            )
            raise

        await self.store.upsert_repository(
            self._repository_record(
                installation_id,
                repository,
                status="ready",
                reason="The trusted Fraeno workflow is available",
            )
        )
        await self.store.save_run(
            RunRecord.create(
                workflow_run_id=workflow.id,
                check_run_id=check.id,
                installation_id=installation_id,
                repository_id=repository_id,
                repository=full_name,
                pull_request_number=pull_request_number,
                head_sha=pull_request.head_sha,
                base_sha=pull_request.base_sha,
                change=pull_request.title,
                head_repository=pull_request.head_repository,
                details_url=workflow.html_url,
                retention_days=self.client.settings.run_retention_days,
            )
        )
        await self.client.update_check_run(
            full_name,
            check.id,
            token,
            status="in_progress",
            details_url=workflow.html_url,
            title="Testing the complete robot system",
            summary=(
                f"**Change** {pull_request.title}\n\n"
                "**Validation** Running\n\n"
                "Fraeno is testing the trusted base and candidate in matching "
                "environments."
            ),
        )

    async def _workflow_run(self, payload: dict[str, Any]) -> None:
        if payload.get("action") != "completed":
            return
        raw_run = payload["workflow_run"]
        raw_path = str(raw_run.get("path") or "").split("@", maxsplit=1)[0]
        if not raw_path.endswith(f"/{self.client.settings.workflow_file}"):
            return
        record = await self.store.get_run(int(raw_run["id"]))
        if record is None:
            raise GitHubApiError(
                "Fraeno workflow completed before correlation was available",
                retryable=True,
            )
        if int(payload["repository"]["id"]) != record.repository_id:
            raise ValueError("workflow repository does not match stored run")
        await self.complete_workflow(
            record,
            WorkflowRun(
                id=int(raw_run["id"]),
                html_url=str(raw_run.get("html_url") or record.details_url),
                status="completed",
                conclusion=str(raw_run.get("conclusion") or "failure"),
                path=raw_path,
            ),
        )

    async def complete_workflow(
        self, record: RunRecord, workflow_run: WorkflowRun
    ) -> None:
        active = await self.store.get_active_run(
            record.repository_id, record.pull_request_number
        )
        if active is None or active.workflow_run_id != record.workflow_run_id:
            LOGGER.info(
                "Ignoring superseded Fraeno workflow completion",
                extra={"workflow_run_id": record.workflow_run_id},
            )
            return
        token = await self.client.installation_token(
            record.installation_id, record.repository_id
        )
        pull_request = await self.client.pull_request(
            record.repository, record.pull_request_number, token
        )
        if (
            pull_request.head_sha != record.head_sha
            or pull_request.state != "open"
            or pull_request.draft
        ):
            await self._neutralize(
                record,
                token,
                title="This Fraeno result is no longer current",
                summary=(
                    "The pull request changed state or moved to a newer commit "
                    "before this validation completed."
                ),
            )
            return

        try:
            report = await self.client.validation_report(
                record.repository,
                record.workflow_run_id,
                record.check_run_id,
                token,
            )
        except GitHubApiError as error:
            if error.retryable:
                raise
            LOGGER.warning(
                "Fraeno validation report could not be read: %s",
                error,
                extra={"workflow_run_id": record.workflow_run_id},
            )
            report = None
        presentation = present_validation(
            report,
            change=record.change,
            workflow_conclusion=workflow_run.conclusion or "failure",
        )
        await self.client.update_check_run(
            record.repository,
            record.check_run_id,
            token,
            status="completed",
            conclusion=presentation.conclusion,
            details_url=workflow_run.html_url or record.details_url,
            title=presentation.title,
            summary=presentation.summary,
        )
        created_at = datetime.fromisoformat(record.created_at)
        duration = max(
            0.0,
            (datetime.now(timezone.utc) - created_at).total_seconds(),
        )
        self.metrics.emit("run_duration_seconds", duration)
        await self.store.clear_active_run(record)

    async def fail_stale_run(self, record: RunRecord, *, reason: str) -> None:
        active = await self.store.get_active_run(
            record.repository_id, record.pull_request_number
        )
        if active is None or active.workflow_run_id != record.workflow_run_id:
            return
        token = await self.client.installation_token(
            record.installation_id, record.repository_id
        )
        await self.client.cancel_workflow_run(
            record.repository, record.workflow_run_id, token
        )
        await self.client.update_check_run(
            record.repository,
            record.check_run_id,
            token,
            status="completed",
            conclusion="failure",
            details_url=record.details_url,
            title="Fraeno validation did not finish",
            summary=reason,
        )
        await self.store.clear_active_run(record)

    async def _stop_pull_request(
        self, payload: dict[str, Any], reason: str
    ) -> None:
        installation_id = int(payload["installation"]["id"])
        repository = payload["repository"]
        repository_id = int(repository["id"])
        pull_request_number = int(payload["pull_request"]["number"])
        active = await self.store.get_active_run(
            repository_id, pull_request_number
        )
        if active is None:
            return
        token = await self.client.installation_token(
            installation_id, repository_id
        )
        await self._neutralize(
            active,
            token,
            title=reason,
            summary=(
                f"{reason}. This check is neutral because it no longer represents "
                "a mergeable pull request."
            ),
        )

    async def _neutralize(
        self,
        record: RunRecord,
        token: str,
        *,
        title: str,
        summary: str,
    ) -> None:
        await self.client.cancel_workflow_run(
            record.repository, record.workflow_run_id, token
        )
        await self.client.update_check_run(
            record.repository,
            record.check_run_id,
            token,
            status="completed",
            conclusion="neutral",
            details_url=record.details_url,
            title=title,
            summary=summary,
        )
        await self.store.clear_active_run(record)

    def _repository_record(
        self,
        installation_id: int,
        repository: dict[str, Any],
        *,
        status: str,
        reason: str,
    ) -> RepositoryRecord:
        return RepositoryRecord.create(
            installation_id=installation_id,
            repository_id=int(repository["id"]),
            full_name=str(repository["full_name"]),
            default_branch=str(repository.get("default_branch") or ""),
            status=status,
            reason=reason,
            retention_days=self.client.settings.repository_retention_days,
        )

    @staticmethod
    def _repository_objects(raw_value: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_value, list):
            return []
        return [value for value in raw_value if isinstance(value, dict)]
