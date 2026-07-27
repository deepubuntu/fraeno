from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any

from packaging.version import InvalidVersion, Version

from fraeno.config import (
    UpdatePolicyConfig,
    UpdateRuleConfig,
    ValidationConfig,
)
from fraeno.models import Ecosystem
from fraeno.update_discovery import UpdateCandidate

UPDATE_BRANCH_PREFIX = "fraeno/update/"
_FINGERPRINT_PATTERN = re.compile(
    r"<!-- fraeno-update-fingerprint: (?P<value>[0-9a-f]{64}) -->"
)
_IDENTITIES_PATTERN = re.compile(
    r"<!-- fraeno-update-identities: (?P<value>\[[^\n]*\]) -->"
)


@dataclass(frozen=True)
class OpenUpdatePullRequest:
    number: int
    head_branch: str
    head_sha: str = ""
    title: str = ""
    body: str = ""

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> OpenUpdatePullRequest:
        number = value.get("number")
        branch = value.get("headRefName", value.get("head_branch"))
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError("open pull request number must be an integer")
        if not isinstance(branch, str) or not branch:
            raise ValueError("open pull request head branch is required")
        return cls(
            number=number,
            head_branch=branch,
            head_sha=str(value.get("headRefOid", value.get("head_sha")) or ""),
            title=str(value.get("title") or ""),
            body=str(value.get("body") or ""),
        )

    @property
    def fingerprint(self) -> str | None:
        match = _FINGERPRINT_PATTERN.search(self.body)
        return match.group("value") if match else None

    @property
    def identities(self) -> frozenset[str]:
        match = _IDENTITIES_PATTERN.search(self.body)
        if match is None:
            return frozenset()
        try:
            values = json.loads(match.group("value"))
        except json.JSONDecodeError:
            return frozenset()
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            return frozenset()
        return frozenset(value.lower() for value in values)


@dataclass(frozen=True)
class PolicySkip:
    identities: tuple[str, ...]
    reason: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UpdateProposal:
    name: str
    candidates: tuple[UpdateCandidate, ...]
    branch: str
    fingerprint: str
    action: str
    existing_pull_request: int | None = None
    existing_head_sha: str = ""

    @property
    def identities(self) -> tuple[str, ...]:
        return tuple(candidate.identity for candidate in self.candidates)

    def to_dict(self, validation: ValidationConfig) -> dict[str, Any]:
        return {
            "name": self.name,
            "branch": self.branch,
            "fingerprint": self.fingerprint,
            "action": self.action,
            "existing_pull_request": self.existing_pull_request,
            "existing_head_sha": self.existing_head_sha,
            "title": proposal_title(self),
            "body": proposal_body(self, validation),
            "updates": [candidate.to_dict() for candidate in self.candidates],
            "changed_files": sorted(
                {
                    path
                    for candidate in self.candidates
                    for path in candidate.source_files
                }
            ),
        }


@dataclass(frozen=True)
class UpdatePlan:
    proposals: tuple[UpdateProposal, ...]
    skipped: tuple[PolicySkip, ...]
    schedule_due: bool
    managed_open_pull_requests: int

    def to_dict(self, validation: ValidationConfig) -> dict[str, Any]:
        return {
            "proposals": [
                proposal.to_dict(validation) for proposal in self.proposals
            ],
            "skipped": [item.to_dict() for item in self.skipped],
            "schedule_due": self.schedule_due,
            "managed_open_pull_requests": self.managed_open_pull_requests,
        }


