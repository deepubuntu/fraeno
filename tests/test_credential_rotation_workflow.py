from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "verify-credential-rotation.yml"
CONTROL_RELEASE_WORKFLOW = (
    ROOT / ".github" / "workflows" / "release-control-plane.yml"
)
SETUP = ROOT / "scripts" / "setup_credential_rotation_gcp.sh"
PYTHON_COMMAND = re.compile(
    r"(?<![\w-])python(?:3(?:\.\d+)*)?\s+(?P<arguments>.*)"
)


def unisolated_python_commands(text: str) -> list[str]:
    failures: list[str] = []
    for line in text.splitlines():
        match = PYTHON_COMMAND.search(line)
        if match is None:
            continue
        arguments = match.group("arguments").split()
        if not arguments or arguments[0] != "-S":
            failures.append(line)
    return failures


def test_rotation_workflow_is_manual_keyless_and_defaults_to_dry_run() -> None:
    workflow = WORKFLOW.read_text()

    assert "workflow_dispatch:" in workflow
    assert "default: dry-run" in workflow
    assert "credentials_json" not in workflow
    assert "id-token: write" in workflow
    assert "environment: credential-rotation" in workflow
    assert "STAGE_FRAENO_ROTATION" in workflow
    assert "Stop after the metadata-only dry run" in workflow
    assert "needs: test" in workflow
    test_job = workflow[workflow.index("  test:") : workflow.index("  verify:")]
    privileged_job = workflow[workflow.index("  verify:") :]
    assert "id-token: write" not in test_job
    assert 'python -m pip install ".[dev,app]"' in test_job
    assert "id-token: write" in privileged_job
    assert "pip install" not in privileged_job
    privileged_python_calls = PYTHON_COMMAND.findall(privileged_job)
    assert privileged_python_calls
    assert unisolated_python_commands(privileged_job) == []
    assert "python -S -m fraeno.credential_rotation" in privileged_job
    assert "python -S -" in privileged_job


def test_python_isolation_guard_catches_versioned_interpreters() -> None:
    unsafe = "\n".join(
        [
            "python3 -m fraeno.credential_rotation inspect-source",
            "if python3.11 - <<'PY'",
        ]
    )

    assert unisolated_python_commands(unsafe) == unsafe.splitlines()
    assert (
        unisolated_python_commands(
            "python3 -S -m fraeno.credential_rotation inspect-source"
        )
        == []
    )


def test_rotation_and_release_share_one_non_canceling_production_lock() -> None:
    for workflow in (
        WORKFLOW.read_text(),
        CONTROL_RELEASE_WORKFLOW.read_text(),
    ):
        assert "group: fraeno-control-plane-production" in workflow
        assert "cancel-in-progress: false" in workflow


def test_rotation_workflow_pins_secret_versions_and_stages_without_traffic() -> None:
    workflow = WORKFLOW.read_text()

    state_position = workflow.index(
        "Require exact versions and unchanged production"
    )
    inspect_position = workflow.index("Inspect exact staged revisions")
    token_position = workflow.index(
        "Mint the private worker verification token"
    )
    verify_position = workflow.index(
        "Verify both staged webhook secrets and App keys"
    )
    assert state_position < inspect_position < token_position < verify_position
    token_step = workflow[token_position:verify_position]
    assert "if: inputs.mode == 'stage'" in token_step
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
    verification = workflow[verify_position:]
    assert (
        "FRAENO_WORKER_ID_TOKEN: "
        "${{ steps.worker-token.outputs.id_token }}" in verification
    )
    assert "private worker verification token is missing" in verification
    assert "worker service URL is not a canonical run.app URL" in workflow
    assert 'parsed.hostname.startswith(f"{service}-")' in workflow
    assert "--no-traffic" in workflow
    assert workflow.count("--no-traffic") == 3
    assert "FRAENO_GITHUB_WEBHOOK_SECRET_PREVIOUS" in workflow
    assert "FRAENO_GITHUB_PRIVATE_KEY_PREVIOUS" in workflow
    assert "FRAENO_CREDENTIAL_ROTATION_STARTED_AT" in workflow
    assert "FRAENO_PREVIOUS_CREDENTIALS_VALID_UNTIL" in workflow
    assert "gcloud secrets versions access" in workflow
    assert "--out-file" in workflow
    assert "PRIVATE_KEY_SECRET\" \\\n+            --project" not in workflow
    assert "probe-stage" in workflow


