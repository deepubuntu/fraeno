from __future__ import annotations

from pathlib import Path

import rclpy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Float64


class SensorDriver(Node):
    def __init__(self) -> None:
        super().__init__("sensor_driver")
        version = Path("fraeno-fixture-version.txt").read_text().strip()
        reliability = (
            ReliabilityPolicy.BEST_EFFORT
            if version.startswith("2.")
            else ReliabilityPolicy.RELIABLE
        )
        sensor_qos = QoSProfile(
            depth=10,
            reliability=reliability,
            durability=DurabilityPolicy.VOLATILE,
        )
        self._sensor = self.create_publisher(
            Float64, "/sensor/reading", sensor_qos
        )
        self._diagnostics = self.create_publisher(
            DiagnosticArray, "/diagnostics", 10
        )
        self._sequence = 0
        self.create_timer(0.05, self._publish_sensor)
        self.create_timer(0.5, self._publish_diagnostic)

    def _publish_sensor(self) -> None:
        message = Float64()
        message.data = float(self._sequence % 100) / 100.0
        self._sequence += 1
        self._sensor.publish(message)

    def _publish_diagnostic(self) -> None:
        status = DiagnosticStatus()
        status.name = "sensor_driver"
        status.hardware_id = "fraeno-fixture"
        status.level = DiagnosticStatus.OK
        status.message = "publishing"
        array = DiagnosticArray()
        array.header.stamp = self.get_clock().now().to_msg()
        array.status = [status]
        self._diagnostics.publish(array)


def main() -> None:
    rclpy.init()
    node = SensorDriver()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
