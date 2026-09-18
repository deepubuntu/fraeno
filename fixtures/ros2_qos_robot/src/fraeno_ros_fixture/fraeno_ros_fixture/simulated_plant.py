from __future__ import annotations

import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import Float64


class SimulatedPlant(Node):
    """Small one-dimensional velocity model for the CI stop-response scenario."""

    def __init__(self) -> None:
        super().__init__("simulated_plant")
        self._target = 0.0
        self._velocity = 0.0
        self._last_step = time.monotonic()
        self.create_subscription(Float64, "/robot/command", self._on_command, 10)
        self._velocity_publisher = self.create_publisher(Float64, "/robot/velocity", 10)
        self.create_timer(0.02, self._step)

    def _on_command(self, message: Float64) -> None:
        self._target = max(-1.0, min(1.0, float(message.data)))

    def _step(self) -> None:
        now = time.monotonic()
        elapsed = min(now - self._last_step, 0.1)
        self._last_step = now
        maximum_change = 4.0 * elapsed
        difference = self._target - self._velocity
        self._velocity += max(-maximum_change, min(maximum_change, difference))
        message = Float64()
        message.data = self._velocity
        self._velocity_publisher.publish(message)


def main() -> None:
    rclpy.init()
    node = SimulatedPlant()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
