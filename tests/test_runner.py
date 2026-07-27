from pathlib import Path

from fraeno.config import load_config
from fraeno.validation.compare import Outcome
from fraeno.validation.runner import run_validation

FIXTURE = Path(__file__).parents[1] / "fixtures" / "deterministic_robot"


def test_end_to_end_safe_update() -> None:
    result = run_validation(
        FIXTURE / "baseline", FIXTURE / "safe", load_config(FIXTURE / "fraeno.yml")
    )
    assert result.outcome is Outcome.PASS


def test_end_to_end_breaking_update() -> None:
    result = run_validation(
        FIXTURE / "baseline", FIXTURE / "broken", load_config(FIXTURE / "fraeno.yml")
    )
    assert result.outcome is Outcome.BLOCK
    assert result.comparison is not None
    assert any(
        finding.code == "topic-qos-incompatible"
        for finding in result.comparison.findings
    )
