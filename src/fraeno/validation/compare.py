from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from fraeno.config import ValidationConfig
from fraeno.validation.observation import QosProfile, SystemObservation, TopicObservation


class Outcome(str, Enum):
    PASS = "pass"
    BLOCK = "block"
    ERROR = "error"


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    entity: str | None = None


@dataclass(frozen=True)
class ComparisonReport:
    outcome: Outcome
    findings: tuple[Finding, ...]
    baseline_summary: dict[str, int]
    candidate_summary: dict[str, int]
    validation_level: str = "L2"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "outcome": self.outcome.value,
            "validation_level": self.validation_level,
            "findings": [asdict(finding) for finding in self.findings],
            "baseline_summary": self.baseline_summary,
            "candidate_summary": self.candidate_summary,
        }


def compare_systems(
    baseline: SystemObservation,
    candidate: SystemObservation,
    config: ValidationConfig,
) -> ComparisonReport:
    baseline_errors = _infrastructure_findings(baseline, "baseline")
    baseline_errors.extend(_contract_findings(baseline, config))
    if not baseline.graph_stable:
        baseline_errors.append(
            Finding("baseline-unstable-graph", "The baseline ROS graph did not stabilize.")
        )
    if not baseline.healthy:
        baseline_errors.append(
            Finding("baseline-unhealthy", "The baseline system reported unhealthy.")
        )
    if baseline_errors:
        return ComparisonReport(
            outcome=Outcome.ERROR,
            findings=tuple(baseline_errors),
            baseline_summary=_summary(baseline),
            candidate_summary=_summary(candidate),
        )

    candidate_errors = _infrastructure_findings(candidate, "candidate")
    if not candidate.graph_stable:
        candidate_errors.append(
            Finding(
                "candidate-unstable-graph",
                "The candidate ROS graph did not stabilize.",
            )
        )
    if candidate_errors:
        return ComparisonReport(
            outcome=Outcome.ERROR,
            findings=tuple(candidate_errors),
            baseline_summary=_summary(baseline),
            candidate_summary=_summary(candidate),
        )

    findings: list[Finding] = []
    if not candidate.healthy:
        findings.append(
            Finding("candidate-unhealthy", "The candidate system reported unhealthy.")
        )
    findings.extend(_process_findings(candidate))
    findings.extend(_contract_findings(candidate, config))
    findings.extend(_compare_graph(baseline, candidate, config))
    findings.extend(_compare_diagnostics(baseline, candidate, config))

    return ComparisonReport(
        outcome=Outcome.BLOCK if findings else Outcome.PASS,
        findings=tuple(_deduplicate(findings)),
        baseline_summary=_summary(baseline),
        candidate_summary=_summary(candidate),
    )


def qos_compatible(offered: QosProfile, requested: QosProfile) -> bool:
    if requested.reliability == "reliable" and offered.reliability != "reliable":
        return False
    if (
        requested.durability in {"transient_local", "transient-local"}
        and offered.durability not in {"transient_local", "transient-local"}
    ):
        return False
    if (
        requested.deadline_ns is not None
        and offered.deadline_ns is not None
        and offered.deadline_ns > requested.deadline_ns
    ):
        return False
    if requested.liveliness not in {"unknown", "automatic"} and (
        offered.liveliness in {"unknown", "automatic"}
    ):
        return False
    return not (
        requested.lease_duration_ns is not None
        and offered.lease_duration_ns is not None
        and offered.lease_duration_ns > requested.lease_duration_ns
    )


def _contract_findings(
    observation: SystemObservation, config: ValidationConfig
) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(
        _missing_findings("node", config.required_nodes, observation.nodes)
    )
    findings.extend(
        _missing_findings("topic", config.required_topics, observation.topics)
    )
    findings.extend(
        _missing_findings("service", config.required_services, observation.services)
    )
    findings.extend(
        _missing_findings("action", config.required_actions, observation.actions)
    )
    findings.extend(
        _missing_findings(
            "transform",
            config.required_transforms,
            observation.transforms,
        )
    )
    findings.extend(
        _missing_findings(
            "diagnostic",
            config.required_diagnostics,
            observation.diagnostics,
        )
    )
    for topic, minimum_rate in config.minimum_topic_rates_hz.items():
        candidate = observation.topics.get(topic)
        if candidate is None:
            continue
        if candidate.rate_hz is None:
            findings.append(
                Finding(
                    "topic-rate-missing",
                    f"No rate measurement was recorded; required at least {minimum_rate:g} Hz.",
                    topic,
                )
            )
        elif candidate.rate_hz < minimum_rate:
            findings.append(
                Finding(
                    "topic-rate-below-minimum",
                    f"Measured {candidate.rate_hz:g} Hz; required at least {minimum_rate:g} Hz.",
                    topic,
                )
            )
    return findings


def _infrastructure_findings(
    observation: SystemObservation,
    phase: str,
) -> list[Finding]:
    return [
        Finding(
            f"{phase}-infrastructure-error",
            error,
        )
        for error in observation.infrastructure_errors
    ]


def _process_findings(observation: SystemObservation) -> list[Finding]:
    return [
        Finding(
            "process-exited",
            (
                "Configured system process exited before observation completed"
                if process.exit_code is None
                else "Configured system process exited before observation completed "
                f"with code {process.exit_code}"
            ),
            " ".join(process.command),
        )
        for process in observation.processes
        if not process.running
    ]


def _missing_findings(
    entity_type: str, required: set[str] | frozenset[str], observed: Any
) -> list[Finding]:
    available = set(observed)
    return [
        Finding(
            f"required-{entity_type}-missing",
            f"Required {entity_type} is missing.",
            name,
        )
        for name in sorted(required - available)
    ]


