from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Protocol


class MetricSink(Protocol):
    def emit(self, name: str, value: float = 1.0) -> None: ...


class JsonLogMetricSink:
    """Write low-cardinality operational events for Cloud Logging alerts."""

    def emit(self, name: str, value: float = 1.0) -> None:
        record = {
            "severity": "INFO",
            "fraeno_metric": name,
            "value": value,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        sys.stdout.write(f"{json.dumps(record, separators=(',', ':'))}\n")
        sys.stdout.flush()


class NullMetricSink:
    def emit(self, name: str, value: float = 1.0) -> None:
        del name, value