def test_rotation_candidates_clone_the_reviewed_active_revision_not_latest() -> None:
    workflow = WORKFLOW.read_text()

    source_gate = workflow[
        workflow.index("Require exact versions and unchanged production") :
        workflow.index("Stop after the metadata-only dry run")
    ]
    stage = workflow[
        workflow.index("Create tagged zero-traffic revisions") :
        workflow.index("Inspect exact staged revisions")
    ]
    inspection = workflow[
        workflow.index("Inspect exact staged revisions") :
        workflow.index("Verify both staged webhook secrets")
    ]

    assert 'revisions describe "$EXPECTED_WEBHOOK_REVISION"' in source_gate
    assert 'revisions describe "$EXPECTED_WORKER_REVISION"' in source_gate
    assert source_gate.count("fraeno.control_release inspect-service") == 2
    assert '--image "$WEBHOOK_ACTIVE_IMAGE"' in stage
    assert '--image "$WORKER_ACTIVE_IMAGE"' in stage
    assert '--service-account "$WEBHOOK_ACTIVE_SERVICE_ACCOUNT"' in stage
    assert '--service-account "$WORKER_ACTIVE_SERVICE_ACCOUNT"' in stage
    assert stage.count("assert_production_traffic") == 3
    assert '--webhook-image "$WEBHOOK_ACTIVE_IMAGE"' in inspection
    assert '--worker-image "$WORKER_ACTIVE_IMAGE"' in inspection
    assert (
        '--webhook-service-account "$WEBHOOK_ACTIVE_SERVICE_ACCOUNT"'
        in inspection
    )
    assert (
        '--worker-service-account "$WORKER_ACTIVE_SERVICE_ACCOUNT"'
        in inspection
    )
    assert (
        "--active-webhook-revision current-webhook-revision.json"
        in inspection
    )
    assert (
        "--active-worker-revision current-worker-revision.json"
        in inspection
    )
    assert "inspect-source" in source_gate
    assert source_gate.index("inspect-source") < workflow.index(
        "Create tagged zero-traffic revisions"
    )
    assert "Remove only this run's candidate tags" in workflow
    assert stage.index(
        '> "$RUNNER_TEMP/fraeno-webhook-candidate-revision"'
    ) < stage.index('gcloud run services update "$WEBHOOK_SERVICE"')
    assert stage.index(
        '> "$RUNNER_TEMP/fraeno-worker-candidate-revision"'
    ) < stage.index('gcloud run services update "$WORKER_SERVICE"')


def test_rotation_workflow_has_secret_safe_evidence_and_cleanup() -> None:
    workflow = WORKFLOW.read_text()

    artifact = workflow[workflow.index("Preserve secret-free rotation evidence") :]
    assert "credential-rotation-evidence.json" in artifact
    assert "credential-rotation-evidence.json.sha256" in artifact
    assert "staged-rotation.json" in artifact
    assert "webhook-active" not in artifact.split(
        "Remove any local credential material"
    )[0]
    assert "if: always()" in workflow
    assert "rm -f" in workflow
    assert '--remove-tags "$CANDIDATE_TAG"' in workflow
    assert "candidate tag no longer identifies this run" in workflow
    cleanup = workflow[
        workflow.index("Remove only this run's candidate tags and confirm production") :
    ]
    assert "validate-cleanup" in cleanup
    assert cleanup.count("cleanup_failed=1") >= 4
    assert (
        'if ! python -S -m fraeno.credential_rotation validate-cleanup'
        in cleanup
    )
    assert 'exit "$cleanup_failed"' in cleanup
    assert "validate-restore" in cleanup
    assert '--remove-secrets "$previous_secret"' in cleanup
    assert "--expected-latest-revision" in cleanup
    assert '"production_mutated"' not in workflow
    assert '"production_traffic_mutated": false' in workflow
    assert '"service_configuration_mutated": false' in workflow
    assert 'plan["service_configuration_mutated"] = True' in workflow
    assert 'plan["candidate_revisions"]' in workflow