def plan_updates(
    candidates: tuple[UpdateCandidate, ...],
    policy: UpdatePolicyConfig,
    *,
    now: datetime,
    open_pull_requests: tuple[OpenUpdatePullRequest, ...] = (),
    ignore_schedule: bool = False,
) -> UpdatePlan:
    active_now = _aware_utc(now)
    managed_open = tuple(
        pull_request
        for pull_request in open_pull_requests
        if pull_request.head_branch.startswith(UPDATE_BRANCH_PREFIX)
    )
    due = ignore_schedule or schedule_due(policy, active_now)
    if not due:
        return UpdatePlan(
            proposals=(),
            skipped=(
                PolicySkip(
                    identities=tuple(
                        candidate.identity
                        for candidate in sorted(
                            candidates, key=_candidate_sort_key
                        )
                    ),
                    reason="schedule_not_due",
                    detail="The configured update schedule is not due.",
                ),
            ),
            schedule_due=False,
            managed_open_pull_requests=len(managed_open),
        )

    eligible: list[UpdateCandidate] = []
    skipped: list[PolicySkip] = []
    for candidate in sorted(candidates, key=_candidate_sort_key):
        rejection = _policy_rejection(candidate, policy, active_now)
        if rejection is None:
            eligible.append(candidate)
        else:
            reason, detail = rejection
            skipped.append(
                PolicySkip(
                    identities=(candidate.identity,),
                    reason=reason,
                    detail=detail,
                )
            )

    grouped: dict[str, list[UpdateCandidate]] = {}
    group_names: dict[str, str] = {}
    for candidate in eligible:
        group_key, group_name = _group_for(candidate, policy)
        grouped.setdefault(group_key, []).append(candidate)
        group_names[group_key] = group_name

    existing_by_branch = {
        pull_request.head_branch: pull_request
        for pull_request in sorted(managed_open, key=lambda item: item.number)
    }
    capacity = max(0, policy.max_open_pull_requests - len(managed_open))
    proposals: list[UpdateProposal] = []
    for group_key in sorted(
        grouped,
        key=lambda value: (0 if value.startswith("group:") else 1, value),
    ):
        grouped_candidates = tuple(
            sorted(grouped[group_key], key=_candidate_sort_key)
        )
        fingerprint = _proposal_fingerprint(grouped_candidates)
        branch = _proposal_branch(group_key)
        identities = frozenset(
            candidate.identity.lower() for candidate in grouped_candidates
        )
        existing = existing_by_branch.get(branch)

        if existing is not None and existing.fingerprint == fingerprint:
            skipped.append(
                PolicySkip(
                    identities=tuple(sorted(identities)),
                    reason="duplicate_open_pull_request",
                    detail=(
                        f"Pull request #{existing.number} already proposes "
                        "this exact update."
                    ),
                )
            )
            continue

        other = next(
            (
                pull_request
                for pull_request in managed_open
                if pull_request.head_branch != branch
                and pull_request.identities.intersection(identities)
            ),
            None,
        )
        if other is not None:
            skipped.append(
                PolicySkip(
                    identities=tuple(sorted(identities)),
                    reason="dependency_has_open_update_pull_request",
                    detail=(
                        f"Pull request #{other.number} already manages at least "
                        "one dependency in this proposal."
                    ),
                )
            )
            continue

        if existing is None and capacity == 0:
            skipped.append(
                PolicySkip(
                    identities=tuple(sorted(identities)),
                    reason="maximum_open_pull_requests",
                    detail=(
                        "The configured maximum number of open Fraeno update "
                        "pull requests has been reached."
                    ),
                )
            )
            continue

        if existing is None:
            capacity -= 1
        proposals.append(
            UpdateProposal(
                name=group_names[group_key],
                candidates=grouped_candidates,
                branch=branch,
                fingerprint=fingerprint,
                action="refresh" if existing else "create",
                existing_pull_request=existing.number if existing else None,
                existing_head_sha=existing.head_sha if existing else "",
            )
        )

    return UpdatePlan(
        proposals=tuple(proposals),
        skipped=tuple(skipped),
        schedule_due=True,
        managed_open_pull_requests=len(managed_open),
    )


