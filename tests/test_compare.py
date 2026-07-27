import json
from pathlib import Path

from fraeno.config import load_config
from fraeno.validation.compare import Outcome, compare_systems, qos_compatible
from fraeno.validation.observation import QosProfile, SystemObservation

FIXTURE = Path(__file__).parents[1] / "fixtures" / "deterministic_robot"


def observation(name: str) -> SystemObservation:
    raw = json.loads((FIXTURE / name / "observation.json").read_text())
    return SystemObservation.from_dict(raw)


def test_safe_update_passes() -> None:
    config = load_config(FIXTURE / "fraeno.yml")
    report = compare_systems(
        observation("baseline"), observation("safe"), config.validation
    )
    assert report.outcome is Outcome.PASS
    assert report.findings == ()


def test_qos_regression_blocks_buildable_update() -> None:
    config = load_config(FIXTURE / "fraeno.yml")
    report = compare_systems(
        observation("baseline"), observation("broken"), config.validation
    )
    assert report.outcome is Outcome.BLOCK
    codes = {finding.code for finding in report.findings}
    assert "topic-qos-incompatible" in codes
    assert "topic-rate-below-minimum" in codes
    assert "diagnostic-regressed" in codes


def test_best_effort_cannot_satisfy_reliable_subscriber() -> None:
    offered = QosProfile(reliability="best_effort")
    requested = QosProfile(reliability="reliable")
    assert not qos_compatible(offered, requested)


def test_unhealthy_baseline_is_error() -> None:
    config = load_config(FIXTURE / "fraeno.yml")
    report = compare_systems(
        observation("broken"), observation("safe"), config.validation
    )
    assert report.outcome is Outcome.ERROR
