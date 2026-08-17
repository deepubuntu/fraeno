from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fraeno.cli import main
from fraeno.config import (
    CommandStep,
    ConfigError,
    UpdateGroupConfig,
    UpdatePolicyConfig,
    UpdateRuleConfig,
    UpdateScheduleConfig,
    ValidationConfig,
    load_config,
)
from fraeno.dependency_graph import Provenance
from fraeno.models import Ecosystem
from fraeno.update_discovery import UpdateCandidate
from fraeno.update_policy import (
    OpenUpdatePullRequest,
    plan_updates,
    proposal_body,
    schedule_due,
    update_type,
)

ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = ROOT / "fixtures" / "update_discovery"
NOW = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)


def candidate(
    identity: str,
    *,
    current: str = "1.2.3",
    target: str = "1.3.0",
    release_date: str = "2026-07-01T00:00:00Z",
    source_files: tuple[str, ...] = ("requirements.txt",),
) -> UpdateCandidate:
    ecosystem_value, name = identity.split(":", maxsplit=1)
    return UpdateCandidate(
        ecosystem=Ecosystem(ecosystem_value),
        name=name,
        current=current,
        target=target,
        source="https://packages.example/release",
        release_date=release_date,
        provenance=(
            Provenance(
                provider="fixture",
                source="https://packages.example/release",
                evidence="The registry returned the target release.",
            ),
        ),
        source_files=source_files,
    )


def validation_config() -> ValidationConfig:
    return ValidationConfig(
        steps=(
            CommandStep(name="build robot", command=("make", "robot")),
        ),
        observation_command=("python3", "observe.py"),
        required_nodes=frozenset({"/controller"}),
        required_topics=frozenset({"/command"}),
    )


def test_loads_complete_update_policy_and_keeps_validation_compatible(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".fraeno.yml"
    path.write_text(
        """
version: 1
project:
  name: robot
updates:
  allow:
    - dependency: "python:*"
      update_types: [minor, patch]
      cooldown_days: 14
    - "docker:*"
  ignore:
    - dependency: "python:legacy"
      update_types: [major]
  update_types: [major, minor, patch, digest, revision]
  cooldown_days: 3
  groups:
    - name: ROS runtime
      patterns: ["docker:ros", "apt:ros-*"]
  schedule:
    interval: monthly
    day_of_month: 12
  max_open_pull_requests: 4
validation:
  steps: []
  observe:
    command: ["python3", "observe.py"]
"""
    )

    config = load_config(path)

    assert config.project_name == "robot"
    assert config.updates.allow[0] == UpdateRuleConfig(
        dependency="python:*",
        update_types=frozenset({"minor", "patch"}),
        cooldown_days=14,
    )
    assert config.updates.ignore[0].dependency == "python:legacy"
    assert config.updates.groups == (
        UpdateGroupConfig(
            name="ROS runtime",
            patterns=("docker:ros", "apt:ros-*"),
        ),
    )
    assert config.updates.schedule == UpdateScheduleConfig(
        interval="monthly",
        day="monday",
        day_of_month=12,
    )
    assert config.updates.max_open_pull_requests == 4
    assert config.validation.observation_command == ("python3", "observe.py")


@pytest.mark.parametrize(
    ("fragment", "message"),
    [
        ("update_types: [security]", "unsupported update types"),
        ("cooldown_days: -1", "non-negative integer"),
        ("max_open_pull_requests: 0", "positive integer"),
        (
            "schedule: {interval: hourly}",
            "must be daily, weekly, monthly, or manual",
        ),
    ],
)
def test_rejects_unsafe_policy_values(
    tmp_path: Path,
    fragment: str,
    message: str,
) -> None:
    path = tmp_path / ".fraeno.yml"
    path.write_text(
        f"""
version: 1
project:
  name: robot
updates:
  {fragment}
validation:
  steps: []
  observe:
    command: ["true"]
"""
    )

    with pytest.raises(ConfigError, match=message):
        load_config(path)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (candidate("python:one", current="1.2.3", target="2.0.0"), "major"),
        (candidate("python:one", current="1.2.3", target="1.4.0"), "minor"),
        (candidate("python:one", current="1.2.3", target="1.2.4"), "patch"),
        (
            candidate(
                "docker:ros",
                current="humble",
                target="sha256:" + ("2" * 64),
            ),
            "digest",
        ),
        (
            candidate(
                "git:navigation",
                current="1" * 40,
                target="2" * 40,
            ),
            "revision",
        ),
        (candidate("apt:curl", current="ubuntu-1", target="ubuntu-2"), "unknown"),
    ],
)
def test_classifies_update_types(value: UpdateCandidate, expected: str) -> None:
    assert update_type(value) == expected


