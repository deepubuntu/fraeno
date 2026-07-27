from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import jwt


def verify_webhook_signature(
    body: bytes, signature_header: str | None, secret: str
) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    received = signature_header.removeprefix("sha256=")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


def create_app_jwt(app_id: str, private_key: str, now: int | None = None) -> str:
    current = int(time.time()) if now is None else now
    claims: dict[str, Any] = {
        "iat": current - 60,
        "exp": current + 9 * 60,
        "iss": app_id,
    }
    return jwt.encode(claims, private_key, algorithm="RS256")
