from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class ObservationError(ValueError):
    pass


@dataclass(frozen=True)
class QosProfile:
    reliability: str = "unknown"
    durability: str = "unknown"
    history: str = "unknown"
    depth: int | None = None
    deadline_ns: int | None = None
    liveliness: str = "unknown"
    lease_duration_ns: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> QosProfile:
        return cls(
            reliability=str(raw.get("reliability", "unknown")).lower(),
            durability=str(raw.get("durability", "unknown")).lower(),
            history=str(raw.get("history", "unknown")).lower(),
            depth=int(raw["depth"]) if raw.get("depth") is not None else None,
            deadline_ns=(
                int(raw["deadline_ns"]) if raw.get("deadline_ns") is not None else None
            ),
            liveliness=str(raw.get("liveliness", "unknown")).lower(),
            lease_duration_ns=(
                int(raw["lease_duration_ns"])
                if raw.get("lease_duration_ns") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class Endpoint:
    node: str
    qos: QosProfile

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Endpoint:
        qos = raw.get("qos", {})
        if not isinstance(qos, dict):
            raise ObservationError("endpoint.qos must be an object")
        return cls(node=str(raw.get("node", "")), qos=QosProfile.from_dict(qos))


@dataclass(frozen=True)
class TopicObservation:
    name: str
    types: tuple[str, ...]
    publishers: tuple[Endpoint, ...] = ()
    subscribers: tuple[Endpoint, ...] = ()
    rate_hz: float | None = None
    message_count: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TopicObservation:
        types = raw.get("types", [])
        if not isinstance(types, list):
            raise ObservationError("topic.types must be a list")
        publishers = raw.get("publishers", [])
        subscribers = raw.get("subscribers", raw.get("subscriptions", []))
        if not isinstance(publishers, list) or not isinstance(subscribers, list):
            raise ObservationError("topic endpoints must be lists")
        return cls(
            name=str(raw.get("name", "")),
            types=tuple(sorted(str(item) for item in types)),
            publishers=tuple(Endpoint.from_dict(item) for item in publishers),
            subscribers=tuple(Endpoint.from_dict(item) for item in subscribers),
            rate_hz=float(raw["rate_hz"]) if raw.get("rate_hz") is not None else None,
            message_count=(
                int(raw["message_count"]) if raw.get("message_count") is not None else None
            ),
        )


@dataclass(frozen=True)
class ProcessObservation:
    command: tuple[str, ...]
    running: bool
    exit_code: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProcessObservation:
        command = raw.get("command", [])
        if not isinstance(command, list) or not all(
            isinstance(part, str) for part in command
        ):
            raise ObservationError("process.command must be a list of strings")
        return cls(
            command=tuple(command),
            running=bool(raw.get("running", False)),
            exit_code=(
                int(raw["exit_code"]) if raw.get("exit_code") is not None else None
            ),
        )


@dataclass(frozen=True)
class SystemObservation:
    healthy: bool
    graph_stable: bool
    nodes: frozenset[str]
    topics: dict[str, TopicObservation]
    services: dict[str, tuple[str, ...]]
    actions: dict[str, tuple[str, ...]]
    transforms: frozenset[str]
    diagnostics: dict[str, int]
    processes: tuple[ProcessObservation, ...] = ()
    infrastructure_errors: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SystemObservation:
        if raw.get("schema_version") not in {1, "1"}:
            raise ObservationError("unsupported or missing observation schema_version")
        graph = raw.get("graph", {})
        if not isinstance(graph, dict):
            raise ObservationError("graph must be an object")

        topic_values = graph.get("topics", [])
        if not isinstance(topic_values, list):
            raise ObservationError("graph.topics must be a list")
        topics = {
            topic.name: topic
            for topic in (TopicObservation.from_dict(item) for item in topic_values)
            if topic.name
        }

        process_values = raw.get("processes", [])
        if not isinstance(process_values, list) or not all(
            isinstance(item, dict) for item in process_values
        ):
            raise ObservationError("processes must be a list of objects")
        evidence = raw.get("evidence", {})
        if not isinstance(evidence, dict):
            raise ObservationError("evidence must be an object")
        evidence_errors = evidence.get("errors", [])
        if not isinstance(evidence_errors, list) or not all(
            isinstance(item, str) and item for item in evidence_errors
        ):
            raise ObservationError("evidence.errors must be a list of strings")
        if evidence.get("complete") is False and not evidence_errors:
            evidence_errors = ["Observer marked infrastructure evidence incomplete."]

        return cls(
            healthy=bool(raw.get("healthy", False)),
            graph_stable=bool(raw.get("graph_stable", False)),
            nodes=frozenset(_entity_names(graph.get("nodes", []))),
            topics=topics,
            services=_typed_entities(graph.get("services", [])),
            actions=_typed_entities(graph.get("actions", [])),
            transforms=frozenset(str(item) for item in raw.get("transforms", [])),
            diagnostics={
                str(name): int(level)
                for name, level in _dictionary(raw.get("diagnostics", {})).items()
            },
            processes=tuple(
                ProcessObservation.from_dict(item) for item in process_values
            ),
            infrastructure_errors=tuple(evidence_errors),
            metadata=_dictionary(raw.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "healthy": self.healthy,
            "graph_stable": self.graph_stable,
            "graph": {
                "nodes": sorted(self.nodes),
                "topics": [
                    {
                        "name": topic.name,
                        "types": list(topic.types),
                        "publishers": [
                            {"node": endpoint.node, "qos": asdict(endpoint.qos)}
                            for endpoint in topic.publishers
                        ],
                        "subscribers": [
                            {"node": endpoint.node, "qos": asdict(endpoint.qos)}
                            for endpoint in topic.subscribers
                        ],
                        "rate_hz": topic.rate_hz,
                        "message_count": topic.message_count,
                    }
                    for topic in sorted(self.topics.values(), key=lambda item: item.name)
                ],
                "services": [
                    {"name": name, "types": list(types)}
                    for name, types in sorted(self.services.items())
                ],
                "actions": [
                    {"name": name, "types": list(types)}
                    for name, types in sorted(self.actions.items())
                ],
            },
            "transforms": sorted(self.transforms),
            "diagnostics": dict(sorted(self.diagnostics.items())),
            "processes": [
                {
                    "command": list(process.command),
                    "running": process.running,
                    "exit_code": process.exit_code,
                }
                for process in self.processes
            ],
            "evidence": {
                "complete": not self.infrastructure_errors,
                "errors": list(self.infrastructure_errors),
            },
            "metadata": self.metadata,
        }


def _dictionary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ObservationError("expected an object")
    return value


def _entity_names(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise ObservationError("graph entity list must be a list")
    return [
        str(item.get("name", "")) if isinstance(item, dict) else str(item)
        for item in raw
        if (isinstance(item, str) and item)
        or (isinstance(item, dict) and item.get("name"))
    ]


def _typed_entities(raw: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw, list):
        raise ObservationError("typed graph entities must be a list")
    result: dict[str, tuple[str, ...]] = {}
    for item in raw:
        if isinstance(item, str):
            result[item] = ()
            continue
        if not isinstance(item, dict) or not item.get("name"):
            continue
        types = item.get("types", [])
        if not isinstance(types, list):
            raise ObservationError("entity types must be a list")
        result[str(item["name"])] = tuple(sorted(str(value) for value in types))
    return result
