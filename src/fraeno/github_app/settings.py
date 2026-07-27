from __future__ import annotations

import os
from dataclasses import dataclass


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class AppSettings:
    app_id: str
    private_key: str
    workflow_file: str = "fraeno-validation.yml"
    github_api_url: str = "https://api.github.com"
    github_api_version: str = "2026-03-10"
    check_name: str = "Fraeno / robot integration"

    @classmethod
    def from_environment(cls) -> AppSettings:
        app_id = os.environ.get("FRAENO_GITHUB_APP_ID", "").strip()
        private_key = os.environ.get("FRAENO_GITHUB_PRIVATE_KEY", "").replace(
            "\\n", "\n"
        )
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
        return cls(
            app_id=app_id,
            private_key=private_key,
            workflow_file=os.environ.get(
                "FRAENO_GITHUB_WORKFLOW_FILE", "fraeno-validation.yml"
            ),
            github_api_url=os.environ.get(
                "FRAENO_GITHUB_API_URL", "https://api.github.com"
            ).rstrip("/"),
        )


@dataclass(frozen=True)
class WebhookSettings:
    webhook_secret: str
    gcp_project: str
    gcp_location: str
    queue_name: str
    worker_url: str
    task_service_account: str

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
        return cls(
            webhook_secret=values["FRAENO_GITHUB_WEBHOOK_SECRET"].strip(),
            gcp_project=values["FRAENO_GCP_PROJECT"].strip(),
            gcp_location=values["FRAENO_GCP_LOCATION"].strip(),
            queue_name=values["FRAENO_TASK_QUEUE"].strip(),
            worker_url=values["FRAENO_WORKER_URL"].rstrip("/"),
            task_service_account=values["FRAENO_TASK_SERVICE_ACCOUNT"].strip(),
        )