def schedule_due(policy: UpdatePolicyConfig, now: datetime) -> bool:
    schedule = policy.schedule
    if schedule.interval == "manual":
        return False
    if schedule.interval == "daily":
        return True
    if schedule.interval == "weekly":
        return now.strftime("%A").lower() == schedule.day
    if schedule.interval == "monthly":
        return now.day == schedule.day_of_month
    raise ValueError(f"unsupported update schedule: {schedule.interval}")


def update_type(candidate: UpdateCandidate) -> str:
    if candidate.ecosystem is Ecosystem.DOCKER and candidate.target.startswith(
        "sha256:"
    ):
        return "digest"
    if candidate.ecosystem is Ecosystem.GIT and re.fullmatch(
        r"[0-9a-fA-F]{40}", candidate.target
    ):
        return "revision"
    try:
        current = Version(candidate.current.removeprefix("v"))
        target = Version(candidate.target.removeprefix("v"))
    except InvalidVersion:
        return "unknown"
    current_release = (*current.release, 0, 0, 0)
    target_release = (*target.release, 0, 0, 0)
    if target_release[0] != current_release[0]:
        return "major"
    if target_release[1] != current_release[1]:
        return "minor"
    return "patch"


def proposal_title(proposal: UpdateProposal) -> str:
    if len(proposal.candidates) == 1:
        candidate = proposal.candidates[0]
        return f"Update {candidate.identity} to {candidate.target}"
    return f"Update {proposal.name} dependencies"


