import hashlib
import hmac

from fraeno.github_app.auth import verify_webhook_signature


def test_webhook_signature_verification() -> None:
    body = b'{"action":"opened"}'
    secret = "test-secret"
    signature = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    assert verify_webhook_signature(body, signature, secret)
    assert not verify_webhook_signature(body + b"x", signature, secret)
    assert not verify_webhook_signature(body, None, secret)
