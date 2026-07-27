from typing import Any

import pytest

from fraeno.github_app.presentation import present_validation


@pytest.mark.parametrize(
    ("report", "workflow_conclusion", "expected_title"),
    [
        (
            {
                "outcome": "error",
                "baseline": {"error": "Invalid observation"},
                "candidate": {"error": None},
                "comparison": None,
            },
            "failure",
            "Configuration failure blocked this change",
        ),
        (
            None,
            "failure",
            "Infrastructure failure blocked this change",
        ),
        (
            {
                "outcome": "block",
                "baseline": {"error": None},
                "candidate": {"error": None},
                "comparison": {
                    "validation_level": "L1",
                    "findings": [
                        {
                            "code": "unsupported-hardware-evidence",
                            "entity": "camera",
                            "message": "Hardware evidence is unsupported on this runner.",
                        }
                    ],
                },
            },
            "failure",
            "Unsupported evidence blocked this change",
        ),
    ],
)
def test_failure_categories_are_distinct(
    report: dict[str, Any] | None,
    workflow_conclusion: str,
    expected_title: str,
) -> None:
    presentation = present_validation(
        report,
        change="Update the sensor driver",
        workflow_conclusion=workflow_conclusion,
    )

    assert presentation.title == expected_title
    assert presentation.conclusion == "failure"
    assert "**Change** Update the sensor driver" in presentation.summary
