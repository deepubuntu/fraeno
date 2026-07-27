import json
from dataclasses import replace
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


def _with_topic_rate(
    source: SystemObservation, topic_name: str, rate_hz: float
) -> SystemObservation:
    topics = dict(source.topics)
    topics[topic_name] = replace(topics[topic_name], rate_hz=rate_hz)
    return replace(source, topics=topics)


def test_real_relative_rate_regression_still_blocks_above_absolute_minimum() -> None:
    baseline = observation("baseline")
    candidate = _with_topic_rate(observation("safe"), "/robot/command", 6.9)
    config = replace(
        load_config(FIXTURE / "fraeno.yml").validation,
        minimum_topic_rates_hz={"/robot/command": 5.0},
    )

    report = compare_systems(baseline, candidate, config)

    codes = {finding.code for finding in report.findings}
    assert report.outcome is Outcome.BLOCK
    assert "topic-rate-regressed" in codes
    assert "topic-rate-below-minimum" not in codes


def test_absolute_minimum_still_blocks_without_relative_regression() -> None:
    baseline = observation("baseline")
    candidate = _with_topic_rate(observation("safe"), "/sensor/reading", 14.9)
    config = load_config(FIXTURE / "fraeno.yml").validation

    report = compare_systems(baseline, candidate, config)

    codes = {finding.code for finding in report.findings}
    assert report.outcome is Outcome.BLOCK
    assert "topic-rate-below-minimum" in codes
    assert "topic-rate-regressed" not in codes