def test_policy_applies_allow_ignore_type_and_cooldown_rules() -> None:
    policy = UpdatePolicyConfig(
        allow=(
            UpdateRuleConfig(
                "python:*",
                update_types=frozenset({"minor", "patch"}),
                cooldown_days=10,
            ),
        ),
        ignore=(UpdateRuleConfig("python:ignored"),),
        update_types=frozenset({"minor", "patch"}),
        cooldown_days=3,
        schedule=UpdateScheduleConfig(interval="daily"),
    )
    candidates = (
        candidate("python:allowed", target="1.3.0"),
        candidate("python:ignored", target="1.2.4"),
        candidate("python:major", target="2.0.0"),
        candidate(
            "python:fresh",
            target="1.2.4",
            release_date="2026-07-25T00:00:00Z",
        ),
        candidate("docker:ros", target="sha256:" + ("2" * 64)),
    )

    plan = plan_updates(candidates, policy, now=NOW)

    assert [proposal.identities for proposal in plan.proposals] == [
        ("python:allowed",)
    ]
    reasons = {
        item.identities[0]: item.reason
        for item in plan.skipped
    }
    assert reasons == {
        "docker:ros": "update_type_not_allowed",
        "python:fresh": "cooldown_active",
        "python:ignored": "ignored",
        "python:major": "update_type_not_allowed",
    }


def test_unknown_release_date_fails_closed_when_cooldown_is_required() -> None:
    policy = UpdatePolicyConfig(
        cooldown_days=7,
        schedule=UpdateScheduleConfig(interval="daily"),
    )

    plan = plan_updates(
        (candidate("apt:curl", release_date="unknown"),),
        policy,
        now=NOW,
    )

    assert plan.proposals == ()
    assert plan.skipped[0].reason == "cooldown_unverifiable"


def test_default_creates_one_dependency_per_deterministic_branch() -> None:
    policy = UpdatePolicyConfig(
        schedule=UpdateScheduleConfig(interval="daily"),
    )
    candidates = (
        candidate("python:beta"),
        candidate("python:alpha"),
    )

    first = plan_updates(candidates, policy, now=NOW)
    second = plan_updates(tuple(reversed(candidates)), policy, now=NOW)

    assert [proposal.identities for proposal in first.proposals] == [
        ("python:alpha",),
        ("python:beta",),
    ]
    assert [proposal.branch for proposal in first.proposals] == [
        proposal.branch for proposal in second.proposals
    ]
    assert all(
        proposal.branch.startswith("fraeno/update/dependency-python-")
        for proposal in first.proposals
    )


def test_explicit_group_combines_matching_dependencies() -> None:
    policy = UpdatePolicyConfig(
        groups=(
            UpdateGroupConfig(
                name="ROS runtime",
                patterns=("docker:ros", "apt:ros-*"),
            ),
        ),
        schedule=UpdateScheduleConfig(interval="daily"),
    )

    plan = plan_updates(
        (
            candidate("docker:ros", target="sha256:" + ("2" * 64)),
            candidate("apt:ros-core", current="1", target="2"),
            candidate("python:rclpy"),
        ),
        policy,
        now=NOW,
    )

    assert [proposal.identities for proposal in plan.proposals] == [
        ("apt:ros-core", "docker:ros"),
        ("python:rclpy",),
    ]
    assert plan.proposals[0].name == "ROS runtime"


def test_duplicate_is_skipped_and_newer_target_refreshes_same_branch() -> None:
    policy = UpdatePolicyConfig(
        schedule=UpdateScheduleConfig(interval="daily"),
    )
    original = plan_updates(
        (candidate("python:requests", target="1.3.0"),),
        policy,
        now=NOW,
    ).proposals[0]
    open_pull_request = OpenUpdatePullRequest(
        number=8,
        head_branch=original.branch,
        body=proposal_body(original, validation_config()),
    )

    duplicate = plan_updates(
        (candidate("python:requests", target="1.3.0"),),
        policy,
        now=NOW,
        open_pull_requests=(open_pull_request,),
    )
    refreshed = plan_updates(
        (candidate("python:requests", target="1.4.0"),),
        policy,
        now=NOW,
        open_pull_requests=(open_pull_request,),
    )

    assert duplicate.proposals == ()
    assert duplicate.skipped[0].reason == "duplicate_open_pull_request"
    assert refreshed.proposals[0].branch == original.branch
    assert refreshed.proposals[0].action == "refresh"
    assert refreshed.proposals[0].existing_pull_request == 8


def test_open_pull_request_for_dependency_blocks_superseded_branch() -> None:
    policy = UpdatePolicyConfig(
        schedule=UpdateScheduleConfig(interval="daily"),
    )
    open_pull_request = OpenUpdatePullRequest(
        number=9,
        head_branch="fraeno/update/old-generated-branch",
        body=(
            "<!-- fraeno-update-identities: "
            '["python:requests"] -->'
        ),
    )

    plan = plan_updates(
        (candidate("python:requests"),),
        policy,
        now=NOW,
        open_pull_requests=(open_pull_request,),
    )

    assert plan.proposals == ()
    assert plan.skipped[0].reason == "dependency_has_open_update_pull_request"


