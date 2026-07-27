from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fraeno.models import ScanReport


def build_lockfile(report: ScanReport, root: Path) -> dict[str, Any]:
    source_hashes: dict[str, str] = {}
    for relative in report.files_scanned:
        source = root / relative
        source_hashes[relative] = hashlib.sha256(source.read_bytes()).hexdigest()

    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "dependencies": [dependency.to_dict() for dependency in report.dependencies],
        "source_hashes": source_hashes,
        "warnings": report.warnings,
    }


def write_lockfile(lockfile: dict[str, Any], destination: Path) -> None:
    destination.write_text(json.dumps(lockfile, indent=2, sort_keys=True) + "\n")
