from pathlib import Path


def test_validation_workflow_checks_out_fork_head_explicitly() -> None:
    workflow = Path(".github/workflows/fraeno-validation.yml").read_text()

    assert "base_repository:" in workflow
    assert "head_repository:" in workflow
    assert "repository: ${{ inputs.base_repository }}" in workflow
    assert "repository: ${{ inputs.head_repository }}" in workflow
    assert workflow.count("persist-credentials: false") == 2
