from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EXPECTED_METRICS = {
    "signature_rejection",
    "queue_delay_seconds",
    "delivery_retry",
    "dispatch_failure",
    "run_duration_seconds",
    "stale_check",
}
EXPECTED_TTL_COLLECTIONS = {
    "github_deliveries",
    "github_runs",
    "github_repositories",
    "github_replay_audit",
}


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def validate_policy(path: Path) -> str:
    policy = load_object(path)
    if policy.get("enabled") is not False:
        raise ValueError(f"{path} must stay disabled until a channel is chosen")
    if policy.get("notificationChannels") != []:
        raise ValueError(f"{path} must not invent a notification destination")
    conditions = policy.get("conditions")
    if not isinstance(conditions, list) or len(conditions) != 1:
        raise ValueError(f"{path} must have one low-noise log condition")
    condition = conditions[0]
    if not isinstance(condition, dict):
        raise ValueError(f"{path} has an invalid condition")
    matched_log = condition.get("conditionMatchedLog")
    if not isinstance(matched_log, dict):
        raise ValueError(f"{path} must use a log match without custom metrics")
    filter_value = matched_log.get("filter")
    if not isinstance(filter_value, str):
        raise ValueError(f"{path} has no log filter")
    matches = [
        metric
        for metric in EXPECTED_METRICS
        if f'fraeno_metric="{metric}"' in filter_value
    ]
    if len(matches) != 1:
        raise ValueError(f"{path} must cover exactly one Fraeno metric")
    strategy = policy.get("alertStrategy")
    if not isinstance(strategy, dict) or "notificationRateLimit" not in strategy:
        raise ValueError(f"{path} must rate-limit notifications")
    return matches[0]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    policy_paths = sorted((root / "deploy/gcp/alert-policies").glob("*.json"))
    covered = {validate_policy(path) for path in policy_paths}
    if covered != EXPECTED_METRICS:
        missing = ", ".join(sorted(EXPECTED_METRICS - covered))
        raise ValueError(f"alert policy coverage is incomplete: {missing}")

    retention = load_object(root / "deploy/gcp/retention.json")
    if retention.get("ttlField") != "expires_at":
        raise ValueError("Firestore TTL must use the timestamp field expires_at")
    configured = set(retention.get("collectionGroups", []))
    if configured != EXPECTED_TTL_COLLECTIONS:
        raise ValueError("Firestore TTL collection coverage is incomplete")
    if "github_active_runs" not in retention.get(
        "excludedCollectionGroups", []
    ):
        raise ValueError("active run locks must be reconciled, not expired")
    print(
        "Validated six disabled log alerts and four Firestore TTL collection "
        "groups."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