def _compare_graph(
    baseline: SystemObservation,
    candidate: SystemObservation,
    config: ValidationConfig,
) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(
        _removed_findings("node", baseline.nodes, candidate.nodes, config)
    )
    findings.extend(
        _removed_findings("topic", baseline.topics, candidate.topics, config)
    )
    findings.extend(
        _removed_findings("service", baseline.services, candidate.services, config)
    )
    findings.extend(
        _removed_findings("action", baseline.actions, candidate.actions, config)
    )
    findings.extend(
        _removed_findings(
            "transform",
            baseline.transforms,
            candidate.transforms,
            config,
        )
    )

    for name in sorted(baseline.topics.keys() & candidate.topics.keys()):
        before = baseline.topics.get(name)
        after = candidate.topics.get(name)
        if before is None or after is None:  # pragma: no cover - guarded by keys
            continue
        if before.types != after.types:
            findings.append(
                Finding(
                    "topic-type-changed",
                    f"Topic types changed from {before.types!r} to {after.types!r}.",
                    name,
                )
            )
        if len(after.publishers) < len(before.publishers):
            findings.append(
                Finding(
                    "topic-publisher-count-regressed",
                    f"Publisher count fell from {len(before.publishers)} to "
                    f"{len(after.publishers)}.",
                    name,
                )
            )
        if len(after.subscribers) < len(before.subscribers):
            findings.append(
                Finding(
                    "topic-subscriber-count-regressed",
                    f"Subscriber count fell from {len(before.subscribers)} to "
                    f"{len(after.subscribers)}.",
                    name,
                )
            )
        if after.publishers and after.subscribers and not _has_compatible_path(after):
            findings.append(
                Finding(
                    "topic-qos-incompatible",
                    "No publisher-to-subscriber QoS-compatible path remains.",
                    name,
                )
            )
        _compare_topic_rate(name, before, after, config, findings)

    findings.extend(
        _typed_entity_changes("service", baseline.services, candidate.services)
    )
    findings.extend(
        _typed_entity_changes("action", baseline.actions, candidate.actions)
    )
    return findings


def _removed_findings(
    entity_type: str,
    baseline: Any,
    candidate: Any,
    config: ValidationConfig,
) -> list[Finding]:
    missing = set(baseline) - set(candidate)
    return [
        Finding(
            f"{entity_type}-removed",
            f"A baseline {entity_type} is missing from the candidate.",
            name,
        )
        for name in sorted(missing)
        if not _missing_allowed(config, entity_type, name)
    ]


def _typed_entity_changes(
    entity_type: str,
    baseline: dict[str, tuple[str, ...]],
    candidate: dict[str, tuple[str, ...]],
) -> list[Finding]:
    return [
        Finding(
            f"{entity_type}-type-changed",
            f"{entity_type.title()} types changed from {baseline[name]!r} "
            f"to {candidate[name]!r}.",
            name,
        )
        for name in sorted(baseline.keys() & candidate.keys())
        if baseline[name] != candidate[name]
    ]


def _missing_allowed(
    config: ValidationConfig,
    entity_type: str,
    name: str,
) -> bool:
    allowed = config.allowed_missing_baseline_entities
    return name in allowed or f"{entity_type}:{name}" in allowed


def _has_compatible_path(topic: TopicObservation) -> bool:
    return any(
        qos_compatible(publisher.qos, subscriber.qos)
        for publisher in topic.publishers
        for subscriber in topic.subscribers
    )


def _compare_topic_rate(
    name: str,
    baseline: TopicObservation,
    candidate: TopicObservation,
    config: ValidationConfig,
    findings: list[Finding],
) -> None:
    if (
        baseline.rate_hz is None
        or candidate.rate_hz is None
        or baseline.rate_hz <= 0
    ):
        return
    minimum_ratio = 1.0 - config.maximum_topic_rate_regression_percent / 100.0
    actual_ratio = candidate.rate_hz / baseline.rate_hz
    if actual_ratio < minimum_ratio:
        findings.append(
            Finding(
                "topic-rate-regressed",
                f"Rate fell from {baseline.rate_hz:g} Hz to {candidate.rate_hz:g} Hz "
                f"({actual_ratio:.1%} of baseline; minimum {minimum_ratio:.1%}).",
                name,
            )
        )


def _compare_diagnostics(
    baseline: SystemObservation,
    candidate: SystemObservation,
    config: ValidationConfig,
) -> list[Finding]:
    findings: list[Finding] = []
    for component, before_level in baseline.diagnostics.items():
        after_level = candidate.diagnostics.get(component)
        if after_level is None:
            if not _missing_allowed(config, "diagnostic", component):
                findings.append(
                    Finding(
                        "diagnostic-missing",
                        "A baseline diagnostic component is missing.",
                        component,
                    )
                )
        elif after_level > before_level:
            findings.append(
                Finding(
                    "diagnostic-regressed",
                    f"Diagnostic level worsened from {before_level} to {after_level}.",
                    component,
                )
            )
    return findings


def _summary(observation: SystemObservation) -> dict[str, int]:
    return {
        "nodes": len(observation.nodes),
        "topics": len(observation.topics),
        "services": len(observation.services),
        "actions": len(observation.actions),
        "transforms": len(observation.transforms),
        "diagnostics": len(observation.diagnostics),
    }


def _deduplicate(findings: list[Finding]) -> list[Finding]:
    unique: dict[tuple[str, str | None], Finding] = {}
    for finding in findings:
        unique[(finding.code, finding.entity)] = finding
    return list(unique.values())
