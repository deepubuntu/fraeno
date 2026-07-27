from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MessageRateWindow:
    """Count messages only after an explicit measurement window begins."""

    count: int = 0
    measuring: bool = False

    def begin(self) -> None:
        self.count = 0
        self.measuring = True

    def record(self) -> None:
        if self.measuring:
            self.count += 1

    def rate_hz(self, duration_seconds: float) -> float:
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than zero")
        return self.count / duration_seconds
