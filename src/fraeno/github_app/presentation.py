from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CheckPresentation:
    title: str
    summary: str
    conclusion: str


def present_validation(
    report: dict[str, Any] | None,
    *,
    change: str,
    workflow_conclusion: str,
) -> CheckPresentation:
    level = _validation_level(report)
    prefix = f"**Change** {_plain(change)}\n\n**Validation** {level}"
    if (
        report is not None
        and report.get("outcome") == "pass"
        and workflow_conclusion == "success"
    ):
        return CheckPresentation(
            title="Robot integration validation passed",
            summary=(
                f"{prefix}\n\nThe configured robot system remained healthy "
                "after this change."
            ),
            conclusion="success",
        )

    category = _failure_category(report, workflow_conclusion)
    findings = _concise_findings(report)
    details = "\n".join(f"- {finding}" for finding in findings)
    if not details:
        details = "- No complete Fraeno report was produced."
    return CheckPresentation(
        title=f"{category} blocked this change",
        summary=f"{prefix}\n\n**Result** {category}\n\n{details}",
        conclusion="failure",
    )


def _failure_category(
    report: dict[str, Any] | None,
    workflow_conclusion: str,
) -> str:
    if report is None:
        return "Infrastructure failure"
    findings = _report_findings(report)
    if any(_is_unsupported(finding) for finding in findings):
        return "Unsupported evidence"
    outcome = report.get("outcome")
    if outcome == "error":
        return "Configuration failure"
    if outcome == "block":
        return "Robot regression"
    if workflow_conclusion != "success":
        return "Infrastructure failure"
    return "Configuration failure"


def _validation_level(report: dict[str, Any] | None) -> str:
    if report is None:
        return "Not reached"
    comparison = report.get("comparison")
    if isinstance(comparison, dict):
        value = comparison.get("validation_level")
        if isinstance(value, str) and value:
            return _plain(value)
    return "Not reached"


def _concise_findings(report: dict[str, Any] | None) -> list[str]:
    if report is None:
        return []
    rendered: list[str] = []
    for finding in _report_findings(report)[:5]:
        entity = finding.get("entity")
        message = finding.get("message")
        prefix = (
            f"`{_inline_code(entity)}` "
            if isinstance(entity, str) and entity
            else ""
        )
        if isinstance(message, str) and message:
            rendered.append(_truncate(prefix + _plain(message)))

    if rendered:
        remaining = len(_report_findings(report)) - len(rendered)
        if remaining > 0:
            rendered.append(f"{remaining} more finding{'s' if remaining != 1 else ''}")
        return rendered

    for phase in ("baseline", "candidate"):
        workspace = report.get(phase)
        if not isinstance(workspace, dict):
            continue
        error = workspace.get("error")
        if isinstance(error, str) and error:
            rendered.append(f"{phase.capitalize()} {_truncate(_plain(error))}")
    return rendered[:5]


def _report_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    comparison = report.get("comparison")
    if not isinstance(comparison, dict):
        return []
    findings = comparison.get("findings", [])
    if not isinstance(findings, list):
        return []
    return [finding for finding in findings if isinstance(finding, dict)]


def _is_unsupported(finding: dict[str, Any]) -> bool:
    code = str(finding.get("code", "")).lower()
    message = str(finding.get("message", "")).lower()
    return "unsupported" in code or "unsupported" in message


def _plain(value: str) -> str:
    return " ".join(value.replace("\x00", "").split())


def _inline_code(value: str) -> str:
    return _plain(value).replace("`", "'")


def _truncate(value: str, limit: int = 240) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"
