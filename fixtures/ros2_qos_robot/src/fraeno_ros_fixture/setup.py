from setuptools import find_packages, setup

package_name = "fraeno_ros_fixture"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(),
    data_files=[
        (
            "share/ament_index/resource_index/packages",
            ["resource/" + package_name],
        ),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="DeepUbuntu",
    maintainer_email="admin@deepubuntu.com",
    description="Live ROS 2 QoS regression fixture for Fraeno.",
    license="Proprietary",
    entry_points={
        "console_scripts": [
            "controller = fraeno_ros_fixture.controller:main",
            "sensor_driver = fraeno_ros_fixture.sensor_driver:main",
        ],
    },
)
