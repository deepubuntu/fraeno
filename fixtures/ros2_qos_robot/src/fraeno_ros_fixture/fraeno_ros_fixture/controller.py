from __future__ import annotations

import time

import rclpy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
from example_interfaces.action import Fibonacci
from geometry_msgs.msg import TransformStamped
from rclpy.action import ActionServer
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Float64
from std_srvs.srv import Trigger
from tf2_ros.static_transform_broadcaster import StaticTransformBroadcaster


class Controller(Node):
    def __init__(self) -> None:
        super().__init__("controller")
        reliable = QoSProfile(
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        self._last_sensor_at: float | None = None
        self._commands = self.create_publisher(
            Float64, "/robot/command", reliable
        )
        self.create_subscription(
            Float64, "/sensor/reading", self._on_sensor, reliable
        )
        self._diagnostics = self.create_publisher(
            DiagnosticArray, "/diagnostics", 10
        )
        self.create_service(Trigger, "/robot/health", self._health)
        self._move_action = ActionServer(
            self,
            Fibonacci,
            "/robot/move",
            self._move,
        )
        self._transform = StaticTransformBroadcaster(self)
        transform = TransformStamped()
        transform.header.stamp = self.get_clock().now().to_msg()
        transform.header.frame_id = "base_link"
        transform.child_frame_id = "sensor_link"
        transform.transform.rotation.w = 1.0
        self._transform.sendTransform(transform)
        self.create_timer(0.5, self._publish_diagnostic)

    def _on_sensor(self, message: Float64) -> None:
        self._last_sensor_at = time.monotonic()
        command = Float64()
        command.data = max(-1.0, min(1.0, message.data))
        self._commands.publish(command)

    def _is_healthy(self) -> bool:
        return (
            self._last_sensor_at is not None
            and time.monotonic() - self._last_sensor_at < 1.0
        )

    def _health(
        self, request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        del request
        response.success = self._is_healthy()
        response.message = (
            "sensor stream healthy" if response.success else "sensor stream missing"
        )
        return response

    def _publish_diagnostic(self) -> None:
        healthy = self._is_healthy()
        status = DiagnosticStatus()
        status.name = "controller"
        status.hardware_id = "fraeno-fixture"
        status.level = DiagnosticStatus.OK if healthy else DiagnosticStatus.ERROR
        status.message = "receiving sensor data" if healthy else "sensor data missing"
        array = DiagnosticArray()
        array.header.stamp = self.get_clock().now().to_msg()
        array.status = [status]
        self._diagnostics.publish(array)

    async def _move(
        self,
        goal_handle: ActionServer,
    ) -> Fibonacci.Result:
        goal_handle.succeed()
        result = Fibonacci.Result()
        result.sequence = [0, 1]
        return result


def main() -> None:
    rclpy.init()
    node = Controller()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
