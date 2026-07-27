from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from fraeno.github_app.auth import create_app_jwt
from fraeno.github_app.settings import AppSettings


class GitHubApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckRun:
    id: int
    html_url: str


@dataclass(frozen=True)
class WorkflowRun:
    id: int
    html_url: str


class GitHubClient:
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

    async def installation_token(
        self, installation_id: int, repository_id: int
    ) -> str:
        response = await self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            token=create_app_jwt(
                self.settings.app_id, self.settings.private_key
            ),
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

    async def dispatch_workflow(
        self,
        repository: str,
        default_branch: str,
        installation_token: str,
        *,
        base_sha: str,
        head_sha: str,
        pull_request_number: int,
        check_run_id: int,
    ) -> WorkflowRun:
        response = await self._request(
            "POST",
            f"/repos/{repository}/actions/workflows/"
            f"{self.settings.workflow_file}/dispatches",
            token=installation_token,
            json={
                "ref": default_branch,
                "inputs": {
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                    "pull_request_number": str(pull_request_number),
                    "check_run_id": str(check_run_id),
                },
            },
        )
        return WorkflowRun(
            id=int(response["workflow_run_id"]),
            html_url=str(response["html_url"]),
        )

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
        json: dict[str, Any],
    ) -> dict[str, Any]:
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
        )
        if response.is_error:
            request_id = response.headers.get("x-github-request-id", "unknown")
            raise GitHubApiError(
                f"GitHub API returned {response.status_code}; request id {request_id}"
            )
        if not response.content:
            return {}
        data = response.json()
        if not isinstance(data, dict):
            raise GitHubApiError("GitHub API returned an unexpected response")
        return data
