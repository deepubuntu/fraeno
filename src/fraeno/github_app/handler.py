from __future__ import annotations

import logging
from typing import Any

from fraeno.github_app.client import GitHubApiError, GitHubClient
from fraeno.github_app.presentation import present_validation
from fraeno.github_app.store import EventStore, RunRecord

LOGGER = logging.getLogger(__name__)

START_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}
STOP_ACTIONS = {
    "closed": "Validation stopped because this pull request was closed",
    "converted_to_draft": "Validation paused while this pull request is a draft",
}


class EventHandler:
    def __init__(self, client: GitHubClient, store: EventStore) -> None:
        self.client = client
        self.store = store

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
        except GitHubApiError as error:
            LOGGER.exception(
                "GitHub event processing failed",
                extra={"event": event, "delivery_id": delivery_id},
            )
            if error.retryable:
                await self.store.fail_delivery(delivery_id)
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
        except GitHubApiError:
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
        workflow_run = payload["workflow_run"]
        raw_path = str(workflow_run.get("path") or "").split("@", maxsplit=1)[0]
        if not raw_path.endswith(f"/{self.client.settings.workflow_file}"):
            return
        record = await self.store.get_run(int(workflow_run["id"]))
        if record is None:
            raise GitHubApiError(
                "Fraeno workflow completed before correlation was available",
                retryable=True,
            )
        if int(payload["repository"]["id"]) != record.repository_id:
            raise ValueError("workflow repository does not match stored run")
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

        raw_conclusion = str(workflow_run.get("conclusion") or "failure")
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
            workflow_conclusion=raw_conclusion,
        )
        await self.client.update_check_run(
            record.repository,
            record.check_run_id,
            token,
            status="completed",
            conclusion=presentation.conclusion,
            details_url=str(workflow_run.get("html_url") or record.details_url),
            title=presentation.title,
            summary=presentation.summary,
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
