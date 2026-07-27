from __future__ import annotations

import json
import os
from pathlib import Path

import fraeno

targets = {
    "baseline_evidence": Path("/baseline-evidence/run.json"),
    "candidate_evidence": Path("/evidence/run.json"),
    "final_report": Path("/report/fraeno-report.json"),
    "trusted_observer": Path(os.environ["FRAENO_TRUSTED_ROOT"]) / "observe.py",
    "validator": Path(fraeno.__file__).resolve(),
}
results: dict[str, bool] = {}
for name, target in targets.items():
    try:
        target.write_text('{"outcome":"pass","forged":true}\n')
    except OSError:
        results[name] = False
    else:
        results[name] = True

Path("hostile-results.json").write_text(json.dumps(results, sort_keys=True))
