from __future__ import annotations

import os
from dataclasses import dataclass


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class AppSettings:
    app_id: str
    private_key: str
    webhook_secret: str
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
        webhook_secret = os.environ.get("FRAENO_GITHUB_WEBHOOK_SECRET", "")
        missing = [
            name
            for name, value in (
                ("FRAENO_GITHUB_APP_ID", app_id),
                ("FRAENO_GITHUB_PRIVATE_KEY", private_key),
                ("FRAENO_GITHUB_WEBHOOK_SECRET", webhook_secret),
            )
            if not value
        ]
        if missing:
            raise SettingsError(f"missing required settings: {', '.join(missing)}")
        return cls(
            app_id=app_id,
            private_key=private_key,
            webhook_secret=webhook_secret,
            workflow_file=os.environ.get(
                "FRAENO_GITHUB_WORKFLOW_FILE", "fraeno-validation.yml"
            ),
            github_api_url=os.environ.get(
                "FRAENO_GITHUB_API_URL", "https://api.github.com"
            ).rstrip("/"),
        )
