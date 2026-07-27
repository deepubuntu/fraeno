from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CommandStep:
    name: str
    command: tuple[str, ...]
    timeout_seconds: int = 600


@dataclass(frozen=True)
class Ros2ObserverConfig:
    launch_command: tuple[str, ...]
    warmup_seconds: float = 2.0
    graph_stabilization_timeout_seconds: float = 15.0
    graph_stabilization_interval_seconds: float = 0.25
    graph_stabilization_samples: int = 3
    sample_seconds: float = 5.0
    measurement_repetitions: int = 3
    rate_topics: frozenset[str] = frozenset()
    diagnostics_topics: frozenset[str] = frozenset()
    transform_topics: frozenset[str] = frozenset()
    shutdown_timeout_seconds: float = 5.0


@dataclass(frozen=True)
class ValidationConfig:
    steps: tuple[CommandStep, ...]
    observation_command: tuple[str, ...]
    observation_timeout_seconds: int = 60
    ros2_observer: Ros2ObserverConfig | None = None
    required_nodes: frozenset[str] = frozenset()
    required_topics: frozenset[str] = frozenset()
    required_services: frozenset[str] = frozenset()
    required_actions: frozenset[str] = frozenset()
    required_transforms: frozenset[str] = frozenset()
    required_diagnostics: frozenset[str] = frozenset()
    allowed_missing_baseline_entities: frozenset[str] = frozenset()
    minimum_topic_rates_hz: dict[str, float] = field(default_factory=dict)
    maximum_topic_rate_regression_percent: float = 20.0


UPDATE_TYPES = frozenset(
    {"major", "minor", "patch", "digest", "revision", "unknown"}
)
SCHEDULE_INTERVALS = frozenset({"daily", "weekly", "monthly", "manual"})
WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


@dataclass(frozen=True)
class UpdateRuleConfig:
    dependency: str
    update_types: frozenset[str] = frozenset()
    cooldown_days: int | None = None


@dataclass(frozen=True)
class UpdateGroupConfig:
    name: str
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class UpdateScheduleConfig:
    interval: str = "weekly"
    day: str = "monday"
    day_of_month: int = 1


@dataclass(frozen=True)
class UpdatePolicyConfig:
    allow: tuple[UpdateRuleConfig, ...] = ()
    ignore: tuple[UpdateRuleConfig, ...] = ()
    update_types: frozenset[str] = UPDATE_TYPES
    cooldown_days: int = 0
    groups: tuple[UpdateGroupConfig, ...] = ()
    schedule: UpdateScheduleConfig = UpdateScheduleConfig()
    max_open_pull_requests: int = 5


@dataclass(frozen=True)
class FraenoConfig:
    version: int
    project_name: str
    validation: ValidationConfig
    updates: UpdatePolicyConfig = UpdatePolicyConfig()


