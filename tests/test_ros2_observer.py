import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fraeno.config import ConfigError, Ros2ObserverConfig, load_config
from fraeno.validation.observation import SystemObservation
from fraeno.validation.ros2_observer import _Probe, _stop_process_group


class FakePolicy:
    RELIABLE = "reliable"
    BEST_EFFORT = "best_effort"
    TRANSIENT_LOCAL = "transient_local"
    VOLATILE = "volatile"


class FakeQos:
    def __init__(self, *, depth: int) -> None:
        self.depth = depth
        self.reliability = FakePolicy.RELIABLE
        self.durability = FakePolicy.VOLATILE
        self.history = "keep_last"
        self.deadline = SimpleNamespace(nanoseconds=0)
        self.liveliness = "automatic"
        self.liveliness_lease_duration = SimpleNamespace(nanoseconds=0)


class FakeNode:
    def __init__(self) -> None:
        self.callbacks: dict[tuple[str, str], Any] = {}

    def create_subscription(
        self,
        message_type: Any,
        topic: str,
        callback: Any,
        qos: Any,
    ) -> object:
        del qos
        self.callbacks[(topic, message_type)] = callback
        return object()

    def get_node_names_and_namespaces(self) -> list[tuple[str, str]]:
        return [
            ("fraeno_observer", "/"),
            ("controller", "/"),
            ("sensor", "/robot"),
        ]

    def get_topic_names_and_types(self) -> list[tuple[str, list[str]]]:
        return [
            ("/diagnostics", ["diagnostic_msgs/msg/DiagnosticArray"]),
            ("/sensor", ["example_msgs/msg/Reading"]),
            ("/tf_static", ["tf2_msgs/msg/TFMessage"]),
        ]

    def get_service_names_and_types(self) -> list[tuple[str, list[str]]]:
        return [
            ("/fraeno_observer/get_parameters", ["rcl_interfaces/srv/GetParameters"]),
            ("/health", ["std_srvs/srv/Trigger"]),
        ]

    def get_action_names_and_types(self) -> list[tuple[str, list[str]]]:
        return [("/move", ["example_interfaces/action/Fibonacci"])]

    def get_publishers_info_by_topic(self, name: str) -> list[Any]:
        del name
        return [self._endpoint("sensor", "/robot")]

    def get_subscriptions_info_by_topic(self, name: str) -> list[Any]:
        del name
        return [
            self._endpoint("controller", "/"),
            self._endpoint("fraeno_observer", "/"),
        ]

    @staticmethod
    def _endpoint(name: str, namespace: str) -> Any:
        return SimpleNamespace(
            node_name=name,
            node_namespace=namespace,
            qos_profile=FakeQos(depth=10),
        )


class FakeProcess:
    def poll(self) -> None:
        return None


def _probe() -> _Probe:
    config = Ros2ObserverConfig(
        launch_command=("ros2", "launch", "robot", "system.launch.py"),
        rate_topics=frozenset({"/sensor"}),
        diagnostics_topics=frozenset({"/diagnostics"}),
        transform_topics=frozenset({"/tf_static"}),
    )
    return _Probe(
        FakeNode(),
        spin_once=lambda *args, **kwargs: None,
        get_message=lambda type_name: type_name,
        qos_profile=FakeQos,
        reliability_policy=FakePolicy,
        durability_policy=FakePolicy,
        config=config,
    )


