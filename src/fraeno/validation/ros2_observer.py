from __future__ import annotations

import importlib
import os
import signal
import statistics
import subprocess
import time
from collections.abc import Callable
from typing import Any, cast

from fraeno.config import Ros2ObserverConfig
from fraeno.validation.observation import (
    Endpoint,
    ProcessObservation,
    QosProfile,
    SystemObservation,
    TopicObservation,
)


class Ros2ObservationError(RuntimeError):
    pass


def _fully_qualified(name: str, namespace: str) -> str:
    if namespace == "/":
        return f"/{name}"
    return f"{namespace.rstrip('/')}/{name}"


def _policy_name(value: Any) -> str:
    name = getattr(value, "name", None)
    if name:
        return str(name).lower()
    return str(value).rsplit(".", maxsplit=1)[-1].lower()


def _duration_ns(value: Any) -> int | None:
    nanoseconds = getattr(value, "nanoseconds", None)
    if nanoseconds is None:
        return None
    result = int(nanoseconds)
    return result if 0 <= result < 2**63 - 1 else None


def _endpoint(info: Any) -> Endpoint:
    qos = info.qos_profile
    return Endpoint(
        node=_fully_qualified(str(info.node_name), str(info.node_namespace)),
        qos=QosProfile(
            reliability=_policy_name(qos.reliability),
            durability=_policy_name(qos.durability),
            history=_policy_name(qos.history),
            depth=int(qos.depth),
            deadline_ns=_duration_ns(qos.deadline),
            liveliness=_policy_name(qos.liveliness),
            lease_duration_ns=_duration_ns(qos.liveliness_lease_duration),
        ),
    )


def _diagnostic_level(value: Any) -> int:
    if isinstance(value, (bytes, bytearray)):
        return int(value[0])
    return int(value)


