from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status

from fraeno.github_app.auth import verify_webhook_signature
from fraeno.github_app.client import GitHubClient
from fraeno.github_app.handler import EventHandler
from fraeno.github_app.settings import AppSettings, SettingsError, WebhookSettings
from fraeno.github_app.store import FirestoreEventStore
from fraeno.github_app.tasks import CloudTasksEnqueuer, TaskEnqueuer

LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 1_000_000


def build_handler(settings: AppSettings) -> EventHandler:
    return EventHandler(GitHubClient(settings), FirestoreEventStore())


def request_too_large(request: Request) -> bool:
    content_length = request.headers.get("content-length")
    if content_length is None:
        return False
    try:
        return int(content_length) > MAX_REQUEST_BYTES
    except ValueError:
        return True


async def json_object(request: Request) -> dict[str, Any]:
    if request_too_large(request):
        raise HTTPException(status_code=413, detail="Request payload is too large")
    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Request payload is too large")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be an object")
    return payload


def create_webhook_app(
    settings: WebhookSettings | None = None,
    enqueuer: TaskEnqueuer | None = None,
) -> FastAPI:
    resolved_settings = settings

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal resolved_settings
        created_enqueuer = False
        if resolved_settings is None:
            try:
                resolved_settings = WebhookSettings.from_environment()
            except SettingsError as error:
                LOGGER.warning("Fraeno webhook is not configured: %s", error)
        if app.state.enqueuer is None and resolved_settings is not None:
            app.state.enqueuer = CloudTasksEnqueuer(resolved_settings)
            created_enqueuer = True
        try:
            yield
        finally:
            if created_enqueuer:
                await app.state.enqueuer.close()

    app = FastAPI(
        title="Fraeno GitHub webhook",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.enqueuer = enqueuer

    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "webhook",
            "configured": app.state.enqueuer is not None,
        }

    @app.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(request: Request) -> Response:
        active_settings = resolved_settings
        active_enqueuer: TaskEnqueuer | None = app.state.enqueuer
        if active_settings is None or active_enqueuer is None:
            raise HTTPException(status_code=503, detail="Webhook is not configured")
        if request_too_large(request):
            raise HTTPException(status_code=413, detail="Webhook payload is too large")
        body = await request.body()
        if len(body) > MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="Webhook payload is too large")
        if not verify_webhook_signature(
            body,
            request.headers.get("x-hub-signature-256"),
            active_settings.webhook_secret,
        ):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

        event = request.headers.get("x-github-event", "")
        delivery_id = request.headers.get("x-github-delivery", "")
        if not event or not delivery_id:
            raise HTTPException(status_code=400, detail="Missing GitHub webhook headers")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from error
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be an object")

        await active_enqueuer.enqueue(event, delivery_id, payload)
        return Response(status_code=status.HTTP_202_ACCEPTED)

    return app


def create_worker_app(
    settings: AppSettings | None = None,
    handler: EventHandler | None = None,
) -> FastAPI:
    resolved_settings = settings

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal resolved_settings
        created_handler = False
        if resolved_settings is None:
            try:
                resolved_settings = AppSettings.from_environment()
            except SettingsError as error:
                LOGGER.warning("Fraeno worker is not configured: %s", error)
        if app.state.handler is None and resolved_settings is not None:
            app.state.handler = build_handler(resolved_settings)
            created_handler = True
        try:
            yield
        finally:
            if created_handler:
                await app.state.handler.client.close()

    app = FastAPI(
        title="Fraeno GitHub worker",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.handler = handler

    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "worker",
            "configured": app.state.handler is not None,
        }

    @app.post("/internal/github-events", status_code=status.HTTP_204_NO_CONTENT)
    async def process_github_event(request: Request) -> Response:
        active_handler: EventHandler | None = app.state.handler
        if active_handler is None:
            raise HTTPException(status_code=503, detail="Worker is not configured")
        if not request.headers.get("x-cloudtasks-taskname"):
            raise HTTPException(status_code=403, detail="Cloud Tasks request required")
        envelope = await json_object(request)
        event = envelope.get("event")
        delivery_id = envelope.get("delivery_id")
        payload = envelope.get("payload")
        if (
            not isinstance(event, str)
            or not event
            or not isinstance(delivery_id, str)
            or not delivery_id
            or not isinstance(payload, dict)
        ):
            raise HTTPException(status_code=400, detail="Invalid event envelope")
        raw_retry_count = request.headers.get("x-cloudtasks-taskretrycount", "0")
        try:
            retry = int(raw_retry_count) > 0
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid Cloud Tasks retry count"
            ) from None
        if not await active_handler.store.claim_delivery(
            delivery_id, retry=retry
        ):
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        try:
            await active_handler.process(event, delivery_id, payload)
        except Exception as error:
            raise HTTPException(
                status_code=500, detail="Event processing failed"
            ) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


def create_service_app() -> FastAPI:
    mode = os.environ.get("FRAENO_SERVICE_MODE", "webhook")
    if mode == "webhook":
        return create_webhook_app()
    if mode == "worker":
        return create_worker_app()
    raise RuntimeError("FRAENO_SERVICE_MODE must be 'webhook' or 'worker'")


app = create_service_app()