def test_generic_probe_discovers_graph_qos_rates_transforms_and_diagnostics() -> None:
    probe = _probe()
    probe.refresh_subscriptions()
    callbacks = probe.node.callbacks
    callbacks[("/sensor", "example_msgs/msg/Reading")](object())
    callbacks[("/diagnostics", "diagnostic_msgs/msg/DiagnosticArray")](
        SimpleNamespace(
            status=[
                SimpleNamespace(name="controller", level=0),
                SimpleNamespace(name="sensor", level=1),
            ]
        )
    )
    callbacks[("/tf_static", "tf2_msgs/msg/TFMessage")](
        SimpleNamespace(
            transforms=[
                SimpleNamespace(
                    header=SimpleNamespace(frame_id="/base_link"),
                    child_frame_id="/sensor_link",
                )
            ]
        )
    )
    probe._rate_samples["/sensor"] = [9.0, 11.0, 10.0]
    probe._rate_message_counts["/sensor"] = 30

    observation = probe.observation(
        graph_stable=True,
        process=FakeProcess(),  # type: ignore[arg-type]
    )

    assert observation.healthy
    assert observation.nodes == frozenset({"/controller", "/robot/sensor"})
    assert observation.services == {"/health": ("std_srvs/srv/Trigger",)}
    assert observation.actions == {
        "/move": ("example_interfaces/action/Fibonacci",)
    }
    assert observation.transforms == frozenset({"base_link->sensor_link"})
    assert observation.diagnostics == {"controller": 0, "sensor": 1}
    assert observation.topics["/sensor"].rate_hz == 10.0
    assert observation.topics["/sensor"].message_count == 30
    assert all(
        endpoint.node != "/fraeno_observer"
        for endpoint in observation.topics["/sensor"].subscribers
    )
    assert SystemObservation.from_dict(observation.to_dict()) == observation


def test_unreadable_configured_topic_is_incomplete_infrastructure_evidence() -> None:
    probe = _probe()
    probe._get_message = lambda type_name: (_ for _ in ()).throw(
        LookupError(type_name)
    )

    probe.refresh_subscriptions()
    observation = probe.observation(
        graph_stable=True,
        process=FakeProcess(),  # type: ignore[arg-type]
    )

    assert not observation.healthy
    assert observation.topics["/sensor"].rate_hz is None
    assert any(
        "Could not observe rate on /sensor" in error
        for error in observation.infrastructure_errors
    )


def test_configured_process_group_is_stopped() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )

    _stop_process_group(process, 1)

    assert process.poll() is not None


def test_ros2_observer_configuration_is_explicit_and_deterministic(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".fraeno.yml"
    path.write_text(
        """
version: 1
project:
  name: robot
validation:
  observe:
    command: [fraeno, observe-ros2, --config, .fraeno.yml]
    ros2:
      launch_command: [ros2, launch, robot, system.launch.py]
      warmup_seconds: 0
      graph_stabilization_timeout_seconds: 12
      graph_stabilization_interval_seconds: 0.5
      graph_stabilization_samples: 4
      sample_seconds: 3
      measurement_repetitions: 5
      diagnostics_topics: [/diagnostics]
      transform_topics: [/tf, /tf_static]
      shutdown_timeout_seconds: 2
  contract:
    required_topics: [/sensor]
    minimum_topic_rates_hz:
      /command: 4
"""
    )

    observer = load_config(path).validation.ros2_observer

    assert observer is not None
    assert observer.launch_command == (
        "ros2",
        "launch",
        "robot",
        "system.launch.py",
    )
    assert observer.warmup_seconds == 0
    assert observer.graph_stabilization_timeout_seconds == 12
    assert observer.graph_stabilization_interval_seconds == 0.5
    assert observer.graph_stabilization_samples == 4
    assert observer.sample_seconds == 3
    assert observer.measurement_repetitions == 5
    assert observer.rate_topics == frozenset({"/sensor", "/command"})
    assert observer.diagnostics_topics == frozenset({"/diagnostics"})
    assert observer.transform_topics == frozenset({"/tf", "/tf_static"})
    assert observer.shutdown_timeout_seconds == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("graph_stabilization_timeout_seconds", 0),
        ("graph_stabilization_interval_seconds", -1),
        ("graph_stabilization_samples", 0),
        ("sample_seconds", 0),
        ("measurement_repetitions", 0),
        ("shutdown_timeout_seconds", 0),
    ],
)
def test_ros2_observer_rejects_nondeterministic_timing(
    tmp_path: Path,
    field: str,
    value: int,
) -> None:
    path = tmp_path / ".fraeno.yml"
    path.write_text(
        f"""
version: 1
project:
  name: robot
validation:
  observe:
    command: [fraeno, observe-ros2]
    ros2:
      launch_command: [ros2, launch, robot, system.launch.py]
      {field}: {value}
"""
    )

    with pytest.raises(ConfigError, match=field):
        load_config(path)
