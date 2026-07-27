from setuptools import setup

setup(
    name="external_robot",
    version="1.0.0",
    packages=["external_robot"],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/external_robot"]),
        ("share/external_robot", ["package.xml"]),
    ],
)
