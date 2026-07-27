from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime
from typing import Any

import jwt

from fraeno.github_app.settings import CredentialRotationWindow


def verify_webhook_signature(
    body: bytes, signature_header: str | None, secret: str
) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    received = signature_header.removeprefix("sha256=")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


def verify_rotating_webhook_signature(
    body: bytes,
    signature_header: str | None,
    *,
    active_secret: str,
    previous_secret: str = "",
    rotation: CredentialRotationWindow | None = None,
    now: datetime | None = None,
) -> str | None:
    active_matches = verify_webhook_signature(
        body, signature_header, active_secret
    )
    previous_matches = bool(
        previous_secret
        and verify_webhook_signature(body, signature_header, previous_secret)
    )
    if active_matches:
        return "active"
    if (
        previous_matches
        and rotation is not None
        and rotation.accepts_previous(now)
    ):
        return "previous"
    return None


def create_app_jwt(app_id: str, private_key: str, now: int | None = None) -> str:
    current = int(time.time()) if now is None else now
    claims: dict[str, Any] = {
        "iat": current - 60,
        "exp": current + 9 * 60,
        "iss": app_id,
    }
    return jwt.encode(claims, private_key, algorithm="RS256")