class _Probe:
    def __init__(
        self,
        node: Any,
        *,
        spin_once: Callable[..., Any],
        get_message: Callable[[str], Any],
        qos_profile: Callable[..., Any],
        reliability_policy: Any,
        durability_policy: Any,
        config: Ros2ObserverConfig,
    ) -> None:
        self.node = node
        self._spin_once = spin_once
        self._get_message = get_message
        self._qos_profile = qos_profile
        self._reliability_policy = reliability_policy
        self._durability_policy = durability_policy
        self._config = config
        self._subscriptions: list[Any] = []
        self._subscription_keys: set[tuple[str, str, str]] = set()
        self._rate_counts = {topic: 0 for topic in config.rate_topics}
        self._rate_samples: dict[str, list[float]] = {
            topic: [] for topic in config.rate_topics
        }
        self._rate_message_counts = {topic: 0 for topic in config.rate_topics}
        self._unmeasurable_rate_topics: set[str] = set()
        self.transforms: set[str] = set()
        self.diagnostics: dict[str, int] = {}
        self.infrastructure_errors: list[str] = []

    def spin_for(self, duration_seconds: float) -> None:
        deadline = time.monotonic() + duration_seconds
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            self._spin_once(self.node, timeout_sec=min(0.1, max(remaining, 0.0)))
            self.refresh_subscriptions()

    def stabilize_graph(self) -> bool:
        deadline = time.monotonic() + self._config.graph_stabilization_timeout_seconds
        previous: tuple[Any, ...] | None = None
        identical_samples = 0
        while time.monotonic() < deadline:
            self._spin_once(
                self.node,
                timeout_sec=self._config.graph_stabilization_interval_seconds,
            )
            self.refresh_subscriptions()
            current = self.fingerprint()
            if current == previous:
                identical_samples += 1
            else:
                previous = current
                identical_samples = 1
            if identical_samples >= self._config.graph_stabilization_samples:
                return True
        return False

    def measure_rates(self) -> None:
        for _ in range(self._config.measurement_repetitions):
            for topic in self._rate_counts:
                self._rate_counts[topic] = 0
            started = time.monotonic()
            self.spin_for(self._config.sample_seconds)
            elapsed = time.monotonic() - started
            for topic, count in self._rate_counts.items():
                self._rate_samples[topic].append(count / elapsed)
                self._rate_message_counts[topic] += count

    def refresh_subscriptions(self) -> None:
        topic_types = dict(self.node.get_topic_names_and_types())
        for topic in sorted(self._config.rate_topics):
            self._subscribe(topic, topic_types.get(topic, []), "rate")
        for topic in sorted(self._config.diagnostics_topics):
            self._subscribe(topic, topic_types.get(topic, []), "diagnostics")
        for topic in sorted(self._config.transform_topics):
            self._subscribe(topic, topic_types.get(topic, []), "transforms")

    def _subscribe(self, topic: str, type_names: list[str], purpose: str) -> None:
        for type_name in sorted(type_names):
            key = (purpose, topic, type_name)
            if key in self._subscription_keys:
                continue
            self._subscription_keys.add(key)
            try:
                message_type = self._get_message(type_name)
                callback = self._callback(purpose, topic)
                subscription = self.node.create_subscription(
                    message_type,
                    topic,
                    callback,
                    self._subscription_qos(topic, purpose),
                )
                self._subscriptions.append(subscription)
            except Exception as error:
                if purpose == "rate":
                    self._unmeasurable_rate_topics.add(topic)
                self.infrastructure_errors.append(
                    f"Could not observe {purpose} on {topic} ({type_name}): {error}"
                )

    def _subscription_qos(self, topic: str, purpose: str) -> Any:
        qos = self._qos_profile(depth=100)
        if purpose == "transforms" and topic == "/tf_static":
            qos.reliability = self._reliability_policy.RELIABLE
            qos.durability = self._durability_policy.TRANSIENT_LOCAL
        else:
            qos.reliability = self._reliability_policy.BEST_EFFORT
            qos.durability = self._durability_policy.VOLATILE
        return qos

    def _callback(self, purpose: str, topic: str) -> Callable[[Any], None]:
        if purpose == "rate":
            return lambda message: self._record_rate(topic, message)
        if purpose == "diagnostics":
            return self._record_diagnostics
        return self._record_transforms

    def _record_rate(self, topic: str, message: Any) -> None:
        del message
        self._rate_counts[topic] += 1

    def _record_diagnostics(self, message: Any) -> None:
        for status in getattr(message, "status", []):
            name = str(getattr(status, "name", ""))
            if not name:
                continue
            level = _diagnostic_level(getattr(status, "level", 3))
            self.diagnostics[name] = max(self.diagnostics.get(name, 0), level)

    def _record_transforms(self, message: Any) -> None:
        for transform in getattr(message, "transforms", []):
            header = getattr(transform, "header", None)
            parent = str(getattr(header, "frame_id", "")).lstrip("/")
            child = str(getattr(transform, "child_frame_id", "")).lstrip("/")
            if parent and child:
                self.transforms.add(f"{parent}->{child}")

    def fingerprint(self) -> tuple[Any, ...]:
        return (
            tuple(sorted(self._nodes())),
            tuple(
                sorted(
                    (
                        name,
                        tuple(sorted(types)),
                        tuple(
                            sorted(
                                (
                                    _endpoint(info)
                                    for info in (
                                        self.node.get_publishers_info_by_topic(name)
                                    )
                                    if str(info.node_name) != "fraeno_observer"
                                ),
                                key=lambda value: (value.node, repr(value.qos)),
                            )
                        ),
                        tuple(
                            sorted(
                                (
                                    _endpoint(info)
                                    for info in (
                                        self.node.get_subscriptions_info_by_topic(name)
                                    )
                                    if str(info.node_name) != "fraeno_observer"
                                ),
                                key=lambda value: (value.node, repr(value.qos)),
                            )
                        ),
                    )
                    for name, types in self.node.get_topic_names_and_types()
                )
            ),
            tuple(
                sorted(
                    (name, tuple(sorted(types)))
                    for name, types in self._services().items()
                )
            ),
            tuple(
                sorted(
                    (name, tuple(sorted(types)))
                    for name, types in self._actions().items()
                )
            ),
        )

    def observation(
        self,
        *,
        graph_stable: bool,
        process: subprocess.Popen[bytes],
    ) -> SystemObservation:
        topic_types = dict(self.node.get_topic_names_and_types())
        topics: dict[str, TopicObservation] = {}
        for name in sorted(topic_types):
            publishers = tuple(
                sorted(
                    (
                        _endpoint(info)
                        for info in self.node.get_publishers_info_by_topic(name)
                        if str(info.node_name) != "fraeno_observer"
                    ),
                    key=lambda value: (value.node, repr(value.qos)),
                )
            )
            subscribers = tuple(
                sorted(
                    (
                        _endpoint(info)
                        for info in self.node.get_subscriptions_info_by_topic(name)
                        if str(info.node_name) != "fraeno_observer"
                    ),
                    key=lambda value: (value.node, repr(value.qos)),
                )
            )
            rate_hz = None
            message_count = None
            if name in self._config.rate_topics:
                message_count = self._rate_message_counts[name]
                if (
                    name not in self._unmeasurable_rate_topics
                    and self._rate_samples[name]
                ):
                    rate_hz = statistics.median(self._rate_samples[name])
            topics[name] = TopicObservation(
                name=name,
                types=tuple(sorted(topic_types[name])),
                publishers=publishers,
                subscribers=subscribers,
                rate_hz=rate_hz,
                message_count=message_count,
            )

        process_running = process.poll() is None
        infrastructure_errors = tuple(dict.fromkeys(self.infrastructure_errors))
        return SystemObservation(
            healthy=process_running and graph_stable and not infrastructure_errors,
            graph_stable=graph_stable,
            nodes=frozenset(self._nodes()),
            topics=topics,
            services=self._services(),
            actions=self._actions(),
            transforms=frozenset(self.transforms),
            diagnostics=dict(self.diagnostics),
            processes=(
                ProcessObservation(
                    command=self._config.launch_command,
                    running=process_running,
                    exit_code=process.poll(),
                ),
            ),
            infrastructure_errors=infrastructure_errors,
            metadata={
                "ros_distro": os.environ.get("ROS_DISTRO"),
                "rmw": os.environ.get("RMW_IMPLEMENTATION", "default"),
                "domain_id": int(os.environ.get("ROS_DOMAIN_ID", "0")),
                "warmup_seconds": self._config.warmup_seconds,
                "graph_stabilization_timeout_seconds": (
                    self._config.graph_stabilization_timeout_seconds
                ),
                "sample_seconds": self._config.sample_seconds,
                "measurement_repetitions": self._config.measurement_repetitions,
                "rate_samples_hz": {
                    topic: [round(value, 6) for value in samples]
                    for topic, samples in sorted(self._rate_samples.items())
                },
            },
        )

    def _nodes(self) -> set[str]:
        return {
            _fully_qualified(str(name), str(namespace))
            for name, namespace in self.node.get_node_names_and_namespaces()
            if str(name) != "fraeno_observer"
        }

    def _services(self) -> dict[str, tuple[str, ...]]:
        return {
            str(name): tuple(sorted(str(value) for value in types))
            for name, types in self.node.get_service_names_and_types()
            if not str(name).startswith("/fraeno_observer/")
        }

    def _actions(self) -> dict[str, tuple[str, ...]]:
        method = getattr(self.node, "get_action_names_and_types", None)
        if method is None:
            action_module = importlib.import_module("rclpy.action")
            method = action_module.get_action_names_and_types
            values = method(self.node)
        else:
            values = method()
        return {
            str(name): tuple(sorted(str(value) for value in types))
            for name, types in cast(list[tuple[str, list[str]]], values)
        }


