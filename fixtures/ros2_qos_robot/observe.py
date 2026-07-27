from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import time
from typing import Any

import rclpy
from diagnostic_msgs.msg import DiagnosticArray
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from std_msgs.msg import Float64


def fully_qualified(name: str, namespace: str) -> str:
    if namespace == "/":
        return f"/{name}"
    return f"{namespace.rstrip('/')}/{name}"


def policy_name(value: Any) -> str:
    name = getattr(value, "name", None)
    if name:
        return str(name).lower()
    return str(value).rsplit(".", maxsplit=1)[-1].lower()


def diagnostic_level(value: Any) -> int:
    if isinstance(value, (bytes, bytearray)):
        return value[0]
    return int(value)


def endpoint(info: Any) -> dict[str, Any]:
    qos = info.qos_profile
    return {
        "node": fully_qualified(info.node_name, info.node_namespace),
        "qos": {
            "reliability": policy_name(qos.reliability),
            "durability": policy_name(qos.durability),
            "history": policy_name(qos.history),
            "depth": int(qos.depth),
            "deadline_ns": None,
            "liveliness": policy_name(qos.liveliness),
            "lease_duration_ns": None,
        },
    }


class Probe(Node):
    def __init__(self) -> None:
        super().__init__("fraeno_observer")
        best_effort = QoSProfile(depth=100)
        best_effort.reliability = ReliabilityPolicy.BEST_EFFORT
        self.sensor_messages = 0
        self.command_messages = 0
        self.diagnostics: dict[str, int] = {}
        self.create_subscription(
            Float64,
            "/sensor/reading",
            self._on_sensor,
            best_effort,
        )
        self.create_subscription(
            Float64,
            "/robot/command",
            self._on_command,
            best_effort,
        )
        self.create_subscription(
            DiagnosticArray,
            "/diagnostics",
            self._on_diagnostics,
            10,
        )

    def _on_sensor(self, message: Float64) -> None:
        del message
        self.sensor_messages += 1

    def _on_command(self, message: Float64) -> None:
        del message
        self.command_messages += 1

    def _on_diagnostics(self, message: DiagnosticArray) -> None:
        for status in message.status:
            self.diagnostics[status.name] = diagnostic_level(status.level)

    def fingerprint(self) -> tuple[Any, ...]:
        nodes = tuple(sorted(self.get_node_names_and_namespaces()))
        topics = tuple(
            sorted(
                (name, tuple(types))
                for name, types in self.get_topic_names_and_types()
            )
        )
        services = tuple(
            sorted(
                (name, tuple(types))
                for name, types in self.get_service_names_and_types()
            )
        )
        return nodes, topics, services

    def graph(self, duration: float) -> dict[str, Any]:
        nodes = sorted(
            fully_qualified(name, namespace)
            for name, namespace in self.get_node_names_and_namespaces()
            if name != "fraeno_observer"
        )
        topic_types = dict(self.get_topic_names_and_types())
        topics = []
        for name in ("/sensor/reading", "/robot/command"):
            topics.append(
                {
                    "name": name,
                    "types": sorted(topic_types.get(name, [])),
                    "publishers": [
                        endpoint(value)
                        for value in self.get_publishers_info_by_topic(name)
                        if value.node_name != "fraeno_observer"
                    ],
                    "subscribers": [
                        endpoint(value)
                        for value in self.get_subscriptions_info_by_topic(name)
                        if value.node_name != "fraeno_observer"
                    ],
                    "rate_hz": (
                        self.sensor_messages / duration
                        if name == "/sensor/reading"
                        else self.command_messages / duration
                    ),
                    "message_count": (
                        self.sensor_messages
                        if name == "/sensor/reading"
                        else self.command_messages
                    ),
                }
            )
        services = [
            {"name": name, "types": sorted(types)}
            for name, types in self.get_service_names_and_types()
            if name == "/robot/health"
        ]
        return {
            "nodes": nodes,
            "topics": topics,
            "services": services,
            "actions": [],
        }


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    os.environ.setdefault("ROS_DOMAIN_ID", str(random.randint(10, 200)))
    processes = [
        subprocess.Popen(
            ["ros2", "run", "fraeno_ros_fixture", executable],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for executable in ("sensor_driver", "controller")
    ]
    rclpy.init()
    probe = Probe()
    graph_stable = False
    try:
        deadline = time.monotonic() + 10
        last_fingerprint: tuple[Any, ...] | None = None
        identical_samples = 0
        while time.monotonic() < deadline:
            rclpy.spin_once(probe, timeout_sec=0.2)
            current = probe.fingerprint()
            if current == last_fingerprint:
                identical_samples += 1
            else:
                last_fingerprint = current
                identical_samples = 1
            required_nodes = {"/sensor_driver", "/controller"}
            observed_nodes = {
                fully_qualified(name, namespace)
                for name, namespace in probe.get_node_names_and_namespaces()
            }
            if identical_samples >= 3 and required_nodes <= observed_nodes:
                graph_stable = True
                break

        started = time.monotonic()
        while time.monotonic() - started < 5:
            rclpy.spin_once(probe, timeout_sec=0.1)
        duration = time.monotonic() - started
        processes_alive = all(process.poll() is None for process in processes)
        healthy = (
            processes_alive
            and graph_stable
            and probe.sensor_messages >= 75
            and probe.command_messages >= 35
            and probe.diagnostics.get("controller", 2) == 0
        )
        result = {
            "schema_version": 1,
            "healthy": healthy,
            "graph_stable": graph_stable,
            "graph": probe.graph(duration),
            "transforms": [],
            "diagnostics": probe.diagnostics,
            "metadata": {
                "ros_distro": os.environ.get("ROS_DISTRO"),
                "rmw": os.environ.get("RMW_IMPLEMENTATION", "default"),
                "domain_id": int(os.environ["ROS_DOMAIN_ID"]),
                "observation_seconds": round(duration, 3),
            },
        }
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        probe.destroy_node()
        rclpy.shutdown()
        for process in processes:
            stop_process(process)


if __name__ == "__main__":
    raise SystemExit(main())
