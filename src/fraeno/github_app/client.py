from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from dataclasses import dataclass
from typing import Any

import httpx

from fraeno.github_app.auth import create_app_jwt
from fraeno.github_app.settings import AppSettings

LOGGER = logging.getLogger(__name__)


class GitHubApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class CheckRun:
    id: int
    html_url: str


@dataclass(frozen=True)
class WorkflowRun:
    id: int
    html_url: str
    status: str = ""
    conclusion: str = ""
    path: str = ""


@dataclass(frozen=True)
class PullRequestState:
    number: int
    title: str
    state: str
    draft: bool
    head_sha: str
    base_sha: str
    head_repository: str
    base_repository: str


MAX_REPORT_ARCHIVE_BYTES = 10_000_000
MAX_REPORT_BYTES = 2_000_000


class GitHubClient:
    dispatch_poll_delays: tuple[float, ...] = (0.0, 1.0, 2.0, 4.0, 8.0)

    def __init__(
        self,
        settings: AppSettings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._client = http_client or httpx.AsyncClient(
            base_url=settings.github_api_url,
            timeout=20,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def installation_token(
        self, installation_id: int, repository_id: int
    ) -> str:
        response = await self._app_request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            json={
                "repository_ids": [repository_id],
                "permissions": {
                    "actions": "write",
                    "checks": "write",
                    "contents": "read",
                    "pull_requests": "read",
                },
            },
        )
        token = response.get("token")
        if not isinstance(token, str) or not token:
            raise GitHubApiError("GitHub did not return an installation token")
        return token

    async def app_installations(self) -> list[dict[str, Any]]:
        installations: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await self._app_list_request(
                "GET",
                "/app/installations",
                params={"per_page": "100", "page": str(page)},
            )
            installations.extend(
                value for value in response if isinstance(value, dict)
            )
            if len(response) < 100:
                return installations
            page += 1

    async def create_check_run(
        self,
        repository: str,
        head_sha: str,
        installation_token: str,
        external_id: str,
    ) -> CheckRun:
        response = await self._request(
            "POST",
            f"/repos/{repository}/check-runs",
            token=installation_token,
            json={
                "name": self.settings.check_name,
                "head_sha": head_sha,
                "status": "queued",
                "external_id": external_id,
                "output": {
                    "title": "Waiting for robot integration validation",
                    "summary": "Fraeno is preparing matching baseline and candidate environments.",
                },
            },
        )
        return CheckRun(id=int(response["id"]), html_url=str(response["html_url"]))

    async def find_check_run(
        self,
        repository: str,
        head_sha: str,
        installation_token: str,
        external_id: str,
    ) -> CheckRun | None:
        response = await self._request(
            "GET",
            f"/repos/{repository}/commits/{head_sha}/check-runs",
            token=installation_token,
            params={
                "check_name": self.settings.check_name,
                "filter": "all",
                "per_page": "100",
            },
        )
        raw_runs = response.get("check_runs", [])
        if not isinstance(raw_runs, list):
            raise GitHubApiError("GitHub returned malformed check runs")
        for raw_run in raw_runs:
            if (
                isinstance(raw_run, dict)
                and raw_run.get("external_id") == external_id
            ):
                return CheckRun(
                    id=int(raw_run["id"]),
                    html_url=str(raw_run["html_url"]),
                )
        return None

    async def dispatch_workflow(
        self,
        repository: str,
        default_branch: str,
        installation_token: str,
        *,
        base_sha: str,
        head_sha: str,
        base_repository: str,
        head_repository: str,
        pull_request_number: int,
        check_run_id: int,
        external_id: str,
    ) -> WorkflowRun:
        delivery_id = external_id.removeprefix("fraeno:")
        await self._request(
            "POST",
            f"/repos/{repository}/actions/workflows/"
            f"{self.settings.workflow_file}/dispatches",
            token=installation_token,
            json={
                "ref": default_branch,
                "inputs": {
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                    "base_repository": base_repository,
                    "head_repository": head_repository,
                    "pull_request_number": str(pull_request_number),
                    "check_run_id": str(check_run_id),
                    "delivery_id": delivery_id,
                },
            },
        )
        for delay in self.dispatch_poll_delays:
            if delay:
                await asyncio.sleep(delay)
            run = await self.find_workflow_run(
                repository, installation_token, delivery_id
            )
            if run is not None:
                return run
        raise GitHubApiError(
            "GitHub accepted the workflow dispatch but the Fraeno run "
            "did not appear in time",
            retryable=True,
        )

    async def find_workflow_run(
        self,
        repository: str,
        installation_token: str,
        delivery_id: str,
    ) -> WorkflowRun | None:
        response = await self._request(
            "GET",
            f"/repos/{repository}/actions/workflows/"
            f"{self.settings.workflow_file}/runs",
            token=installation_token,
            params={
                "event": "workflow_dispatch",
                "per_page": "100",
            },
        )
        raw_runs = response.get("workflow_runs", [])
        if not isinstance(raw_runs, list):
            raise GitHubApiError("GitHub returned malformed workflow runs")
        expected_title = f"Fraeno {delivery_id}"
        for raw_run in raw_runs:
            if (
                isinstance(raw_run, dict)
                and raw_run.get("display_title") == expected_title
            ):
                return WorkflowRun(
                    id=int(raw_run["id"]),
                    html_url=str(raw_run["html_url"]),
                    status=str(raw_run.get("status", "")),
                    conclusion=str(raw_run.get("conclusion") or ""),
                    path=str(raw_run.get("path") or ""),
                )
        return None

    async def workflow_available(
        self,
        repository: str,
        installation_token: str,
    ) -> bool:
        response = await self._request(
            "GET",
            f"/repos/{repository}/actions/workflows/{self.settings.workflow_file}",
            token=installation_token,
            allowed_statuses={404},
        )
        return bool(response)

    async def workflow_run(
        self,
        repository: str,
        workflow_run_id: int,
        installation_token: str,
    ) -> WorkflowRun:
        response = await self._request(
            "GET",
            f"/repos/{repository}/actions/runs/{workflow_run_id}",
            token=installation_token,
        )
        try:
            return WorkflowRun(
                id=int(response["id"]),
                html_url=str(response["html_url"]),
                status=str(response["status"]),
                conclusion=str(response.get("conclusion") or ""),
                path=str(response.get("path") or ""),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise GitHubApiError(
                "GitHub returned a malformed workflow run"
            ) from error

    async def app_delivery(self, github_delivery_id: int) -> dict[str, Any]:
        return await self._app_request(
            "GET",
            f"/app/hook/deliveries/{github_delivery_id}",
        )

    async def redeliver_app_delivery(self, github_delivery_id: int) -> None:
        await self._app_request(
            "POST",
            f"/app/hook/deliveries/{github_delivery_id}/attempts",
        )

    async def credential_readiness(self) -> dict[str, Any]:
        await self._request(
            "GET",
            "/app",
            token=create_app_jwt(
                self.settings.app_id, self.settings.private_key
            ),
        )
        previous_configured = bool(self.settings.previous_private_key)
        overlap_active = bool(
            previous_configured
            and self.settings.credential_rotation is not None
            and self.settings.credential_rotation.accepts_previous()
        )
        previous_valid = False
        if overlap_active:
            await self._request(
                "GET",
                "/app",
                token=create_app_jwt(
                    self.settings.app_id,
                    self.settings.previous_private_key,
                ),
            )
            previous_valid = True
        return {
            "status": "ok",
            "active_key_valid": True,
            "previous_key_configured": previous_configured,
            "overlap_active": overlap_active,
            "previous_key_valid": previous_valid,
        }

    async def _app_request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        allowed_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        try:
            return await self._request(
                method,
                path,
                token=create_app_jwt(
                    self.settings.app_id, self.settings.private_key
                ),
                json=json,
                params=params,
                allowed_statuses=allowed_statuses,
            )
        except GitHubApiError as error:
            if (
                error.status_code != 401
                or not self.settings.previous_private_key
                or self.settings.credential_rotation is None
                or not self.settings.credential_rotation.accepts_previous()
            ):
                raise
        LOGGER.warning("GitHub App authentication used the previous key")
        return await self._request(
            method,
            path,
            token=create_app_jwt(
                self.settings.app_id, self.settings.previous_private_key
            ),
            json=json,
            params=params,
            allowed_statuses=allowed_statuses,
        )

    async def _app_list_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> list[Any]:
        try:
            return await self._request_list(
                method,
                path,
                token=create_app_jwt(
                    self.settings.app_id, self.settings.private_key
                ),
                params=params,
            )
        except GitHubApiError as error:
            if (
                error.status_code != 401
                or not self.settings.previous_private_key
                or self.settings.credential_rotation is None
                or not self.settings.credential_rotation.accepts_previous()
            ):
                raise
        LOGGER.warning("GitHub App authentication used the previous key")
        return await self._request_list(
            method,
            path,
            token=create_app_jwt(
                self.settings.app_id, self.settings.previous_private_key
            ),
            params=params,
        )

    async def pull_request(
        self,
        repository: str,
        pull_request_number: int,
        installation_token: str,
    ) -> PullRequestState:
        response = await self._request(
            "GET",
            f"/repos/{repository}/pulls/{pull_request_number}",
            token=installation_token,
        )
        head = response.get("head")
        base = response.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise GitHubApiError("GitHub returned a malformed pull request")
        head_repository = head.get("repo")
        base_repository = base.get("repo")
        if not isinstance(head_repository, dict) or not isinstance(
            base_repository, dict
        ):
            raise GitHubApiError("The pull request repository is no longer available")
        try:
            return PullRequestState(
                number=int(response["number"]),
                title=str(response["title"]),
                state=str(response["state"]),
                draft=bool(response.get("draft", False)),
                head_sha=str(head["sha"]),
                base_sha=str(base["sha"]),
                head_repository=str(head_repository["full_name"]),
                base_repository=str(base_repository["full_name"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise GitHubApiError("GitHub returned a malformed pull request") from error

    async def cancel_workflow_run(
        self,
        repository: str,
        workflow_run_id: int,
        installation_token: str,
    ) -> None:
        await self._request(
            "POST",
            f"/repos/{repository}/actions/runs/{workflow_run_id}/cancel",
            token=installation_token,
            allowed_statuses={404, 409},
        )

    async def validation_report(
        self,
        repository: str,
        workflow_run_id: int,
        check_run_id: int,
        installation_token: str,
    ) -> dict[str, Any] | None:
        response = await self._request(
            "GET",
            f"/repos/{repository}/actions/runs/{workflow_run_id}/artifacts",
            token=installation_token,
            params={"per_page": "100"},
        )
        artifacts = response.get("artifacts", [])
        if not isinstance(artifacts, list):
            raise GitHubApiError("GitHub returned malformed workflow artifacts")
        expected_name = f"fraeno-report-{check_run_id}"
        artifact = next(
            (
                value
                for value in artifacts
                if isinstance(value, dict)
                and value.get("name") == expected_name
                and not value.get("expired", False)
            ),
            None,
        )
        if artifact is None:
            return None
        archive_url = artifact.get("archive_download_url")
        if not isinstance(archive_url, str) or not archive_url:
            raise GitHubApiError("Fraeno report artifact has no download URL")
        archive = await self._request_bytes(
            "GET",
            archive_url,
            token=installation_token,
        )
        if len(archive) > MAX_REPORT_ARCHIVE_BYTES:
            raise GitHubApiError("Fraeno report archive is too large")
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                report_name = next(
                    (
                        name
                        for name in bundle.namelist()
                        if name.rsplit("/", maxsplit=1)[-1] == "fraeno-report.json"
                    ),
                    None,
                )
                if report_name is None:
                    raise GitHubApiError(
                        "Fraeno report archive does not contain fraeno-report.json"
                    )
                info = bundle.getinfo(report_name)
                if info.file_size > MAX_REPORT_BYTES:
                    raise GitHubApiError("Fraeno report is too large")
                raw_report = bundle.read(info)
        except zipfile.BadZipFile as error:
            raise GitHubApiError("Fraeno report artifact is not a valid zip file") from error
        try:
            report = json.loads(raw_report)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubApiError("Fraeno report is not valid JSON") from error
        if not isinstance(report, dict):
            raise GitHubApiError("Fraeno report must be a JSON object")
        return report

    async def update_check_run(
        self,
        repository: str,
        check_run_id: int,
        installation_token: str,
        *,
        status: str,
        details_url: str,
        conclusion: str | None = None,
        title: str,
        summary: str,
    ) -> None:
        payload: dict[str, Any] = {
            "status": status,
            "details_url": details_url,
            "output": {"title": title, "summary": summary},
        }
        if conclusion is not None:
            payload["conclusion"] = conclusion
        await self._request(
            "PATCH",
            f"/repos/{repository}/check-runs/{check_run_id}",
            token=installation_token,
            json=payload,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        allowed_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method,
                path,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": self.settings.github_api_version,
                    "User-Agent": "fraeno-github-app",
                },
                json=json,
                params=params,
            )
        except httpx.RequestError as error:
            raise GitHubApiError(
                "GitHub API request failed before a response",
                retryable=True,
            ) from error
        if response.is_error and response.status_code not in (allowed_statuses or set()):
            request_id = response.headers.get("x-github-request-id", "unknown")
            raise GitHubApiError(
                f"GitHub API returned {response.status_code}; request id {request_id}",
                retryable=(
                    response.status_code in {408, 429}
                    or response.status_code >= 500
                ),
                status_code=response.status_code,
            )
        if not response.content:
            return {}
        data = response.json()
        if not isinstance(data, dict):
            raise GitHubApiError("GitHub API returned an unexpected response")
        return data

    async def _request_list(
        self,
        method: str,
        path: str,
        *,
        token: str,
        params: dict[str, str] | None = None,
    ) -> list[Any]:
        try:
            response = await self._client.request(
                method,
                path,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": self.settings.github_api_version,
                    "User-Agent": "fraeno-github-app",
                },
                params=params,
            )
        except httpx.RequestError as error:
            raise GitHubApiError(
                "GitHub API request failed before a response",
                retryable=True,
            ) from error
        if response.is_error:
            request_id = response.headers.get("x-github-request-id", "unknown")
            raise GitHubApiError(
                f"GitHub API returned {response.status_code}; request id {request_id}",
                retryable=(
                    response.status_code in {408, 429}
                    or response.status_code >= 500
                ),
                status_code=response.status_code,
            )
        data = response.json()
        if not isinstance(data, list):
            raise GitHubApiError("GitHub API returned an unexpected response")
        return data

    async def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        token: str,
    ) -> bytes:
        try:
            response = await self._client.request(
                method,
                path,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": self.settings.github_api_version,
                    "User-Agent": "fraeno-github-app",
                },
                follow_redirects=True,
            )
        except httpx.RequestError as error:
            raise GitHubApiError(
                "GitHub API request failed before a response",
                retryable=True,
            ) from error
        if response.is_error:
            request_id = response.headers.get("x-github-request-id", "unknown")
            raise GitHubApiError(
                f"GitHub API returned {response.status_code}; request id {request_id}",
                retryable=(
                    response.status_code in {408, 429}
                    or response.status_code >= 500
                ),
                status_code=response.status_code,
            )
        return response.content