def test_normal_release_refuses_a_pending_rotation_template() -> None:
    workflow = CONTROL_RELEASE_WORKFLOW.read_text()

    source_gate = workflow[
        workflow.index("previous-webhook-service.json") :
        workflow.index("Configure Artifact Registry")
    ]
    assert source_gate.count("validate-release-source") == 2
    assert "--active-revision previous-webhook-revision.json" in source_gate
    assert "--active-revision previous-worker-revision.json" in source_gate


def test_rotation_docs_gate_retirement_on_live_verification() -> None:
    docs = (ROOT / "docs" / "credential-rotation.md").read_text()

    assert "authorize-retirement" in docs
    assert "github_delivery_guid" in docs
    assert "installation_ids" in docs
    assert "check_run_ids" in docs
    assert "Only then:" in docs
    assert "disable the previous webhook-secret version" in docs
    assert "delete the previous GitHub App private key" in docs
    assert "checks structure and chronology only" in docs
    assert "it does not query GitHub" in docs
    assert "operator must separately confirm" in docs
    assert "authorize-promotion" in docs
    assert "--checksum credential-rotation-evidence.json.sha256" in docs
    assert "less than ten minutes left" in docs
    assert "Stop and run a new stage" in docs
    assert "short-lived attestation" in docs
    assert "copies the exact staged revisions" in docs
    assert "secret versions" in docs
    assert "into `rotation_identity`" in docs


def test_rotation_setup_is_explicit_idempotent_and_least_privileged() -> None:
    setup = SETUP.read_text()
    service_role = (
        ROOT / "deploy" / "gcp" / "credential-rotation-role.yaml"
    ).read_text()
    operation_role = (
        ROOT / "deploy" / "gcp" / "credential-rotation-operation-role.yaml"
    ).read_text()

    first_write = setup.index("gcloud iam service-accounts create")
    assert 'if [[ "${1:-}" != "--apply" ]]' in setup[:first_write]
    assert "Dry run only" in setup[:first_write]
    assert "roles create" in setup
    assert "roles update" in setup
    assert "providers create-oidc" in setup
    assert "providers update-oidc" in setup
    assert "assertion.repository_id == '1313414423'" in setup
    assert "assertion.repository_owner_id == '224500479'" in setup
    assert "assertion.ref == 'refs/heads/main'" in setup
    assert "assertion.workflow == 'Verify staged credential rotation'" in setup
    assert "assertion.event_name == 'workflow_dispatch'" in setup
    assert "roles/secretmanager.secretAccessor" in setup
    assert setup.count("roles/secretmanager.secretAccessor") == 1
    assert "fraeno-github-webhook-secret" in setup
    assert "roles/run.invoker" in setup
    assert "roles/iam.serviceAccountUser" in setup
    assert "roles/iam.serviceAccountOpenIdTokenCreator" in setup
    assert "gh variable set GCP_PROJECT_ID" in setup
    assert "gh variable set GCP_ROTATION_SERVICE_ACCOUNT" in setup

    assert "run.services.update" in service_role
    assert "run.operations.get" not in service_role
    assert "run.operations.get" in operation_role
    assert "secretmanager.versions.access" not in service_role
    assert "iam.serviceAccounts.actAs" not in service_role
