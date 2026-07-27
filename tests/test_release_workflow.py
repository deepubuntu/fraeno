from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "publish-runner.yml"
CONTROL_RELEASE_WORKFLOW = (
    ROOT / ".github" / "workflows" / "release-control-plane.yml"
)
CONTROL_IMAGE = (
    "us-central1-docker.pkg.dev/deepubuntu-32f9e/"
    "fraeno-control-plane/control-plane"
)
CONTROL_COMMIT = "a" * 40
CONTROL_DIGEST = "sha256:" + ("b" * 64)
SECOND_CONTROL_DIGEST = "sha256:" + ("c" * 64)
CONTROL_PACKAGE_ROOT = (
    "projects/deepubuntu-32f9e/locations/us-central1/repositories/"
    "fraeno-control-plane/packages/control-plane"
)
CONTROL_TAG_ROOT = f"{CONTROL_PACKAGE_ROOT}/tags"
CONTROL_VERSION_ROOT = f"{CONTROL_PACKAGE_ROOT}/versions"


def _registry_tag_resolver_source() -> str:
    workflow = CONTROL_RELEASE_WORKFLOW.read_text()
    block = workflow.split("# REGISTRY_TAG_RESOLVER_START", maxsplit=1)[1].split(
        "# REGISTRY_TAG_RESOLVER_END",
        maxsplit=1,
    )[0]
    source = block.split("<<'PY'\n", maxsplit=1)[1].rsplit(
        "\n          PY",
        maxsplit=1,
    )[0]
    return textwrap.dedent(source)