def _command(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(
        isinstance(part, str) and part for part in value
    ):
        raise ConfigError(f"{field_name} must be a non-empty list of strings")
    return tuple(value)


def _string_set(value: Any, field_name: str) -> frozenset[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ConfigError(f"{field_name} must be a list of non-empty strings")
    return frozenset(value)


def _positive_float(value: Any, field_name: str, default: float) -> float:
    try:
        result = float(default if value is None else value)
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{field_name} must be a positive number") from error
    if result <= 0:
        raise ConfigError(f"{field_name} must be a positive number")
    return result


def _non_negative_float(value: Any, field_name: str, default: float) -> float:
    try:
        result = float(default if value is None else value)
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{field_name} must be a non-negative number") from error
    if result < 0:
        raise ConfigError(f"{field_name} must be a non-negative number")
    return result


def _positive_int(value: Any, field_name: str, default: int) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} must be a positive integer")
    try:
        result = int(default if value is None else value)
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{field_name} must be a positive integer") from error
    if result <= 0:
        raise ConfigError(f"{field_name} must be a positive integer")
    return result


def _ros2_observer(
    raw: Any,
    *,
    default_rate_topics: frozenset[str],
) -> Ros2ObserverConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("validation.observe.ros2 must be an object")
    return Ros2ObserverConfig(
        launch_command=_command(
            raw.get("launch_command"),
            "validation.observe.ros2.launch_command",
        ),
        warmup_seconds=_non_negative_float(
            raw.get("warmup_seconds"),
            "validation.observe.ros2.warmup_seconds",
            2.0,
        ),
        graph_stabilization_timeout_seconds=_positive_float(
            raw.get("graph_stabilization_timeout_seconds"),
            "validation.observe.ros2.graph_stabilization_timeout_seconds",
            15.0,
        ),
        graph_stabilization_interval_seconds=_positive_float(
            raw.get("graph_stabilization_interval_seconds"),
            "validation.observe.ros2.graph_stabilization_interval_seconds",
            0.25,
        ),
        graph_stabilization_samples=_positive_int(
            raw.get("graph_stabilization_samples"),
            "validation.observe.ros2.graph_stabilization_samples",
            3,
        ),
        sample_seconds=_positive_float(
            raw.get("sample_seconds"),
            "validation.observe.ros2.sample_seconds",
            5.0,
        ),
        measurement_repetitions=_positive_int(
            raw.get("measurement_repetitions"),
            "validation.observe.ros2.measurement_repetitions",
            3,
        ),
        rate_topics=(
            _string_set(
                raw["rate_topics"],
                "validation.observe.ros2.rate_topics",
            )
            if "rate_topics" in raw
            else default_rate_topics
        ),
        diagnostics_topics=_string_set(
            raw.get("diagnostics_topics", []),
            "validation.observe.ros2.diagnostics_topics",
        ),
        transform_topics=_string_set(
            raw.get("transform_topics", []),
            "validation.observe.ros2.transform_topics",
        ),
        shutdown_timeout_seconds=_positive_float(
            raw.get("shutdown_timeout_seconds"),
            "validation.observe.ros2.shutdown_timeout_seconds",
            5.0,
        ),
    )


def _update_types(value: Any, field_name: str) -> frozenset[str]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ConfigError(f"{field_name} must be a non-empty list of update types")
    normalized = frozenset(item.lower() for item in value)
    unsupported = sorted(normalized - UPDATE_TYPES)
    if unsupported:
        raise ConfigError(
            f"{field_name} contains unsupported update types: "
            + ", ".join(unsupported)
        )
    return normalized


def _nonnegative_integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field_name} must be a non-negative integer")
    if value < 0:
        raise ConfigError(f"{field_name} must be a non-negative integer")
    return int(value)


