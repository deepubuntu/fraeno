from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "publish-runner.yml"


def test_release_workflow_is_manual_and_minimally_privileged() -> None:
    workflow = RELEASE_WORKFLOW.read_text()

    assert "workflow_dispatch:" in workflow
    assert re.search(r"^\s{2}(push|release):", workflow, re.MULTILINE) is None
    assert "environment: runner-production" in workflow
    assert "actions: read" in workflow
    assert "checks: read" in workflow
    assert "contents: read" in workflow
    assert "id-token: write" in workflow
    assert "pull-requests: read" in workflow
    assert "contents: write" not in workflow


def test_release_workflow_uses_wif_and_direct_artifact_registry() -> None:
    workflow = RELEASE_WORKFLOW.read_text()

    assert "google-github-actions/auth@" in workflow
    assert "workload_identity_provider:" in workflow
    assert "service_account:" in workflow
    assert "credentials_json" not in workflow
    assert "GCP_ARTIFACT_REGISTRY_REPOSITORY" in workflow
    assert "EXPECTED_GCP_PROJECT_ID: deepubuntu-32f9e" in workflow
    assert "EXPECTED_GCP_LOCATION: us-central1" in workflow
    assert "EXPECTED_GCP_REPOSITORY: fraeno-runner" in workflow
    assert (
        "EXPECTED_GCP_WORKLOAD_IDENTITY_PROVIDER: "
        "projects/286435890377/locations/global/workloadIdentityPools/"
        "fraeno-github/providers/fraeno-runner"
    ) in workflow
    assert (
        "EXPECTED_GCP_RELEASE_SERVICE_ACCOUNT: "
        "fraeno-runner-publisher@deepubuntu-32f9e.iam.gserviceaccount.com"
    ) in workflow
    assert 'GCP_REPOSITORY" != "$EXPECTED_GCP_REPOSITORY' in workflow
    assert (
        'GCP_WORKLOAD_IDENTITY_PROVIDER" != '
        "\\\n            \"$EXPECTED_GCP_WORKLOAD_IDENTITY_PROVIDER\""
    ) in workflow
    assert (
        'GCP_RELEASE_SERVICE_ACCOUNT" != '
        "\\\n            \"$EXPECTED_GCP_RELEASE_SERVICE_ACCOUNT\""
    ) in workflow
    assert "-docker.pkg.dev" in workflow
    assert "supabase" not in workflow.lower()
    assert "render" not in workflow.lower()
    assert "vercel" not in workflow.lower()


def test_release_docs_name_only_the_public_runner_repository() -> None:
    release_docs = (ROOT / "docs" / "releases.md").read_text()

    assert "us-central1-docker.pkg.dev/deepubuntu-32f9e/fraeno-runner" in release_docs
    assert "`us-central1`, and `fraeno-runner`" in release_docs
    assert "update fraeno-runner" in release_docs
    assert "private mixed `fraeno`" in release_docs
    assert "`deepubuntu/fraeno`" in release_docs
    assert "`refs/heads/main`" in release_docs
    assert "`Publish immutable Fraeno runner`" in release_docs
    assert "`workflow_dispatch`" in release_docs
    assert "job_workflow_ref" not in release_docs


def test_release_workflow_tests_and_gates_before_publish() -> None:
    workflow = RELEASE_WORKFLOW.read_text()

    python_position = workflow.index("Set up Python")
    identity_position = workflow.index("Validate release identity")
    test_position = workflow.index("Test the exact release commit")
    container_position = workflow.index("Build and test the exact runner")
    gate_position = workflow.index("Require every release check")
    auth_position = workflow.index("Authenticate to Google Cloud")
    buildx_position = workflow.index("Set up Buildx for attestations")
    publish_position = workflow.index("Publish immutable runner")
    assert 'python-version: "3.11"' in workflow
    assert python_position < identity_position
    assert test_position < auth_position
    assert container_position < auth_position
    assert gate_position < auth_position
    assert "driver: docker-container" in workflow
    assert auth_position < publish_position
    assert buildx_position < publish_position
    for check in (
        "Fraeno / robot integration",
        "container",
        "ros-integration",
        "test",
    ):
        assert check in (ROOT / "src" / "fraeno" / "release_safety.py").read_text()


def test_release_workflow_publishes_only_create_once_tags_and_digest_evidence() -> None:
    workflow = RELEASE_WORKFLOW.read_text()

    assert '--tag "$RUNNER_IMAGE:v$RELEASE_VERSION"' in workflow
    assert '--tag "$RUNNER_IMAGE:$RELEASE_SHA"' in workflow
    assert ":latest" not in workflow
    assert "--sbom=true" in workflow
    assert "--provenance=mode=max" in workflow
    assert "--metadata-file buildkit-metadata.json" in workflow
    assert "image_summary.digest" in workflow
    assert "sha256sum fraeno-runner-release.json" in workflow
    assert "PREVIOUS_DIGEST" in workflow
    assert "tests/container/test-external-runner.sh" in workflow
    assert 'release_tree="$(git rev-parse "$RELEASE_SHA^{tree}")"' in workflow
    assert 'if [[ "$reviewed_tree" != "$release_tree" ]]' in workflow
    assert "--reviewed-input reviewed-check-runs.json" in workflow
    assert "reviewed-pr.json" in workflow
    assert "retention-days: 90" in workflow


def test_every_runner_related_action_is_pinned_to_a_commit() -> None:
    workflow_paths = [
        ROOT / ".github" / "workflows" / "ci.yml",
        ROOT / ".github" / "workflows" / "fraeno-updates.yml",
        ROOT / ".github" / "workflows" / "fraeno-validation.yml",
        RELEASE_WORKFLOW,
        *sorted((ROOT / "templates" / "github").glob("*.yml")),
    ]
    references: list[tuple[Path, str]] = []
    for path in workflow_paths:
        for line in path.read_text().splitlines():
            match = re.search(r"\buses:\s*\S+@([^ ]+)", line)
            if match:
                references.append((path, match.group(1)))

    assert references
    for path, reference in references:
        assert re.fullmatch(r"[0-9a-f]{40}", reference), (
            f"{path} uses mutable action reference {reference}"
        )


def test_runner_image_records_the_release_commit() -> None:
    dockerfile = (ROOT / "runner" / "Dockerfile").read_text()

    assert "ARG FRAENO_REVISION=unknown" in dockerfile
    assert 'org.opencontainers.image.revision="${FRAENO_REVISION}"' in dockerfile