def observe_ros2(config: Ros2ObserverConfig) -> SystemObservation:
    try:
        rclpy = importlib.import_module("rclpy")
        node_module = importlib.import_module("rclpy.node")
        qos_module = importlib.import_module("rclpy.qos")
        utilities_module = importlib.import_module("rosidl_runtime_py.utilities")
    except ImportError as error:
        raise Ros2ObservationError(
            "ROS 2 Python libraries are not available in this environment"
        ) from error

    rclpy.init()
    try:
        try:
            node = node_module.Node(
                "fraeno_observer",
                enable_rosout=False,
                start_parameter_services=False,
            )
        except TypeError:
            node = node_module.Node("fraeno_observer", enable_rosout=False)
        probe = _Probe(
            node,
            spin_once=rclpy.spin_once,
            get_message=utilities_module.get_message,
            qos_profile=qos_module.QoSProfile,
            reliability_policy=qos_module.ReliabilityPolicy,
            durability_policy=qos_module.DurabilityPolicy,
            config=config,
        )
        try:
            process = subprocess.Popen(
                config.launch_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            process_group = os.getpgid(process.pid)
        except OSError as error:
            node.destroy_node()
            raise Ros2ObservationError(
                f"Could not launch the configured robot system: {error}"
            ) from error
        try:
            probe.spin_for(config.warmup_seconds)
            graph_stable = probe.stabilize_graph()
            probe.measure_rates()
            return probe.observation(graph_stable=graph_stable, process=process)
        finally:
            _stop_process_group(
                process,
                config.shutdown_timeout_seconds,
                process_group=process_group,
            )
            node.destroy_node()
    finally:
        rclpy.shutdown()


def _stop_process_group(
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
    *,
    process_group: int | None = None,
) -> None:
    if process_group is None:
        try:
            process_group = os.getpgid(process.pid)
        except ProcessLookupError:
            process.wait()
            return
    for signal_value in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process_group, signal_value)
        except ProcessLookupError:
            process.wait()
            return
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            process.poll()
            if not _process_group_exists(process_group):
                process.wait()
                return
            time.sleep(0.05)
    process.wait()


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
