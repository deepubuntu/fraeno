from dataclasses import replace
from pathlib import Path

from fraeno.config import load_config
from fraeno.validation.compare import Outcome, compare_systems
from fraeno.validation.observation import SystemObservation

ROOT = Path(__file__).parents[1]


def _config():
    config = load_config(ROOT / ".fraeno.yml").validation
    return replace(
        config,
        required_nodes=frozenset(),
        required_topics=frozenset(),
        required_services=frozenset(),
        required_actions=frozenset(),
        required_transforms=frozenset(),
        required_diagnostics=frozenset(),
        minimum_topic_rates_hz={},
    )


def _observation(evidence):
    return SystemObservation(
        healthy=True,
        graph_stable=True,
        nodes=frozenset(),
        topics={},
        services={},
        actions={},
        transforms=frozenset(),
        diagnostics={},
        metadata={"simulated_estop": evidence},
    )


SAFE = {
    "initial_speed": 0.5,
    "stop_latency_seconds": 0.3,
    "final_speed": 0.0,
    "stop_triggered": True,
    "post_stop_samples": 10,
}


def test_simulated_estop_passes_with_measured_stop() -> None:
    report = compare_systems(_observation(SAFE), _observation(SAFE), _config())
    assert report.outcome is Outcome.PASS


def test_simulated_estop_blocks_update_that_keeps_moving() -> None:
    moving = {**SAFE, "stop_latency_seconds": None, "final_speed": 0.5}
    report = compare_systems(_observation(SAFE), _observation(moving), _config())
    assert report.outcome is Outcome.BLOCK
    assert "simulated-estop-timeout" in {finding.code for finding in report.findings}


def test_simulated_estop_missing_evidence_cannot_pass() -> None:
    report = compare_systems(_observation(SAFE), _observation(None), _config())
    assert report.outcome is Outcome.BLOCK
    assert "simulated-estop-evidence-missing" in {
        finding.code for finding in report.findings
    }


def test_simulated_estop_requires_moving_baseline() -> None:
    stationary = {**SAFE, "initial_speed": 0.0}
    report = compare_systems(_observation(stationary), _observation(SAFE), _config())
    assert report.outcome is Outcome.ERROR


def test_simulated_estop_config_is_loaded() -> None:
    scenario = load_config(ROOT / ".fraeno.yml").validation.ros2_observer
    assert scenario is not None
    assert scenario.simulated_estop is not None
    assert scenario.simulated_estop.stop_topic == "/robot/e_stop"
    assert scenario.simulated_estop.maximum_stop_seconds == 0.6
