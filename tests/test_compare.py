import json
from dataclasses import replace
from pathlib import Path

from fraeno.config import load_config
from fraeno.validation.compare import Outcome, compare_systems, qos_compatible
from fraeno.validation.observation import (
    Endpoint,
    ProcessObservation,
    QosProfile,
    SystemObservation,
    TopicObservation,
)

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


def _complete_observation() -> SystemObservation:
    endpoint = Endpoint("/controller", QosProfile(reliability="reliable"))
    return SystemObservation(
        healthy=True,
        graph_stable=True,
        nodes=frozenset({"/controller", "/optional_node"}),
        topics={
            "/stream": TopicObservation(
                name="/stream",
                types=("example/msg/Reading",),
                publishers=(endpoint,),
                subscribers=(endpoint,),
                rate_hz=20.0,
                message_count=60,
            ),
            "/optional_topic": TopicObservation(
                name="/optional_topic",
                types=("example/msg/Optional",),
            ),
        },
        services={
            "/health": ("example/srv/Health",),
            "/optional_service": ("example/srv/Optional",),
        },
        actions={
            "/move": ("example/action/Move",),
            "/optional_action": ("example/action/Optional",),
        },
        transforms=frozenset(
            {"base_link->sensor_link", "base_link->optional_link"}
        ),
        diagnostics={"controller": 0, "optional_diagnostic": 0},
        processes=(ProcessObservation(("ros2", "launch"), True),),
    )


def test_complete_graph_regressions_are_compared_not_only_required_topics() -> None:
    baseline = _complete_observation()
    candidate = replace(
        baseline,
        nodes=frozenset({"/controller"}),
        topics={
            "/stream": replace(
                baseline.topics["/stream"],
                types=("example/msg/Changed",),
                rate_hz=10.0,
            )
        },
        services={"/health": ("example/srv/Changed",)},
        actions={"/move": ("example/action/Changed",)},
        transforms=frozenset({"base_link->sensor_link"}),
        diagnostics={"controller": 1},
    )
    config = replace(
        load_config(FIXTURE / "fraeno.yml").validation,
        required_nodes=frozenset(),
        required_topics=frozenset(),
        required_services=frozenset(),
        required_actions=frozenset(),
        minimum_topic_rates_hz={},
    )

    report = compare_systems(baseline, candidate, config)

    assert report.outcome is Outcome.BLOCK
    assert {finding.code for finding in report.findings} == {
        "action-removed",
        "action-type-changed",
        "diagnostic-missing",
        "diagnostic-regressed",
        "node-removed",
        "service-removed",
        "service-type-changed",
        "topic-removed",
        "topic-rate-regressed",
        "topic-type-changed",
        "transform-removed",
    }


def test_allowed_missing_baseline_entities_are_applied_by_entity_type() -> None:
    baseline = _complete_observation()
    candidate = replace(
        baseline,
        nodes=frozenset({"/controller"}),
        topics={"/stream": baseline.topics["/stream"]},
        services={"/health": baseline.services["/health"]},
        actions={"/move": baseline.actions["/move"]},
        transforms=frozenset({"base_link->sensor_link"}),
        diagnostics={"controller": 0},
    )
    config = replace(
        load_config(FIXTURE / "fraeno.yml").validation,
        required_nodes=frozenset(),
        required_topics=frozenset(),
        required_services=frozenset(),
        required_actions=frozenset(),
        minimum_topic_rates_hz={},
        allowed_missing_baseline_entities=frozenset(
            {
                "node:/optional_node",
                "topic:/optional_topic",
                "service:/optional_service",
                "action:/optional_action",
                "transform:base_link->optional_link",
                "diagnostic:optional_diagnostic",
            }
        ),
    )

    report = compare_systems(baseline, candidate, config)

    assert report.outcome is Outcome.PASS
    assert report.findings == ()


