from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class SettingsError(ValueError):
    pass


MAX_CREDENTIAL_ROTATION_WINDOW = timedelta(hours=1)


def _utc_timestamp(name: str, raw_value: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SettingsError(f"{name} must be an RFC 3339 UTC timestamp") from error
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise SettingsError(f"{name} must be an RFC 3339 UTC timestamp")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class CredentialRotationWindow:
    started_at: datetime
    previous_valid_until: datetime

    @classmethod
    def from_environment(
        cls,
        *,
        previous_credential_name: str,
        previous_credential: str,
    ) -> CredentialRotationWindow | None:
        started_raw = os.environ.get(
            "FRAENO_CREDENTIAL_ROTATION_STARTED_AT", ""
        ).strip()
        expires_raw = os.environ.get(
            "FRAENO_PREVIOUS_CREDENTIALS_VALID_UNTIL", ""
        ).strip()
        configured = {
            previous_credential_name: previous_credential,
            "FRAENO_CREDENTIAL_ROTATION_STARTED_AT": started_raw,
            "FRAENO_PREVIOUS_CREDENTIALS_VALID_UNTIL": expires_raw,
        }
        present = [name for name, value in configured.items() if value]
        if not present:
            return None
        if len(present) != len(configured):
            missing = [name for name, value in configured.items() if not value]
            raise SettingsError(
                "credential rotation settings must be configured together; "
                f"missing: {', '.join(missing)}"
            )
        started_at = _utc_timestamp(
            "FRAENO_CREDENTIAL_ROTATION_STARTED_AT", started_raw
        )
        previous_valid_until = _utc_timestamp(
            "FRAENO_PREVIOUS_CREDENTIALS_VALID_UNTIL", expires_raw
        )
        duration = previous_valid_until - started_at
        if duration <= timedelta(0):
            raise SettingsError(
                "FRAENO_PREVIOUS_CREDENTIALS_VALID_UNTIL must be after "
                "FRAENO_CREDENTIAL_ROTATION_STARTED_AT"
            )
        if duration > MAX_CREDENTIAL_ROTATION_WINDOW:
            raise SettingsError("credential overlap cannot exceed one hour")
        return cls(
            started_at=started_at,
            previous_valid_until=previous_valid_until,
        )

    def accepts_previous(self, now: datetime | None = None) -> bool:
        observed_at = now or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        observed_at = observed_at.astimezone(timezone.utc)
        return self.started_at <= observed_at < self.previous_valid_until


def _positive_integer(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise SettingsError(f"{name} must be a positive integer") from error
    if value <= 0:
        raise SettingsError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class AppSettings:
    app_id: str
    private_key: str
    previous_private_key: str = ""
    credential_rotation: CredentialRotationWindow | None = None
    workflow_file: str = "fraeno-validation.yml"
    github_api_url: str = "https://api.github.com"
    github_api_version: str = "2026-03-10"
    check_name: str = "Fraeno / robot integration"
    max_delivery_attempts: int = 5
    delivery_stale_seconds: int = 900
    run_stale_seconds: int = 900
    max_run_seconds: int = 7200
    delivery_retention_days: int = 14
    run_retention_days: int = 30
    repository_retention_days: int = 30
    replay_audit_retention_days: int = 90

    @classmethod
    def from_environment(cls) -> AppSettings:
        app_id = os.environ.get("FRAENO_GITHUB_APP_ID", "").strip()
        private_key = os.environ.get("FRAENO_GITHUB_PRIVATE_KEY", "").replace(
            "\\n", "\n"
        )
        previous_private_key = os.environ.get(
            "FRAENO_GITHUB_PRIVATE_KEY_PREVIOUS", ""
        ).replace("\\n", "\n")
        missing = [
            name
            for name, value in (
                ("FRAENO_GITHUB_APP_ID", app_id),
                ("FRAENO_GITHUB_PRIVATE_KEY", private_key),
            )
            if not value
        ]
        if missing:
            raise SettingsError(f"missing required settings: {', '.join(missing)}")
        credential_rotation = CredentialRotationWindow.from_environment(
            previous_credential_name="FRAENO_GITHUB_PRIVATE_KEY_PREVIOUS",
            previous_credential=previous_private_key,
        )
        if previous_private_key and previous_private_key == private_key:
            raise SettingsError(
                "active and previous GitHub private keys must be different"
            )
        return cls(
            app_id=app_id,
            private_key=private_key,
            previous_private_key=previous_private_key,
            credential_rotation=credential_rotation,
            workflow_file=os.environ.get(
                "FRAENO_GITHUB_WORKFLOW_FILE", "fraeno-validation.yml"
            ),
            github_api_url=os.environ.get(
                "FRAENO_GITHUB_API_URL", "https://api.github.com"
            ).rstrip("/"),
            max_delivery_attempts=_positive_integer(
                "FRAENO_MAX_DELIVERY_ATTEMPTS", 5
            ),
            delivery_stale_seconds=_positive_integer(
                "FRAENO_DELIVERY_STALE_SECONDS", 900
            ),
            run_stale_seconds=_positive_integer(
                "FRAENO_RUN_STALE_SECONDS", 900
            ),
            max_run_seconds=_positive_integer("FRAENO_MAX_RUN_SECONDS", 7200),
            delivery_retention_days=_positive_integer(
                "FRAENO_DELIVERY_RETENTION_DAYS", 14
            ),
            run_retention_days=_positive_integer(
                "FRAENO_RUN_RETENTION_DAYS", 30
            ),
            repository_retention_days=_positive_integer(
                "FRAENO_REPOSITORY_RETENTION_DAYS", 30
            ),
            replay_audit_retention_days=_positive_integer(
                "FRAENO_REPLAY_AUDIT_RETENTION_DAYS", 90
            ),
        )


@dataclass(frozen=True)
class WebhookSettings:
    webhook_secret: str
    gcp_project: str
    gcp_location: str
    queue_name: str
    worker_url: str
    task_service_account: str
    previous_webhook_secret: str = ""
    credential_rotation: CredentialRotationWindow | None = None

    @classmethod
    def from_environment(cls) -> WebhookSettings:
        values = {
            "FRAENO_GITHUB_WEBHOOK_SECRET": os.environ.get(
                "FRAENO_GITHUB_WEBHOOK_SECRET", ""
            ),
            "FRAENO_GCP_PROJECT": os.environ.get("FRAENO_GCP_PROJECT", ""),
            "FRAENO_GCP_LOCATION": os.environ.get(
                "FRAENO_GCP_LOCATION", "us-central1"
            ),
            "FRAENO_TASK_QUEUE": os.environ.get(
                "FRAENO_TASK_QUEUE", "fraeno-github-events"
            ),
            "FRAENO_WORKER_URL": os.environ.get("FRAENO_WORKER_URL", ""),
            "FRAENO_TASK_SERVICE_ACCOUNT": os.environ.get(
                "FRAENO_TASK_SERVICE_ACCOUNT", ""
            ),
        }
        missing = [name for name, value in values.items() if not value.strip()]
        if missing:
            raise SettingsError(f"missing required settings: {', '.join(missing)}")
        previous_webhook_secret = os.environ.get(
            "FRAENO_GITHUB_WEBHOOK_SECRET_PREVIOUS", ""
        ).strip()
        credential_rotation = CredentialRotationWindow.from_environment(
            previous_credential_name="FRAENO_GITHUB_WEBHOOK_SECRET_PREVIOUS",
            previous_credential=previous_webhook_secret,
        )
        if previous_webhook_secret and (
            previous_webhook_secret
            == values["FRAENO_GITHUB_WEBHOOK_SECRET"].strip()
        ):
            raise SettingsError(
                "active and previous webhook secrets must be different"
            )
        return cls(
            webhook_secret=values["FRAENO_GITHUB_WEBHOOK_SECRET"].strip(),
            gcp_project=values["FRAENO_GCP_PROJECT"].strip(),
            gcp_location=values["FRAENO_GCP_LOCATION"].strip(),
            queue_name=values["FRAENO_TASK_QUEUE"].strip(),
            worker_url=values["FRAENO_WORKER_URL"].rstrip("/"),
            task_service_account=values["FRAENO_TASK_SERVICE_ACCOUNT"].strip(),
            previous_webhook_secret=previous_webhook_secret,
            credential_rotation=credential_rotation,
        )
