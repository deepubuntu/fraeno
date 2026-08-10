import sys
from pathlib import Path

from fraeno.config import FraenoConfig, ValidationConfig, load_config
from fraeno.validation.compare import Outcome
from fraeno.validation.runner import run_validation

FIXTURE = Path(__file__).parents[1] / "fixtures" / "deterministic_robot"


def test_end_to_end_safe_update() -> None:
    result = run_validation(
        FIXTURE / "baseline", FIXTURE / "safe", load_config(FIXTURE / "fraeno.yml")
    )
    assert result.outcome is Outcome.PASS
    assert result.to_dict()["engine"] == {"name": "fraeno", "version": "0.2.2"}


def test_end_to_end_breaking_update() -> None:
    result = run_validation(
        FIXTURE / "baseline", FIXTURE / "broken", load_config(FIXTURE / "fraeno.yml")
    )
    assert result.outcome is Outcome.BLOCK
    assert result.comparison is not None
    assert any(
        finding.code == "topic-qos-incompatible"
        for finding in result.comparison.findings
    )


def test_validation_isolates_baseline_and_candidate_ros_domains(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    observation = {
        "schema_version": 1,
        "healthy": True,
        "graph_stable": True,
        "graph": {
            "nodes": [],
            "topics": [],
            "services": [],
            "actions": [],
        },
        "transforms": [],
        "diagnostics": {},
    }
    command = (
        sys.executable,
        "-c",
        (
            "import json, os; "
            f"result = {observation!r}; "
            "result['metadata'] = {'domain_id': int(os.environ['ROS_DOMAIN_ID'])}; "
            "print(json.dumps(result))"
        ),
    )
    config = FraenoConfig(
        version=1,
        project_name="domain-isolation",
        validation=ValidationConfig(steps=(), observation_command=command),
    )

    result = run_validation(baseline, candidate, config)

    assert result.baseline.observation is not None
    assert result.candidate.observation is not None
    baseline_domain_id = result.baseline.observation.metadata["domain_id"]
    candidate_domain_id = result.candidate.observation.metadata["domain_id"]
    assert candidate_domain_id == baseline_domain_id + 1
    assert 1 <= baseline_domain_id <= 99


def test_candidate_observation_failure_is_infrastructure_error(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    script = (
        "import json, os, sys; "
        "candidate = os.environ['FRAENO_PHASE'] == 'candidate'; "
        "sys.exit(8) if candidate else print(json.dumps("
        "{'schema_version': 1, 'healthy': True, 'graph_stable': True, "
        "'graph': {'nodes': [], 'topics': [], 'services': [], 'actions': []}, "
        "'transforms': [], 'diagnostics': {}}))"
    )
    config = FraenoConfig(
        version=1,
        project_name="observation-failure",
        validation=ValidationConfig(
            steps=(),
            observation_command=(sys.executable, "-c", script),
        ),
    )

    result = run_validation(baseline, candidate, config)

    assert result.outcome is Outcome.ERROR
    assert result.candidate.error == "Observation command failed."
