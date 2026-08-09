from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status

from fraeno.github_app.auth import verify_rotating_webhook_signature
from fraeno.github_app.client import GitHubClient
from fraeno.github_app.handler import EventHandler
from fraeno.github_app.metrics import JsonLogMetricSink, MetricSink
from fraeno.github_app.recovery import Reconciler
from fraeno.github_app.settings import AppSettings, SettingsError, WebhookSettings
from fraeno.github_app.store import FirestoreEventStore
from fraeno.github_app.tasks import CloudTasksEnqueuer, TaskEnqueuer

LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 1_000_000


def build_handler(
    settings: AppSettings, metrics: MetricSink | None = None
) -> EventHandler:
    return EventHandler(
        GitHubClient(settings),
        FirestoreEventStore(
            delivery_retention_days=settings.delivery_retention_days
        ),
        metrics,
    )


def request_too_large(request: Request) -> bool:
    content_length = request.headers.get("content-length")
    if content_length is None:
        return False
    try:
        declared_length = int(content_length)
    except ValueError:
        return True
    return declared_length < 0 or declared_length > MAX_REQUEST_BYTES


async def limited_request_body(request: Request) -> bytes:
    """Read a request body without buffering more than the configured limit."""
    if request_too_large(request):
        raise HTTPException(status_code=413, detail="Request payload is too large")
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > MAX_REQUEST_BYTES - len(body):
            raise HTTPException(status_code=413, detail="Request payload is too large")
        body.extend(chunk)
    return bytes(body)


async def json_object(request: Request) -> dict[str, Any]:
    body = await limited_request_body(request)
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
    metrics: MetricSink | None = None,
) -> FastAPI:
    resolved_settings = settings
    active_metrics = metrics or JsonLogMetricSink()

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
        version="0.2.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.enqueuer = enqueuer

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "webhook",
            "configured": app.state.enqueuer is not None,
        }

    @app.post(
        "/credential-readiness/webhook",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def webhook_credential_readiness(request: Request) -> Response:
        active_settings = resolved_settings
        if active_settings is None:
            raise HTTPException(status_code=503, detail="Webhook is not configured")
        body = await limited_request_body(request)
        matched_secret = verify_rotating_webhook_signature(
            body,
            request.headers.get("x-hub-signature-256"),
            active_secret=active_settings.webhook_secret,
            previous_secret=active_settings.previous_webhook_secret,
            rotation=active_settings.credential_rotation,
        )
        if matched_secret is None:
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(request: Request) -> Response:
        active_settings = resolved_settings
        active_enqueuer: TaskEnqueuer | None = app.state.enqueuer
        if active_settings is None or active_enqueuer is None:
            raise HTTPException(status_code=503, detail="Webhook is not configured")
        body = await limited_request_body(request)
        matched_secret = verify_rotating_webhook_signature(
            body,
            request.headers.get("x-hub-signature-256"),
            active_secret=active_settings.webhook_secret,
            previous_secret=active_settings.previous_webhook_secret,
            rotation=active_settings.credential_rotation,
        )
        if matched_secret is None:
            active_metrics.emit("signature_rejection")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        if matched_secret == "previous":
            active_metrics.emit("previous_webhook_secret_accepted")

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
    reconciler: Reconciler | None = None,
    metrics: MetricSink | None = None,
) -> FastAPI:
    resolved_settings = settings
    active_metrics = metrics or JsonLogMetricSink()

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
            app.state.handler = build_handler(resolved_settings, active_metrics)
            created_handler = True
        if (
            app.state.reconciler is None
            and isinstance(app.state.handler, EventHandler)
            and resolved_settings is not None
        ):
            app.state.reconciler = Reconciler(
                app.state.handler.client,
                app.state.handler.store,
                app.state.handler,
                resolved_settings,
                active_metrics,
            )
        try:
            yield
        finally:
            if created_handler:
                await app.state.handler.client.close()

    app = FastAPI(
        title="Fraeno GitHub worker",
        version="0.2.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.handler = handler
    app.state.reconciler = reconciler

    @app.get("/health")
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
            retry_count = int(raw_retry_count)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid Cloud Tasks retry count"
            ) from None
        retry = retry_count > 0
        if retry:
            active_metrics.emit("delivery_retry", float(retry_count))
        enqueued_at = envelope.get("enqueued_at")
        if isinstance(enqueued_at, str):
            try:
                queued_at = datetime.fromisoformat(enqueued_at)
                queue_delay = max(
                    0.0,
                    (datetime.now(timezone.utc) - queued_at).total_seconds(),
                )
            except ValueError:
                pass
            else:
                active_metrics.emit("queue_delay_seconds", queue_delay)
        if not await active_handler.store.claim_delivery(
            delivery_id, event=event, retry=retry
        ):
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        try:
            await active_handler.process(event, delivery_id, payload)
        except Exception as error:
            active_settings = resolved_settings
            if (
                active_settings is not None
                and retry_count + 1 >= active_settings.max_delivery_attempts
            ):
                await active_handler.store.dead_letter_delivery(
                    delivery_id,
                    error_kind=type(error).__name__,
                )
                return Response(status_code=status.HTTP_204_NO_CONTENT)
            raise HTTPException(
                status_code=500, detail="Event processing failed"
            ) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/internal/reconcile")
    async def reconcile(request: Request) -> dict[str, int]:
        active_reconciler: Reconciler | None = app.state.reconciler
        if active_reconciler is None:
            raise HTTPException(status_code=503, detail="Reconciler is not configured")
        if request.headers.get("x-cloudscheduler") != "true":
            raise HTTPException(
                status_code=403, detail="Cloud Scheduler request required"
            )
        return (await active_reconciler.reconcile()).to_dict()

    @app.post("/internal/credential-readiness")
    async def credential_readiness(request: Request) -> dict[str, Any]:
        active_handler: EventHandler | None = app.state.handler
        if active_handler is None:
            raise HTTPException(status_code=503, detail="Worker is not configured")
        if request.headers.get("x-fraeno-credential-check") != "true":
            raise HTTPException(
                status_code=403, detail="Credential verification request required"
            )
        return await active_handler.client.credential_readiness()

    return app


def create_service_app() -> FastAPI:
    mode = os.environ.get("FRAENO_SERVICE_MODE", "webhook")
    if mode == "webhook":
        return create_webhook_app()
    if mode == "worker":
        return create_worker_app()
    raise RuntimeError("FRAENO_SERVICE_MODE must be 'webhook' or 'worker'")


app = create_service_app()
