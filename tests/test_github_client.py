import io
import json
import zipfile

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
        assert payload["inputs"]["base_repository"] == "deepubuntu/fraeno"
        assert payload["inputs"]["head_repository"] == "contributor/fraeno"
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
        base_repository="deepubuntu/fraeno",
        head_repository="contributor/fraeno",
        pull_request_number=7,
        check_run_id=200,
        external_id="fraeno:delivery-7",
    )
    await client.close()

    assert run.id == 300


@pytest.mark.anyio
async def test_pull_request_returns_current_head_and_fork_repository() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "number": 7,
                "title": "Update driver",
                "state": "open",
                "draft": False,
                "head": {
                    "sha": "candidate",
                    "repo": {"full_name": "contributor/fraeno"},
                },
                "base": {
                    "sha": "baseline",
                    "repo": {"full_name": "deepubuntu/fraeno"},
                },
            },
        )

    client = client_with(httpx.MockTransport(respond))
    pull_request = await client.pull_request("deepubuntu/fraeno", 7, "token")
    await client.close()

    assert pull_request.head_sha == "candidate"
    assert pull_request.head_repository == "contributor/fraeno"
    assert pull_request.base_repository == "deepubuntu/fraeno"


@pytest.mark.anyio
async def test_validation_report_is_loaded_from_expected_artifact() -> None:
    report = {
        "outcome": "block",
        "comparison": {"validation_level": "L2", "findings": []},
    }
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as bundle:
        bundle.writestr("fraeno-report.json", json.dumps(report))

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {
                            "name": "fraeno-report-200",
                            "expired": False,
                            "archive_download_url": "https://github.test/artifact.zip",
                        }
                    ]
                },
            )
        assert request.url.path == "/artifact.zip"
        return httpx.Response(200, content=archive_buffer.getvalue())

    client = client_with(httpx.MockTransport(respond))
    loaded = await client.validation_report(
        "deepubuntu/fraeno",
        workflow_run_id=300,
        check_run_id=200,
        installation_token="token",
    )
    await client.close()

    assert loaded == report


@pytest.mark.anyio
async def test_cancel_already_completed_workflow_is_safe() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409)

    client = client_with(httpx.MockTransport(respond))
    await client.cancel_workflow_run("deepubuntu/fraeno", 300, "token")
    await client.close()


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
async def test_repository_readiness_distinguishes_missing_workflow() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/fraeno-validation.yml"):
            return httpx.Response(404)
        return httpx.Response(500)

    client = client_with(httpx.MockTransport(respond))
    available = await client.workflow_available(
        "deepubuntu/customer-robot", "token"
    )
    await client.close()

    assert available is False


@pytest.mark.anyio
async def test_workflow_state_can_be_reconciled_after_lost_webhook() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/actions/runs/300")
        return httpx.Response(
            200,
            json={
                "id": 300,
                "html_url": "https://github.test/runs/300",
                "status": "completed",
                "conclusion": "success",
                "path": ".github/workflows/fraeno-validation.yml",
            },
        )

    client = client_with(httpx.MockTransport(respond))
    run = await client.workflow_run("deepubuntu/fraeno", 300, "token")
    await client.close()

    assert run.status == "completed"
    assert run.conclusion == "success"


@pytest.mark.anyio
async def test_app_delivery_redelivery_uses_app_jwt_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "fraeno.github_app.client.create_app_jwt",
        lambda app_id, private_key: f"jwt-{app_id}-{private_key}",
    )
    requests: list[tuple[str, str]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": 812,
                    "guid": "91c2d46f-17b2-4d9d-aa0d-100e079c0c20",
                },
            )
        return httpx.Response(202)

    client = client_with(httpx.MockTransport(respond))
    delivery = await client.app_delivery(812)
    await client.redeliver_app_delivery(812)
    await client.close()

    assert delivery["guid"] == "91c2d46f-17b2-4d9d-aa0d-100e079c0c20"
    assert requests == [
        ("GET", "/app/hook/deliveries/812"),
        ("POST", "/app/hook/deliveries/812/attempts"),
    ]


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
