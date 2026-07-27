from __future__ import annotations

import logging
from typing import Any

from fraeno.github_app.client import GitHubApiError, GitHubClient
from fraeno.github_app.store import EventStore, RunRecord

LOGGER = logging.getLogger(__name__)

PULL_REQUEST_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}
CONCLUSIONS = {
    "success": "success",
    "failure": "failure",
    "cancelled": "cancelled",
    "timed_out": "timed_out",
    "skipped": "skipped",
    "neutral": "neutral",
    "action_required": "action_required",
    "stale": "failure",
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
        if action not in PULL_REQUEST_ACTIONS:
            return
        pull_request = payload["pull_request"]
        if pull_request.get("draft") and action != "ready_for_review":
            return
        await self._dispatch(
            delivery_id=delivery_id,
            payload=payload,
            pull_request_number=int(pull_request["number"]),
            head_sha=str(pull_request["head"]["sha"]),
            base_sha=str(pull_request["base"]["sha"]),
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
            head_sha=str(check_run["head_sha"]),
            base_sha=str(pull_request["base"]["sha"]),
        )

    async def _dispatch(
        self,
        *,
        delivery_id: str,
        payload: dict[str, Any],
        pull_request_number: int,
        head_sha: str,
        base_sha: str,
    ) -> None:
        installation_id = int(payload["installation"]["id"])
        repository = payload["repository"]
        repository_id = int(repository["id"])
        full_name = str(repository["full_name"])
        default_branch = str(repository["default_branch"])
        token = await self.client.installation_token(installation_id, repository_id)
        external_id = f"fraeno:{delivery_id}"
        check = await self.client.find_check_run(
            full_name, head_sha, token, external_id
        )
        if check is None:
            check = await self.client.create_check_run(
                full_name, head_sha, token, external_id
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
                    base_sha=base_sha,
                    head_sha=head_sha,
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
                head_sha=head_sha,
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
                "Fraeno is running the base and candidate commits in matching "
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
        token = await self.client.installation_token(
            record.installation_id, record.repository_id
        )
        raw_conclusion = str(workflow_run.get("conclusion") or "failure")
        conclusion = CONCLUSIONS.get(raw_conclusion, "failure")
        succeeded = conclusion == "success"
        await self.client.update_check_run(
            record.repository,
            record.check_run_id,
            token,
            status="completed",
            conclusion=conclusion,
            details_url=str(workflow_run.get("html_url") or record.details_url),
            title=(
                "Robot integration validation passed"
                if succeeded
                else "Robot integration validation blocked this change"
            ),
            summary=(
                "The configured robot system remained healthy after the dependency change."
                if succeeded
                else "The workflow found a regression or incomplete evidence. "
                "Open the workflow for the complete Fraeno report."
            ),
        )