def _rules(value: Any, field_name: str) -> tuple[UpdateRuleConfig, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{field_name} must be a list")
    rules: list[UpdateRuleConfig] = []
    for index, raw_rule in enumerate(value):
        item_name = f"{field_name}[{index}]"
        dependency: str
        rule_types: frozenset[str]
        cooldown_days: int | None
        if isinstance(raw_rule, str):
            dependency = raw_rule
            rule_types = frozenset()
            cooldown_days = None
        elif isinstance(raw_rule, dict):
            unsupported = sorted(
                set(raw_rule) - {"dependency", "update_types", "cooldown_days"}
            )
            if unsupported:
                raise ConfigError(
                    f"{item_name} contains unsupported fields: "
                    + ", ".join(unsupported)
                )
            raw_dependency = raw_rule.get("dependency")
            if not isinstance(raw_dependency, str) or not raw_dependency:
                raise ConfigError(f"{item_name}.dependency is required")
            dependency = raw_dependency
            raw_types = raw_rule.get("update_types")
            rule_types = (
                _update_types(raw_types, f"{item_name}.update_types")
                if raw_types is not None
                else frozenset()
            )
            raw_cooldown = raw_rule.get("cooldown_days")
            cooldown_days = (
                _nonnegative_integer(raw_cooldown, f"{item_name}.cooldown_days")
                if raw_cooldown is not None
                else None
            )
        else:
            raise ConfigError(f"{item_name} must be a dependency pattern or object")
        if (
            not dependency
            or dependency != dependency.strip()
            or any(character.isspace() for character in dependency)
        ):
            raise ConfigError(
                f"{item_name}.dependency must be a non-empty pattern without whitespace"
            )
        rules.append(
            UpdateRuleConfig(
                dependency=dependency.lower(),
                update_types=rule_types,
                cooldown_days=cooldown_days,
            )
        )
    return tuple(rules)


def _groups(value: Any) -> tuple[UpdateGroupConfig, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError("updates.groups must be a list")
    groups: list[UpdateGroupConfig] = []
    seen_names: set[str] = set()
    for index, raw_group in enumerate(value):
        field_name = f"updates.groups[{index}]"
        if not isinstance(raw_group, dict):
            raise ConfigError(f"{field_name} must be an object")
        unsupported = sorted(set(raw_group) - {"name", "patterns"})
        if unsupported:
            raise ConfigError(
                f"{field_name} contains unsupported fields: "
                + ", ".join(unsupported)
            )
        name = raw_group.get("name")
        patterns = raw_group.get("patterns")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{field_name}.name is required")
        normalized_name = name.strip()
        if (
            len(normalized_name) > 50
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]*", normalized_name)
            is None
        ):
            raise ConfigError(
                f"{field_name}.name must use 50 or fewer letters, numbers, "
                "spaces, dots, underscores, or hyphens"
            )
        if normalized_name.lower() in seen_names:
            raise ConfigError("updates.groups names must be unique")
        seen_names.add(normalized_name.lower())
        if not isinstance(patterns, list) or not patterns or not all(
            isinstance(pattern, str) and pattern and pattern == pattern.strip()
            and not any(character.isspace() for character in pattern)
            for pattern in patterns
        ):
            raise ConfigError(
                f"{field_name}.patterns must be a non-empty list of patterns"
            )
        groups.append(
            UpdateGroupConfig(
                name=normalized_name,
                patterns=tuple(pattern.lower() for pattern in patterns),
            )
        )
    return tuple(groups)


def _schedule(value: Any) -> UpdateScheduleConfig:
    if value is None:
        return UpdateScheduleConfig()
    if isinstance(value, str):
        raw_schedule: dict[str, Any] = {"interval": value}
    elif isinstance(value, dict):
        raw_schedule = value
    else:
        raise ConfigError("updates.schedule must be an interval or object")
    unsupported = sorted(
        set(raw_schedule) - {"interval", "day", "day_of_month"}
    )
    if unsupported:
        raise ConfigError(
            "updates.schedule contains unsupported fields: "
            + ", ".join(unsupported)
        )
    interval = raw_schedule.get("interval", "weekly")
    if not isinstance(interval, str) or interval.lower() not in SCHEDULE_INTERVALS:
        raise ConfigError(
            "updates.schedule.interval must be daily, weekly, monthly, or manual"
        )
    normalized_interval = interval.lower()
    day = raw_schedule.get("day", "monday")
    if not isinstance(day, str) or day.lower() not in WEEKDAYS:
        raise ConfigError(
            "updates.schedule.day must be a lowercase weekday name"
        )
    day_of_month = _nonnegative_integer(
        raw_schedule.get("day_of_month", 1),
        "updates.schedule.day_of_month",
    )
    if not 1 <= day_of_month <= 28:
        raise ConfigError("updates.schedule.day_of_month must be between 1 and 28")
    return UpdateScheduleConfig(
        interval=normalized_interval,
        day=day.lower(),
        day_of_month=day_of_month,
    )


def _update_policy(value: Any) -> UpdatePolicyConfig:
    if value is None:
        return UpdatePolicyConfig()
    if not isinstance(value, dict):
        raise ConfigError("updates must be an object")
    unsupported = sorted(
        set(value)
        - {
            "allow",
            "ignore",
            "update_types",
            "cooldown_days",
            "groups",
            "schedule",
            "max_open_pull_requests",
        }
    )
    if unsupported:
        raise ConfigError(
            "updates contains unsupported fields: " + ", ".join(unsupported)
        )
    raw_types = value.get("update_types")
    update_types = (
        _update_types(raw_types, "updates.update_types")
        if raw_types is not None
        else UPDATE_TYPES
    )
    max_open = value.get("max_open_pull_requests", 5)
    if isinstance(max_open, bool) or not isinstance(max_open, int):
        raise ConfigError("updates.max_open_pull_requests must be a positive integer")
    if max_open <= 0:
        raise ConfigError(
            "updates.max_open_pull_requests must be a positive integer"
        )
    return UpdatePolicyConfig(
        allow=_rules(value.get("allow"), "updates.allow"),
        ignore=_rules(value.get("ignore"), "updates.ignore"),
        update_types=update_types,
        cooldown_days=_nonnegative_integer(
            value.get("cooldown_days", 0), "updates.cooldown_days"
        ),
        groups=_groups(value.get("groups")),
        schedule=_schedule(value.get("schedule")),
        max_open_pull_requests=max_open,
    )


def load_config(path: Path) -> FraenoConfig:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ConfigError("configuration must be a YAML object")
    version = raw.get("version")
    if version != 1:
        raise ConfigError("only configuration version 1 is supported")

    project = raw.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("name"), str):
        raise ConfigError("project.name is required")

    validation = raw.get("validation")
    if not isinstance(validation, dict):
        raise ConfigError("validation is required")

    steps_raw = validation.get("steps", [])
    if not isinstance(steps_raw, list):
        raise ConfigError("validation.steps must be a list")
    steps: list[CommandStep] = []
    for index, step in enumerate(steps_raw):
        if not isinstance(step, dict) or not isinstance(step.get("name"), str):
            raise ConfigError(f"validation.steps[{index}].name is required")
        steps.append(
            CommandStep(
                name=step["name"],
                command=_command(
                    step.get("command"), f"validation.steps[{index}].command"
                ),
                timeout_seconds=int(step.get("timeout_seconds", 600)),
            )
        )

    observe = validation.get("observe")
    if not isinstance(observe, dict):
        raise ConfigError("validation.observe is required")
    contract = validation.get("contract", {})
    if not isinstance(contract, dict):
        raise ConfigError("validation.contract must be an object")

    rate_values = contract.get("minimum_topic_rates_hz", {})
    if not isinstance(rate_values, dict):
        raise ConfigError("minimum_topic_rates_hz must be an object")
    minimum_topic_rates_hz = {
        str(topic): float(rate) for topic, rate in rate_values.items()
    }
    if any(rate < 0 for rate in minimum_topic_rates_hz.values()):
        raise ConfigError("minimum_topic_rates_hz values must be non-negative")

    required_nodes = _string_set(
        contract.get("required_nodes", []),
        "validation.contract.required_nodes",
    )
    required_topics = _string_set(
        contract.get("required_topics", []),
        "validation.contract.required_topics",
    )
    required_services = _string_set(
        contract.get("required_services", []),
        "validation.contract.required_services",
    )
    required_actions = _string_set(
        contract.get("required_actions", []),
        "validation.contract.required_actions",
    )
    required_transforms = _string_set(
        contract.get("required_transforms", []),
        "validation.contract.required_transforms",
    )
    required_diagnostics = _string_set(
        contract.get("required_diagnostics", []),
        "validation.contract.required_diagnostics",
    )
    allowed_missing = _string_set(
        contract.get("allowed_missing_baseline_entities", []),
        "validation.contract.allowed_missing_baseline_entities",
    )
    maximum_regression = float(
        contract.get("maximum_topic_rate_regression_percent", 20.0)
    )
    if not 0 <= maximum_regression <= 100:
        raise ConfigError(
            "maximum_topic_rate_regression_percent must be between 0 and 100"
        )

    return FraenoConfig(
        version=version,
        project_name=project["name"],
        validation=ValidationConfig(
            steps=tuple(steps),
            observation_command=_command(
                observe.get("command"), "validation.observe.command"
            ),
            observation_timeout_seconds=int(observe.get("timeout_seconds", 60)),
            ros2_observer=_ros2_observer(
                observe.get("ros2"),
                default_rate_topics=frozenset(
                    required_topics | minimum_topic_rates_hz.keys()
                ),
            ),
            required_nodes=required_nodes,
            required_topics=required_topics,
            required_services=required_services,
            required_actions=required_actions,
            required_transforms=required_transforms,
            required_diagnostics=required_diagnostics,
            allowed_missing_baseline_entities=allowed_missing,
            minimum_topic_rates_hz=minimum_topic_rates_hz,
            maximum_topic_rate_regression_percent=maximum_regression,
        ),
        updates=_update_policy(raw.get("updates")),
    )