def _run_registry_tag_resolver(
    tmp_path: Path,
    inventory: object,
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    inventory_path = tmp_path / "inventory.json"
    semantic_path = tmp_path / "semantic.txt"
    commit_path = tmp_path / "commit.txt"
    inventory_path.write_text(json.dumps(inventory))
    result = subprocess.run(
        [
            sys.executable,
            "-",
            CONTROL_IMAGE,
            CONTROL_TAG_ROOT,
            "v0.2.0",
            CONTROL_COMMIT,
            str(inventory_path),
            str(semantic_path),
            str(commit_path),
        ],
        input=_registry_tag_resolver_source(),
        text=True,
        capture_output=True,
        check=False,
    )
    semantic = semantic_path.read_text().strip() if semantic_path.exists() else ""
    commit = commit_path.read_text().strip() if commit_path.exists() else ""
    return result, semantic, commit


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
    external_runner_test = (
        ROOT / "tests" / "container" / "test-external-runner.sh"
    ).read_text()

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
    exact_runner_step = workflow[
        workflow.index("Build and test the exact runner") :
        workflow.index("Require every release check")
    ]
    rollback_step = workflow[
        workflow.index("Test the protected rollback image") :
        workflow.index("Set up Buildx for attestations")
    ]
    assert '"$RELEASE_VERSION"' in exact_runner_step
    assert 'rollback_version="$(' in rollback_step
    assert '"$rollback_version"' in rollback_step
    assert "Protected rollback image has no semantic version label." in rollback_step
    assert "org.opencontainers.image.version" in external_runner_test
    assert 'version": sys.argv[2]' in external_runner_test
    assert 'version": "0.2.0"' not in external_runner_test
    assert 'release_tree="$(git rev-parse "$RELEASE_SHA^{tree}")"' in workflow
    assert 'if [[ "$reviewed_tree" != "$release_tree" ]]' in workflow
    assert "--reviewed-input reviewed-check-runs.json" in workflow
    assert "reviewed-pr.json" in workflow
    assert "retention-days: 90" in workflow


def test_every_runner_related_action_is_pinned_to_a_commit() -> None:
    workflow_paths = [
        *sorted((ROOT / ".github" / "workflows").glob("*.yml")),
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


def test_action_comments_match_the_pinned_releases() -> None:
    expected_versions = {
        "3d3c42e5aac5ba805825da76410c181273ba90b1": "v7.0.1",
        "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a": "v7.0.1",
        "ece7cb06caefa5fff74198d8649806c4678c61a1": "v6",
        "7c6bc770dae815cd3e89ee6cdf493a5fab2cc093": "v3",
        "aa5489c8933f4cc7a4f7d45035b3b1440c9c10db": "v3",
        "bb05f3f5519dd87d3ba754cc423b652a5edd6d2c": "v4.2.0",
    }
    workflow_paths = [
        *sorted((ROOT / ".github" / "workflows").glob("*.yml")),
        *sorted((ROOT / "templates" / "github").glob("*.yml")),
    ]
    for path in workflow_paths:
        for line in path.read_text().splitlines():
            match = re.search(r"\buses:\s*\S+@([0-9a-f]{40})\s+#\s+(\S+)", line)
            if match:
                sha, version = match.groups()
                assert expected_versions.get(sha) == version, (
                    f"{path} labels {sha} as {version}"
                )


def test_runner_image_records_the_release_commit() -> None:
    dockerfile = (ROOT / "runner" / "Dockerfile").read_text()

    assert "ARG FRAENO_REVISION=unknown" in dockerfile
    assert 'org.opencontainers.image.revision="${FRAENO_REVISION}"' in dockerfile


def test_control_release_is_manual_keyless_and_exactly_scoped() -> None:
    workflow = CONTROL_RELEASE_WORKFLOW.read_text()

    assert "workflow_dispatch:" in workflow
    triggers = workflow[: workflow.index("permissions:")]
    assert re.search(r"^\s{2}(push|release):", triggers, re.MULTILINE) is None
    assert "environment: control-plane-production" in workflow
    assert "credentials_json" not in workflow
    assert "EXPECTED_GCP_REPOSITORY: fraeno-control-plane" in workflow
    assert "fraeno-control-plane-releaser@" in workflow
    assert "fraeno-github/providers/fraeno-control-plane" in workflow
    assert "WEBHOOK_SERVICE: fraeno-github-webhook" in workflow
    assert "WORKER_SERVICE: fraeno-github-worker" in workflow
    assert workflow.index("Require every release check") < workflow.index(
        "Authenticate to Google Cloud"
    )


def test_control_release_requires_same_commit_runner_evidence() -> None:
    workflow = CONTROL_RELEASE_WORKFLOW.read_text()

    assert "publish-runner.yml/runs" in workflow
    assert 'run.get("head_sha") == commit' in workflow
    assert 'run.get("display_title") == title' in workflow
    assert "sha256sum --check fraeno-runner-release.json.sha256" in workflow
    assert '"$RUNNER_IMAGE:v$RELEASE_VERSION"' in workflow
    assert '"$RUNNER_IMAGE:$RELEASE_SHA"' in workflow
    assert "runner_semantic_digest" in workflow
    assert "RUNNER_EVIDENCE_DIGEST" in workflow
    assert "resume_run_id:" in workflow
    assert "resume-control-plane-evidence" in workflow
    assert "sha256sum --check fraeno-control-plane-release.json.sha256" in workflow
    assert "Existing tag does not match proven resume evidence." in workflow


def test_control_release_lists_tags_without_parsing_error_messages() -> None:
    workflow = CONTROL_RELEASE_WORKFLOW.read_text()

    assert workflow.count("gcloud artifacts docker tags list") == 2
    assert "--include-tags" not in workflow
    assert "--format json > control-plane-tags.json" in workflow
    assert "describe_optional_digest" not in workflow
    assert 'grep --quiet "NOT_FOUND"' not in workflow
    assert "semantic_digest=\"$(<semantic-tag-digest.txt)\"" in workflow
    assert "commit_digest=\"$(<commit-tag-digest.txt)\"" in workflow
    finalize = workflow[workflow.index("Finalize immutable release tags") :]
    assert "refresh_tag_digests" in finalize
    assert finalize.count("refresh_tag_digests") == 3
    assert "images describe" not in finalize
    assert "|| true" not in finalize
    assert finalize.index(
        'require_tag_available "$RELEASE_SHA" "$commit_digest"'
    ) < finalize.index(
        'add_tag_if_absent "$RELEASE_SHA" "$commit_digest"'
    )
    assert finalize.index(
        'require_tag_available "v$RELEASE_VERSION" "$semantic_digest"'
    ) < finalize.index(
        'add_tag_if_absent "v$RELEASE_VERSION" "$semantic_digest"'
    )
    assert finalize.index(
        'require_tag_available "v$RELEASE_VERSION" "$semantic_digest"'
    ) < finalize.index(
        'add_tag_if_absent "$RELEASE_SHA" "$commit_digest"'
    )


def test_registry_tag_resolver_treats_an_empty_inventory_as_a_new_release(
    tmp_path: Path,
) -> None:
    result, semantic, commit = _run_registry_tag_resolver(tmp_path, [])

    assert result.returncode == 0, result.stderr
    assert semantic == ""
    assert commit == ""


def test_registry_tag_resolver_returns_exact_structured_digests(
    tmp_path: Path,
) -> None:
    inventory = [
        {
            "tag": f"{CONTROL_TAG_ROOT}/v0.2.0-near-match",
            "image": CONTROL_IMAGE,
            "version": f"{CONTROL_VERSION_ROOT}/{SECOND_CONTROL_DIGEST}",
        },
        {
            "tag": f"{CONTROL_TAG_ROOT}/v0.2.0",
            "image": CONTROL_IMAGE,
            "version": f"{CONTROL_VERSION_ROOT}/{CONTROL_DIGEST}",
        },
        {
            "tag": f"{CONTROL_TAG_ROOT}/{CONTROL_COMMIT}",
            "image": CONTROL_IMAGE,
            "version": f"{CONTROL_VERSION_ROOT}/{CONTROL_DIGEST}",
        },
    ]

    result, semantic, commit = _run_registry_tag_resolver(tmp_path, inventory)

    assert result.returncode == 0, result.stderr
    assert semantic == CONTROL_DIGEST
    assert commit == CONTROL_DIGEST


@pytest.mark.parametrize("field", ["tag", "image", "version"])
@pytest.mark.parametrize("value", ["missing", None, 17])
def test_registry_tag_resolver_rejects_missing_or_non_string_fields(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    item: dict[str, object] = {
        "tag": f"{CONTROL_TAG_ROOT}/unrelated",
        "image": CONTROL_IMAGE,
        "version": f"{CONTROL_VERSION_ROOT}/{CONTROL_DIGEST}",
    }
    if value == "missing":
        item.pop(field)
    else:
        item[field] = value

    result, _, _ = _run_registry_tag_resolver(tmp_path, [item])

    assert result.returncode != 0
    assert "must contain string tag, image, and version fields" in result.stderr


@pytest.mark.parametrize(
    ("inventory", "message"),
    [
        ({}, "inventory must be a JSON list"),
        ([None], "inventory contains a non-object"),
        (
            [
                {
                    "tag": f"{CONTROL_TAG_ROOT}/v0.2.0",
                    "image": "us-central1-docker.pkg.dev/other/repository/image",
                    "version": f"{CONTROL_VERSION_ROOT}/{CONTROL_DIGEST}",
                }
            ],
            "returned another image",
        ),
        (
            [
                {
                    "tag": f"{CONTROL_TAG_ROOT}/v0.2.0",
                    "image": CONTROL_IMAGE,
                    "version": (
                        "projects/other/locations/us-central1/repositories/"
                        f"repository/packages/control-plane/versions/{CONTROL_DIGEST}"
                    ),
                }
            ],
            "returned an invalid version",
        ),
        (
            [
                {
                    "tag": f"{CONTROL_TAG_ROOT}/v0.2.0",
                    "image": CONTROL_IMAGE,
                    "version": f"{CONTROL_VERSION_ROOT}/not-a-digest",
                }
            ],
            "returned an invalid digest",
        ),
        (
            [
                {
                    "tag": f"{CONTROL_TAG_ROOT}/v0.2.0",
                    "image": CONTROL_IMAGE,
                    "version": f"{CONTROL_VERSION_ROOT}/{CONTROL_DIGEST}",
                },
                {
                    "tag": f"{CONTROL_TAG_ROOT}/v0.2.0",
                    "image": CONTROL_IMAGE,
                    "version": (
                        f"{CONTROL_VERSION_ROOT}/{SECOND_CONTROL_DIGEST}"
                    ),
                },
            ],
            "returned more than one digest",
        ),
    ],
)
def test_registry_tag_resolver_fails_closed_on_invalid_inventory(
    tmp_path: Path,
    inventory: object,
    message: str,
) -> None:
    result, _, _ = _run_registry_tag_resolver(tmp_path, inventory)

    assert result.returncode != 0
    assert message in result.stderr


def test_control_release_proves_rollback_and_restores_candidate() -> None:
    workflow = CONTROL_RELEASE_WORKFLOW.read_text()

    state_position = workflow.index(
        "Require immutable repository and exact production state"
    )
    publish_position = workflow.index(
        "Publish or resume immutable release candidate"
    )
    token_position = workflow.index("Mint the private worker smoke token")
    deploy_position = workflow.index(
        "Deploy, prove rollback, and restore the release"
    )
    assert (
        state_position < publish_position < token_position < deploy_position
    )
    token_step = workflow[token_position:deploy_position]
    assert (
        "google-github-actions/auth@"
        "7c6bc770dae815cd3e89ee6cdf493a5fab2cc093" in token_step
    )
    assert "token_format: id_token" in token_step
    assert (
        "id_token_audience: "
        "${{ steps.production-state.outputs.worker_audience }}"
        in token_step
    )
    assert "create_credentials_file: false" in token_step
    assert "export_environment_variables: false" in token_step
    assert "gcloud auth print-identity-token" not in workflow
    deploy_step = workflow[deploy_position:]
    assert (
        "PRIVATE_WORKER_ID_TOKEN: "
        "${{ steps.worker-token.outputs.id_token }}" in deploy_step
    )
    assert 'test "$audience" = "$WORKER_AUDIENCE"' in deploy_step
    assert 'test -n "$PRIVATE_WORKER_ID_TOKEN"' in deploy_step
    assert "worker service URL is not a canonical run.app URL" in workflow
    assert 'parsed.hostname.startswith(f"{service}-")' in workflow
    candidate = 'set_traffic "$WORKER_SERVICE" "$worker_candidate_revision"'
    rollback = 'set_traffic "$WORKER_SERVICE" "$PREVIOUS_WORKER_REVISION"'
    assert workflow.count(candidate) == 2
    assert workflow.count(rollback) == 2
    traffic_changes = workflow[workflow.index('deploy_candidate "$WEBHOOK_SERVICE"') :]
    first_candidate = traffic_changes.index(candidate)
    rollback_position = traffic_changes.index(rollback)
    restored_candidate = traffic_changes.index(candidate, first_candidate + 1)
    assert first_candidate < rollback_position < restored_candidate
    assert "trap restore_previous_on_failure EXIT" in workflow
    assert "--remove-tags \"$traffic_tag\"" in workflow
    assert "--revision \"$webhook_candidate_revision\"" in workflow
    assert "--revision \"$worker_candidate_revision\"" in workflow
    assert 'revisions describe "$PREVIOUS_WEBHOOK_REVISION"' in workflow
    assert 'revisions describe "$PREVIOUS_WORKER_REVISION"' in workflow
    assert "--revision-input previous-webhook-revision.json" in workflow
    assert "--revision-input final-webhook-revision.json" in workflow
    assert "candidate_restored\": True" in workflow
    assert "retention-days: 90" in workflow


def test_github_release_can_run_only_after_production_evidence() -> None:
    workflow = CONTROL_RELEASE_WORKFLOW.read_text()

    publish_job = workflow.index("publish-github-release:")
    artifact_step = workflow.index("Preserve deployment and rollback evidence")
    tag_step = workflow.index("Finalize immutable release tags")
    cleanup_step = workflow.index(
        "Restore previous production after a failed release"
    )
    assert artifact_step < publish_job
    assert artifact_step < tag_step < cleanup_step < publish_job
    assert "failure() &&" in workflow
    assert "steps.gcp-auth.outcome == 'success' &&" in workflow
    assert "steps.deploy.outcome != 'skipped'" in workflow
    assert (
        '--to-revisions "$PREVIOUS_WEBHOOK_REVISION=100"'
        in workflow[cleanup_step:publish_job]
    )
    assert (
        '--to-revisions "$PREVIOUS_WORKER_REVISION=100"'
        in workflow[cleanup_step:publish_job]
    )
    assert "needs: release" in workflow[publish_job:]
    assert "contents: write" in workflow[publish_job:]
    assert "id-token: write" not in workflow[publish_job:]
    assert 'gh release create "v$RELEASE_VERSION"' in workflow[publish_job:]
    assert '--target "$RELEASE_SHA"' in workflow[publish_job:]
    assert "verify_release_tag" in workflow[publish_job:]
