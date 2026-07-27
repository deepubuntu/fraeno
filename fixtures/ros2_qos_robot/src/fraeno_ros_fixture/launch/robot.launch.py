from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    return LaunchDescription(
        [
            Node(
                package="fraeno_ros_fixture",
                executable="sensor_driver",
                output="screen",
            ),
            Node(
                package="fraeno_ros_fixture",
                executable="controller",
                output="screen",
            ),
        ]
    )
