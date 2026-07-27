from __future__ import annotations

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
class ValidationConfig:
    steps: tuple[CommandStep, ...]
    observation_command: tuple[str, ...]
    observation_timeout_seconds: int = 60
    required_nodes: frozenset[str] = frozenset()
    required_topics: frozenset[str] = frozenset()
    required_services: frozenset[str] = frozenset()
    required_actions: frozenset[str] = frozenset()
    allowed_missing_baseline_entities: frozenset[str] = frozenset()
    minimum_topic_rates_hz: dict[str, float] = field(default_factory=dict)
    maximum_topic_rate_regression_percent: float = 20.0


@dataclass(frozen=True)
class FraenoConfig:
    version: int
    project_name: str
    validation: ValidationConfig


def _command(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(
        isinstance(part, str) and part for part in value
    ):
        raise ConfigError(f"{field_name} must be a non-empty list of strings")
    return tuple(value)


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

    return FraenoConfig(
        version=version,
        project_name=project["name"],
        validation=ValidationConfig(
            steps=tuple(steps),
            observation_command=_command(
                observe.get("command"), "validation.observe.command"
            ),
            observation_timeout_seconds=int(observe.get("timeout_seconds", 60)),
            required_nodes=frozenset(contract.get("required_nodes", [])),
            required_topics=frozenset(contract.get("required_topics", [])),
            required_services=frozenset(contract.get("required_services", [])),
            required_actions=frozenset(contract.get("required_actions", [])),
            allowed_missing_baseline_entities=frozenset(
                contract.get("allowed_missing_baseline_entities", [])
            ),
            minimum_topic_rates_hz={
                str(topic): float(rate) for topic, rate in rate_values.items()
            },
            maximum_topic_rate_regression_percent=float(
                contract.get("maximum_topic_rate_regression_percent", 20.0)
            ),
        ),
    )