def test_required_transforms_and_diagnostics_are_contract_requirements() -> None:
    observation_without_evidence = replace(
        _complete_observation(),
        transforms=frozenset(),
        diagnostics={},
    )
    config = replace(
        load_config(FIXTURE / "fraeno.yml").validation,
        required_nodes=frozenset(),
        required_topics=frozenset(),
        required_services=frozenset(),
        required_actions=frozenset(),
        required_transforms=frozenset({"base_link->sensor_link"}),
        required_diagnostics=frozenset({"controller"}),
        minimum_topic_rates_hz={},
    )

    report = compare_systems(
        observation_without_evidence,
        _complete_observation(),
        config,
    )

    assert report.outcome is Outcome.ERROR
    assert {finding.code for finding in report.findings} == {
        "required-diagnostic-missing",
        "required-transform-missing",
    }


def test_missing_infrastructure_evidence_is_an_error_not_a_regression() -> None:
    baseline = _complete_observation()
    candidate = replace(
        baseline,
        infrastructure_errors=("Could not load example/msg/Reading",),
    )
    config = replace(
        load_config(FIXTURE / "fraeno.yml").validation,
        required_nodes=frozenset(),
        required_topics=frozenset(),
        required_services=frozenset(),
        required_actions=frozenset(),
        minimum_topic_rates_hz={},
    )

    report = compare_systems(baseline, candidate, config)

    assert report.outcome is Outcome.ERROR
    assert [finding.code for finding in report.findings] == [
        "candidate-infrastructure-error"
    ]


def test_exited_configured_process_blocks_candidate() -> None:
    baseline = _complete_observation()
    candidate = replace(
        baseline,
        healthy=False,
        processes=(ProcessObservation(("ros2", "launch"), False, 7),),
    )
    config = replace(
        load_config(FIXTURE / "fraeno.yml").validation,
        required_nodes=frozenset(),
        required_topics=frozenset(),
        required_services=frozenset(),
        required_actions=frozenset(),
        minimum_topic_rates_hz={},
    )

    report = compare_systems(baseline, candidate, config)

    assert report.outcome is Outcome.BLOCK
    assert {finding.code for finding in report.findings} >= {
        "candidate-unhealthy",
        "process-exited",
    }


def test_qos_is_compared_for_observed_topics_without_required_topic_list() -> None:
    baseline = _complete_observation()
    incompatible_subscriber = Endpoint(
        "/controller",
        QosProfile(reliability="reliable"),
    )
    best_effort_publisher = Endpoint(
        "/sensor",
        QosProfile(reliability="best_effort"),
    )
    candidate = replace(
        baseline,
        topics={
            **baseline.topics,
            "/stream": replace(
                baseline.topics["/stream"],
                publishers=(best_effort_publisher,),
                subscribers=(incompatible_subscriber,),
            ),
        },
    )
    config = replace(
        load_config(FIXTURE / "fraeno.yml").validation,
        required_nodes=frozenset(),
        required_topics=frozenset(),
        required_services=frozenset(),
        required_actions=frozenset(),
        minimum_topic_rates_hz={},
    )

    report = compare_systems(baseline, candidate, config)

    assert report.outcome is Outcome.BLOCK
    assert any(
        finding.code == "topic-qos-incompatible"
        and finding.entity == "/stream"
        for finding in report.findings
    )


def test_unstable_candidate_graph_is_infrastructure_error() -> None:
    baseline = _complete_observation()
    candidate = replace(baseline, graph_stable=False)
    config = replace(
        load_config(FIXTURE / "fraeno.yml").validation,
        required_nodes=frozenset(),
        required_topics=frozenset(),
        required_services=frozenset(),
        required_actions=frozenset(),
        minimum_topic_rates_hz={},
    )

    report = compare_systems(baseline, candidate, config)

    assert report.outcome is Outcome.ERROR
    assert [finding.code for finding in report.findings] == [
        "candidate-unstable-graph"
    ]
