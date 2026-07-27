from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def observation(version: str) -> dict[str, Any]:
    broken = version.startswith("2.")
    return {
        "schema_version": 1,
        "healthy": not broken,
        "graph_stable": True,
        "graph": {
            "nodes": ["/controller", "/sensor_driver"],
            "topics": [
                {
                    "name": "/sensor/reading",
                    "types": ["std_msgs/msg/Float64"],
                    "publishers": [
                        {
                            "node": "/sensor_driver",
                            "qos": {
                                "reliability": (
                                    "best_effort" if broken else "reliable"
                                ),
                                "durability": "volatile",
                                "history": "keep_last",
                                "depth": 10,
                            },
                        }
                    ],
                    "subscribers": [
                        {
                            "node": "/controller",
                            "qos": {
                                "reliability": "reliable",
                                "durability": "volatile",
                                "history": "keep_last",
                                "depth": 10,
                            },
                        }
                    ],
                    "rate_hz": 20.0,
                    "message_count": 100,
                },
                {
                    "name": "/robot/command",
                    "types": ["std_msgs/msg/Float64"],
                    "publishers": [
                        {
                            "node": "/controller",
                            "qos": {
                                "reliability": "reliable",
                                "durability": "volatile",
                            },
                        }
                    ],
                    "subscribers": [],
                    "rate_hz": 0.0 if broken else 10.0,
                    "message_count": 0 if broken else 50,
                },
            ],
            "services": [
                {"name": "/robot/health", "types": ["std_srvs/srv/Trigger"]}
            ],
            "actions": [
                {
                    "name": "/robot/move",
                    "types": ["example_interfaces/action/Fibonacci"],
                }
            ],
        },
        "transforms": ["base_link->sensor_link"],
        "diagnostics": {
            "controller": 2 if broken else 0,
            "sensor_driver": 0,
        },
        "metadata": {"dependency_version": version},
    }


def main() -> int:
    version_file = Path("fraeno-fixture-version.txt")
    if not version_file.is_file():
        raise SystemExit("fraeno-fixture-version.txt is missing")
    print(json.dumps(observation(version_file.read_text().strip()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
