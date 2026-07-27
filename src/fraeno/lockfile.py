from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from fraeno.dependency_graph import (
    DependencyGraphBuilder,
    ResolutionProvider,
    TargetPlatform,
    infer_target_platform,
)
from fraeno.models import ScanReport


class LockfileError(ValueError):
    pass


def build_lockfile(
    report: ScanReport,
    root: Path,
    *,
    target: TargetPlatform | None = None,
    providers: Sequence[ResolutionProvider] = (),
) -> dict[str, Any]:
    selected_target = target or infer_target_platform(report)
    return DependencyGraphBuilder(selected_target, providers).build(report, root).to_dict()


def read_lockfile(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise LockfileError("lockfile must contain a JSON object")
    lockfile = cast(dict[str, Any], raw)
    if lockfile.get("schema_version") != 2:
        raise LockfileError("lockfile must use schema_version 2")
    return lockfile


def write_lockfile(lockfile: Mapping[str, Any], destination: Path) -> None:
    destination.write_text(json.dumps(lockfile, indent=2, sort_keys=True) + "\n")


def compare_lockfiles(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    _require_schema_two(baseline)
    _require_schema_two(candidate)
    baseline_declarations = _nodes_by_id(baseline, "declarations")
    candidate_declarations = _nodes_by_id(candidate, "declarations")
    baseline_artifacts = _nodes_by_id(baseline, "artifacts")
    candidate_artifacts = _nodes_by_id(candidate, "artifacts")
    baseline_manifests = _nodes_by_id(baseline, "manifests")
    candidate_manifests = _nodes_by_id(candidate, "manifests")

    declaration_changes: list[dict[str, Any]] = []
    declaration_ids = sorted(set(baseline_declarations) | set(candidate_declarations))
    for declaration_id in declaration_ids:
        before = baseline_declarations.get(declaration_id)
        after = candidate_declarations.get(declaration_id)
        if before is None:
            declaration_changes.append(
                _declaration_change(
                    "added",
                    None,
                    after,
                    baseline_artifacts,
                    candidate_artifacts,
                )
            )
            continue
        if after is None:
            declaration_changes.append(
                _declaration_change(
                    "removed",
                    before,
                    None,
                    baseline_artifacts,
                    candidate_artifacts,
                )
            )
            continue
        before_state = _declaration_state(before, baseline_artifacts)
        after_state = _declaration_state(after, candidate_artifacts)
        if before_state != after_state:
            declaration_changes.append(
                _declaration_change(
                    "changed",
                    before,
                    after,
                    baseline_artifacts,
                    candidate_artifacts,
                )
            )

    component_changes = _component_changes(declaration_changes)
    target_changes = _mapping_changes(
        _mapping(baseline.get("target"), "target"),
        _mapping(candidate.get("target"), "target"),
    )
    manifest_changes = _manifest_changes(
        baseline_manifests,
        candidate_manifests,
    )
    cross_layer_changes = [change for change in component_changes if len(change["ecosystems"]) > 1]
    changed = bool(declaration_changes or target_changes or manifest_changes)
    return {
        "schema_version": 1,
        "changed": changed,
        "target_changes": target_changes,
        "manifest_changes": manifest_changes,
        "declaration_changes": declaration_changes,
        "component_changes": component_changes,
        "cross_layer_changes": cross_layer_changes,
        "summary": {
            "declarations_changed": len(declaration_changes),
            "components_changed": len(component_changes),
            "cross_layer_components_changed": len(cross_layer_changes),
            "manifests_changed": len(manifest_changes),
            "target_fields_changed": len(target_changes),
            "explanations": [str(change["explanation"]) for change in component_changes],
        },
    }


def _require_schema_two(lockfile: Mapping[str, Any]) -> None:
    if lockfile.get("schema_version") != 2:
        raise LockfileError("lock comparison requires schema_version 2")


def _nodes_by_id(
    payload: Mapping[str, Any],
    field: str,
) -> dict[str, dict[str, Any]]:
    raw_nodes = payload.get(field)
    if not isinstance(raw_nodes, list):
        raise LockfileError(f"lockfile field {field!r} must be a list")
    result: dict[str, dict[str, Any]] = {}
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            raise LockfileError(f"lockfile field {field!r} contains a non-object")
        node = cast(dict[str, Any], raw_node)
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise LockfileError(f"lockfile field {field!r} contains an invalid id")
        if node_id in result:
            raise LockfileError(f"lockfile field {field!r} contains duplicate id {node_id}")
        result[node_id] = node
    return result


def _declaration_change(
    kind: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    baseline_artifacts: Mapping[str, dict[str, Any]],
    candidate_artifacts: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    declaration = after or before
    if declaration is None:
        raise LockfileError("declaration change has no declaration")
    before_state = _declaration_state(before, baseline_artifacts) if before is not None else None
    after_state = _declaration_state(after, candidate_artifacts) if after is not None else None
    source = _mapping(declaration.get("source"), "declaration source")
    ecosystem = _required_string(declaration, "ecosystem")
    name = _required_string(declaration, "name")
    component_id = _required_string(declaration, "component_id")
    return {
        "kind": kind,
        "declaration_id": _required_string(declaration, "id"),
        "component_id": component_id,
        "ecosystem": ecosystem,
        "name": name,
        "source": {
            "path": _required_string(source, "path"),
            "line": source.get("line"),
        },
        "before": before_state,
        "after": after_state,
        "explanation": _declaration_explanation(
            kind,
            ecosystem,
            name,
            source,
            before_state,
            after_state,
        ),
    }


def _declaration_state(
    declaration: dict[str, Any],
    artifacts: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    resolution = _mapping(declaration.get("resolution"), "declaration resolution")
    artifact_id = resolution.get("artifact_id")
    artifact: dict[str, Any] | None = None
    if artifact_id is not None:
        if not isinstance(artifact_id, str):
            raise LockfileError("artifact_id must be a string or null")
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            raise LockfileError(f"declaration references missing artifact {artifact_id}")
    return {
        "constraint": declaration.get("constraint"),
        "declared_value": declaration.get("declared_value"),
        "resolution_status": resolution.get("status"),
        "unknown_reason": resolution.get("unknown_reason"),
        "artifact_id": artifact_id,
        "artifact_version": artifact.get("version") if artifact else None,
        "target": declaration.get("target"),
    }


def _component_changes(
    declaration_changes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for change in declaration_changes:
        component_id = str(change["component_id"])
        grouped.setdefault(component_id, []).append(change)

    changes: list[dict[str, Any]] = []
    for component_id, items in sorted(grouped.items()):
        ordered = sorted(
            items,
            key=lambda item: (
                str(item["ecosystem"]),
                str(cast(dict[str, Any], item["source"])["path"]),
                int(cast(dict[str, Any], item["source"]).get("line") or 0),
                str(item["name"]),
            ),
        )
        ecosystems = sorted({str(item["ecosystem"]) for item in ordered})
        layer_word = "layer" if len(ecosystems) == 1 else "layers"
        details = " ".join(str(item["explanation"]) for item in ordered)
        changes.append(
            {
                "component_id": component_id,
                "component": component_id.removeprefix("component:"),
                "ecosystems": ecosystems,
                "declaration_changes": ordered,
                "explanation": (
                    f"{component_id.removeprefix('component:')} changed across "
                    f"{', '.join(ecosystems)} {layer_word}. {details}"
                ),
            }
        )
    return changes


def _declaration_explanation(
    kind: str,
    ecosystem: str,
    name: str,
    source: Mapping[str, Any],
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> str:
    location = _source_description(source)
    if kind == "added":
        return f"{ecosystem} {name} at {location} was added at {_state_value(after)}."
    if kind == "removed":
        return f"{ecosystem} {name} at {location} was removed from {_state_value(before)}."
    return (
        f"{ecosystem} {name} at {location} changed from "
        f"{_state_value(before)} to {_state_value(after)}."
    )


def _state_value(state: Mapping[str, Any] | None) -> str:
    if state is None:
        return "absent"
    version = state.get("artifact_version") or state.get("declared_value")
    if version is None:
        return f"unknown resolution ({state.get('unknown_reason')})"
    return str(version)


def _source_description(source: Mapping[str, Any]) -> str:
    path = _required_string(source, "path")
    line = source.get("line")
    if line is None:
        return path
    return f"{path} line {line}"


def _manifest_changes(
    baseline: Mapping[str, dict[str, Any]],
    candidate: Mapping[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for manifest_id in sorted(set(baseline) | set(candidate)):
        before = baseline.get(manifest_id)
        after = candidate.get(manifest_id)
        if before is None:
            changes.append(
                {
                    "kind": "added",
                    "manifest_id": manifest_id,
                    "path": _required_string(cast(dict[str, Any], after), "path"),
                    "before_sha256": None,
                    "after_sha256": cast(dict[str, Any], after).get("sha256"),
                }
            )
        elif after is None:
            changes.append(
                {
                    "kind": "removed",
                    "manifest_id": manifest_id,
                    "path": _required_string(before, "path"),
                    "before_sha256": before.get("sha256"),
                    "after_sha256": None,
                }
            )
        elif before.get("sha256") != after.get("sha256"):
            changes.append(
                {
                    "kind": "changed",
                    "manifest_id": manifest_id,
                    "path": _required_string(after, "path"),
                    "before_sha256": before.get("sha256"),
                    "after_sha256": after.get("sha256"),
                }
            )
    return changes


def _mapping_changes(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "field": field,
            "before": baseline.get(field),
            "after": candidate.get(field),
        }
        for field in sorted(set(baseline) | set(candidate))
        if baseline.get(field) != candidate.get(field)
    ]


def _mapping(value: object, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LockfileError(f"{description} must be an object")
    return cast(dict[str, Any], value)


def _required_string(value: Mapping[str, Any], field: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw:
        raise LockfileError(f"field {field!r} must be a non-empty string")
    return raw