def test_maximum_open_pull_requests_allows_refresh_but_blocks_new_branch() -> None:
    policy = UpdatePolicyConfig(
        schedule=UpdateScheduleConfig(interval="daily"),
        max_open_pull_requests=1,
    )
    original = plan_updates(
        (candidate("python:requests"),),
        policy,
        now=NOW,
    ).proposals[0]
    open_pull_request = OpenUpdatePullRequest(
        number=10,
        head_branch=original.branch,
        body=proposal_body(original, validation_config()),
    )

    plan = plan_updates(
        (
            candidate("python:requests", target="1.4.0"),
            candidate("python:urllib3"),
        ),
        policy,
        now=NOW,
        open_pull_requests=(open_pull_request,),
    )

    assert plan.proposals[0].action == "refresh"
    assert plan.proposals[0].identities == ("python:requests",)
    assert plan.skipped[0].identities == ("python:urllib3",)
    assert plan.skipped[0].reason == "maximum_open_pull_requests"


def test_schedule_is_deterministic_and_manual_runs_can_override_it() -> None:
    weekly = UpdatePolicyConfig(
        schedule=UpdateScheduleConfig(interval="weekly", day="monday")
    )
    monthly = UpdatePolicyConfig(
        schedule=UpdateScheduleConfig(interval="monthly", day_of_month=27)
    )
    manual = UpdatePolicyConfig(
        schedule=UpdateScheduleConfig(interval="manual")
    )

    assert schedule_due(weekly, NOW)
    assert schedule_due(monthly, NOW)
    assert not schedule_due(manual, NOW)
    assert plan_updates(
        (candidate("python:requests"),),
        manual,
        now=NOW,
        ignore_schedule=True,
    ).proposals


def test_pull_request_body_explains_change_and_evidence_boundaries() -> None:
    proposal = plan_updates(
        (candidate("python:requests"),),
        UpdatePolicyConfig(
            schedule=UpdateScheduleConfig(interval="daily")
        ),
        now=NOW,
    ).proposals[0]

    body = proposal_body(proposal, validation_config())

    assert "`python:requests` from `1.2.3` to `1.3.0`" in body
    assert "`requirements.txt`" in body
    assert "The registry returned the target release." in body
    assert "Run the trusted `build robot` step." in body
    assert "matching baseline and candidate systems" in body
    assert "Physical hardware behavior is not covered" in body
    assert "fraeno-update-fingerprint" in body
    assert "fraeno-update-identities" in body


def test_propose_update_cli_applies_group_without_network(tmp_path: Path) -> None:
    repository = tmp_path / "robot"
    shutil.copytree(FIXTURE_ROOT / "robot_repo", repository)
    config = repository / ".fraeno.yml"
    config.write_text(
        """
version: 1
project:
  name: fixture
updates:
  allow: ["apt:*", "docker:*"]
  groups:
    - name: Runtime image
      patterns: ["apt:*", "docker:*"]
  schedule: manual
  max_open_pull_requests: 2
validation:
  steps:
    - name: build
      command: ["true"]
  observe:
    command: ["true"]
"""
    )
    open_pull_requests = tmp_path / "open.json"
    open_pull_requests.write_text("[]\n")
    output = tmp_path / "proposal.json"

    result = main(
        [
            "propose-update",
            str(repository),
            "--config",
            str(config),
            "--open-pull-requests",
            str(open_pull_requests),
            "--catalog",
            str(FIXTURE_ROOT / "catalog.json"),
            "--ros-distro",
            "humble",
            "--os",
            "ubuntu",
            "--os-version",
            "22.04",
            "--architecture",
            "amd64",
            "--ignore-schedule",
            "--now",
            "2026-07-27T12:00:00Z",
            "--output",
            str(output),
        ]
    )

    payload = json.loads(output.read_text())
    assert result == 0
    assert payload["schema_version"] == 3
    assert payload["updated"]
    assert [
        update["dependency"] for update in payload["proposal"]["results"]
    ] == ["apt:curl", "docker:ros"]
    dockerfile = (repository / "Dockerfile").read_text()
    assert "curl=7.81.0-1ubuntu1.21" in dockerfile
    assert "ros@sha256:" in dockerfile
    assert payload["proposal"]["changed_files"] == ["Dockerfile"]


def test_update_workflow_uses_policy_and_deterministic_branch_refresh() -> None:
    config = load_config(ROOT / ".fraeno.yml")
    workflow = (
        ROOT / ".github" / "workflows" / "fraeno-updates.yml"
    ).read_text()

    assert [rule.dependency for rule in config.updates.allow] == ["docker:python"]
    assert 'cron: "17 9 * * *"' in workflow
    assert "group: fraeno-dependency-updates" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "--open-pull-requests fraeno-open-pull-requests.json" in workflow
    assert "propose-update ." in workflow
    assert "--ignore-schedule" in workflow
    assert "--force-with-lease=" in workflow
    assert "gh pr edit" in workflow
    assert "gh pr create" in workflow
    assert "GITHUB_RUN_ID" not in workflow
    assert "GITHUB_RUN_ATTEMPT" not in workflow
