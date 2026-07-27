from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response, status

from fraeno.github_app.auth import verify_webhook_signature
from fraeno.github_app.client import GitHubClient
from fraeno.github_app.handler import EventHandler
from fraeno.github_app.settings import AppSettings, SettingsError
from fraeno.github_app.store import FirestoreEventStore, MemoryEventStore

LOGGER = logging.getLogger(__name__)
MAX_WEBHOOK_BYTES = 1_000_000


def build_handler(settings: AppSettings) -> EventHandler:
    store = (
        FirestoreEventStore()
        if os.environ.get("FRAENO_STORE", "memory") == "firestore"
        else MemoryEventStore()
    )
    return EventHandler(GitHubClient(settings), store)


def create_app(
    settings: AppSettings | None = None,
    handler: EventHandler | None = None,
) -> FastAPI:
    resolved_settings = settings

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal resolved_settings
        if resolved_settings is None:
            try:
                resolved_settings = AppSettings.from_environment()
            except SettingsError as error:
                LOGGER.warning("GitHub App is not configured: %s", error)
        if app.state.handler is None and resolved_settings is not None:
            app.state.handler = build_handler(resolved_settings)
        yield

    app = FastAPI(title="Fraeno GitHub App", version="0.1.0", lifespan=lifespan)
    app.state.handler = handler
    app.state.settings = resolved_settings

    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "configured": app.state.handler is not None,
        }

    @app.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(
        request: Request, background_tasks: BackgroundTasks
    ) -> Response:
        active_settings: AppSettings | None = resolved_settings
        active_handler: EventHandler | None = app.state.handler
        if active_settings is None or active_handler is None:
            raise HTTPException(status_code=503, detail="GitHub App is not configured")
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Webhook payload is too large")
        body = await request.body()
        if len(body) > MAX_WEBHOOK_BYTES:
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
        if not await active_handler.store.claim_delivery(delivery_id):
            return Response(status_code=status.HTTP_202_ACCEPTED)

        background_tasks.add_task(
            active_handler.process, event, delivery_id, payload
        )
        return Response(status_code=status.HTTP_202_ACCEPTED)

    return app


app = create_app()