def proposal_body(
    proposal: UpdateProposal,
    validation: ValidationConfig,
) -> str:
    identities = json.dumps(list(proposal.identities), separators=(",", ":"))
    lines = [
        f"<!-- fraeno-update-fingerprint: {proposal.fingerprint} -->",
        f"<!-- fraeno-update-identities: {identities} -->",
        "",
        "## What changed",
        "",
    ]
    for candidate in proposal.candidates:
        lines.append(
            f"- `{candidate.identity}` from `{candidate.current}` "
            f"to `{candidate.target}`"
        )

    lines.extend(["", "## Manifests", ""])
    for path in sorted(
        {
            path
            for candidate in proposal.candidates
            for path in candidate.source_files
        }
    ):
        lines.append(f"- `{path}`")

    lines.extend(["", "## Resolution evidence", ""])
    for candidate in proposal.candidates:
        lines.append(
            f"- `{candidate.identity}` uses {update_type(candidate)} update "
            f"evidence published on {_plain(candidate.release_date)}."
        )
        for provenance in candidate.provenance:
            lines.append(
                f"  - {_plain(provenance.evidence)} "
                f"[Source]({_safe_link(provenance.source)})"
            )

    lines.extend(["", "## Validation scope", ""])
    if validation.steps:
        for step in validation.steps:
            lines.append(f"- Run the trusted `{_inline(step.name)}` step.")
    else:
        lines.append("- No build or static command is configured.")
    lines.append(
        "- Run the trusted observation command for matching baseline and "
        "candidate systems."
    )
    contract_entities = (
        len(validation.required_nodes)
        + len(validation.required_topics)
        + len(validation.required_services)
        + len(validation.required_actions)
    )
    lines.append(
        f"- Check {contract_entities} required graph entities, configured topic "
        "rates, QoS, transforms, and diagnostics."
    )

    lines.extend(["", "## Missing evidence", ""])
    missing = _missing_evidence(proposal)
    lines.extend(f"- {item}" for item in missing)
    lines.extend(
        [
            "",
            "Fraeno will block this pull request if the trusted robot integration "
            "check fails or does not produce complete evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def _policy_rejection(
    candidate: UpdateCandidate,
    policy: UpdatePolicyConfig,
    now: datetime,
) -> tuple[str, str] | None:
    identity = candidate.identity.lower()
    kind = update_type(candidate)
    if candidate.ecosystem not in {
        Ecosystem.PYTHON,
        Ecosystem.DOCKER,
        Ecosystem.APT,
        Ecosystem.GIT,
    }:
        return (
            "not_automatically_managed",
            "Fraeno can observe this dependency but cannot rewrite it safely yet.",
        )
    if kind not in policy.update_types:
        return (
            "update_type_not_allowed",
            f"{kind} updates are not enabled by the repository policy.",
        )

    matching_allow = _matching_rules(policy.allow, identity)
    if policy.allow and not matching_allow:
        return (
            "not_allowed",
            "The dependency does not match an allow rule.",
        )
    if matching_allow and not any(_rule_allows(rule, kind) for rule in matching_allow):
        return (
            "update_type_not_allowed",
            f"No matching allow rule permits {kind} updates.",
        )

    matching_ignore = _matching_rules(policy.ignore, identity)
    if any(_rule_allows(rule, kind) for rule in matching_ignore):
        return (
            "ignored",
            "A matching ignore rule excludes this update.",
        )

    cooldown_days = max(
        [
            policy.cooldown_days,
            *[
                rule.cooldown_days
                for rule in matching_allow
                if rule.cooldown_days is not None
            ],
        ]
    )
    if cooldown_days:
        released_at = _release_date(candidate.release_date)
        if released_at is None:
            return (
                "cooldown_unverifiable",
                "The release date is missing, so the cooldown cannot be proven.",
            )
        age_seconds = (now - released_at).total_seconds()
        if age_seconds < cooldown_days * 86_400:
            return (
                "cooldown_active",
                f"The release is younger than the required {cooldown_days}-day cooldown.",
            )
    return None


def _matching_rules(
    rules: tuple[UpdateRuleConfig, ...],
    identity: str,
) -> tuple[UpdateRuleConfig, ...]:
    return tuple(
        rule for rule in rules if fnmatchcase(identity, rule.dependency)
    )


def _rule_allows(rule: UpdateRuleConfig, kind: str) -> bool:
    return not rule.update_types or kind in rule.update_types


def _group_for(
    candidate: UpdateCandidate,
    policy: UpdatePolicyConfig,
) -> tuple[str, str]:
    identity = candidate.identity.lower()
    for group in policy.groups:
        if any(fnmatchcase(identity, pattern) for pattern in group.patterns):
            return f"group:{group.name.lower()}", group.name
    return f"dependency:{identity}", candidate.identity


def _proposal_fingerprint(candidates: tuple[UpdateCandidate, ...]) -> str:
    payload = [
        {
            "identity": candidate.identity.lower(),
            "current": candidate.current,
            "target": candidate.target,
            "source_files": list(candidate.source_files),
        }
        for candidate in candidates
    ]
    rendered = json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(rendered).hexdigest()


def _proposal_branch(group_key: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", group_key.lower()).strip("-")
    slug = slug[:48].rstrip("-") or "dependencies"
    suffix = hashlib.sha256(group_key.encode()).hexdigest()[:8]
    return f"{UPDATE_BRANCH_PREFIX}{slug}-{suffix}"


def _candidate_sort_key(candidate: UpdateCandidate) -> tuple[str, str]:
    return candidate.ecosystem.value, candidate.name.lower()


def _release_date(value: str) -> datetime | None:
    if not value or value.lower() == "unknown":
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware_utc(parsed)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _missing_evidence(proposal: UpdateProposal) -> list[str]:
    missing: list[str] = []
    for candidate in proposal.candidates:
        if candidate.release_date.lower() == "unknown":
            missing.append(
                f"`{candidate.identity}` has no verified release date."
            )
        if not candidate.provenance:
            missing.append(
                f"`{candidate.identity}` has no registry provenance."
            )
    missing.append(
        "Physical hardware behavior is not covered unless the repository's "
        "trusted workflow runs on hardware."
    )
    return missing


def _plain(value: str) -> str:
    return " ".join(value.replace("\x00", "").split())


def _inline(value: str) -> str:
    return _plain(value).replace("`", "'")


def _safe_link(value: str) -> str:
    cleaned = _plain(value)
    if cleaned.startswith(("https://", "http://")):
        return cleaned.replace(")", "%29")
    return "#"
