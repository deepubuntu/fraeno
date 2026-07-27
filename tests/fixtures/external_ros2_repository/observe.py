from __future__ import annotations

import json
from pathlib import Path


def endpoint(node: str, reliability: str) -> dict[str, object]:
    return {
        "node": node,
        "qos": {
            "reliability": reliability,
            "durability": "volatile",
            "history": "keep_last",
            "depth": 10,
        },
    }


profile = Path("robot-profile.txt").read_text().strip()
broken = profile == "best-effort"
attack_results: dict[str, bool] = {}
attack_file = Path("hostile-results.json")
if attack_file.exists():
    attack_results = json.loads(attack_file.read_text())

result = {
    "schema_version": 1,
    "healthy": not broken,
    "graph_stable": True,
    "graph": {
        "nodes": ["/external_sensor", "/external_controller"],
        "topics": [
            {
                "name": "/external/sensor",
                "types": ["std_msgs/msg/Float64"],
                "publishers": [
                    endpoint(
                        "/external_sensor",
                        "best_effort" if broken else "reliable",
                    )
                ],
                "subscribers": [endpoint("/external_controller", "reliable")],
                "rate_hz": 20.0,
                "message_count": 20,
            },
            {
                "name": "/external/command",
                "types": ["std_msgs/msg/Float64"],
                "publishers": [endpoint("/external_controller", "reliable")],
                "subscribers": [],
                "rate_hz": 0.0 if broken else 10.0,
                "message_count": 0 if broken else 10,
            },
        ],
        "services": [
            {"name": "/external/health", "types": ["std_srvs/srv/Trigger"]}
        ],
        "actions": [],
    },
    "transforms": [],
    "diagnostics": {"external_controller": 2 if broken else 0},
    "metadata": {
        "profile": profile,
        "protected_write_attempts_succeeded": attack_results,
    },
}
print(json.dumps(result))
