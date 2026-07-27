import json

import httpx
import pytest

from fraeno.github_app.client import GitHubApiError, GitHubClient
from fraeno.github_app.settings import AppSettings


def client_with(handler: httpx.MockTransport) -> GitHubClient:
    settings = AppSettings(
        app_id="1",
        private_key="key",
        github_api_url="https://github.test",
    )
    http_client = httpx.AsyncClient(
        base_url=settings.github_api_url,
        transport=handler,
    )
    return GitHubClient(settings, http_client)


@pytest.mark.anyio
async def test_workflow_dispatch_carries_recovery_identity() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["inputs"]["delivery_id"] == "delivery-7"
        assert payload["inputs"]["head_sha"] == "candidate"
        return httpx.Response(
            200,
            json={"workflow_run_id": 300, "html_url": "https://github.test/run/300"},
        )

    client = client_with(httpx.MockTransport(respond))
    run = await client.dispatch_workflow(
        "deepubuntu/fraeno",
        "main",
        "token",
        base_sha="baseline",
        head_sha="candidate",
        pull_request_number=7,
        check_run_id=200,
        external_id="fraeno:delivery-7",
    )
    await client.close()

    assert run.id == 300


@pytest.mark.anyio
async def test_workflow_recovery_uses_unique_run_title() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        assert "head_sha" not in request.url.params
        return httpx.Response(
            200,
            json={
                "workflow_runs": [
                    {
                        "id": 300,
                        "display_title": "Fraeno delivery-7",
                        "html_url": "https://github.test/run/300",
                    }
                ]
            },
        )

    client = client_with(httpx.MockTransport(respond))
    run = await client.find_workflow_run(
        "deepubuntu/fraeno",
        "token",
        "delivery-7",
    )
    await client.close()

    assert run is not None
    assert run.id == 300


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(403, False), (429, True), (503, True)],
)
async def test_github_error_classification(
    status_code: int, retryable: bool
) -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, headers={"x-github-request-id": "request"})

    client = client_with(httpx.MockTransport(respond))
    with pytest.raises(GitHubApiError) as raised:
        await client.find_workflow_run(
            "deepubuntu/fraeno",
            "token",
            "delivery",
        )
    await client.close()

    assert raised.value.retryable is retryable
