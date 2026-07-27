# Immutable runner releases

Fraeno publishes its validation runner directly to Google Artifact Registry.
The target repository is:

```text
us-central1-docker.pkg.dev/deepubuntu-32f9e/fraeno-runner
```

The release workflow is manual until production identity and review gates are
configured. It never accepts a service-account key. It also refuses any project,
region, or repository other than this public runner-only target.

## Production prerequisites

Create the `runner-production` GitHub environment, then create these variables
inside that environment:

```text
GCP_PROJECT_ID
GCP_ARTIFACT_REGISTRY_LOCATION
GCP_ARTIFACT_REGISTRY_REPOSITORY
GCP_WORKLOAD_IDENTITY_PROVIDER
GCP_RELEASE_SERVICE_ACCOUNT
```

For the current GCP target, the first three values are `deepubuntu-32f9e`,
`us-central1`, and `fraeno-runner`.

Use these exact values for the remaining two variables:

```text
GCP_WORKLOAD_IDENTITY_PROVIDER=projects/286435890377/locations/global/workloadIdentityPools/fraeno-github/providers/fraeno-runner
GCP_RELEASE_SERVICE_ACCOUNT=fraeno-runner-publisher@deepubuntu-32f9e.iam.gserviceaccount.com
```

The workload identity provider must accept only tokens where the repository is
`deepubuntu/fraeno`, the repository ID is `1313414423`, the owner ID is
`224500479`, the ref is `refs/heads/main`, the workflow name is
`Publish immutable Fraeno runner`, and the event is `workflow_dispatch`.
Map `assertion.repository_id` to `attribute.repository_id`, then grant that
repository principal `roles/iam.workloadIdentityUser` on the dedicated
publisher service account. The provider condition enforces every other claim.

The publisher service account needs `roles/artifactregistry.writer` only on the
`fraeno-runner` repository. It does not need project editor, Cloud Run admin,
service-account key creation, or Secret Manager access.

Allow deployments to `runner-production` only from `main` and configure a
required reviewer when the GitHub plan supports it. The workflow uses no GitHub
secret for Google credentials.
The repository must remain publicly readable through
`roles/artifactregistry.reader` for `allUsers`. Enable immutable tags on the
existing repository:

```bash
gcloud artifacts repositories update fraeno-runner \
  --project deepubuntu-32f9e \
  --location us-central1 \
  --immutable-tags
```

That GCP change is intentionally not made by the workflow. A publisher must not
be able to weaken the repository that protects its own releases.

The private mixed `fraeno` Artifact Registry repository is not a runner release
target. The workflow checks the exact project, region, and repository before it
requests Google credentials.

## Release inputs

Run `Publish immutable Fraeno runner` from GitHub Actions with:

- `version`, an exact SemVer that matches `project.version` in
  `pyproject.toml`
- `commit_sha`, a full reviewed commit reachable from `main`
- `previous_digest`, the current production runner digest, or `none` only when
  no runner image exists

GitHub creates a new commit when it squash-merges a pull request. The gate
requires successful `test`, `container`, and `ros-integration` checks on that
exact release commit. It also requires a successful
`Fraeno / robot integration` check on the merged pull request head. Before
accepting that review, it proves the pull request head and release commit have
the exact same Git tree.

## What is published

The workflow tests the commit before obtaining Google credentials. It builds
and exercises the isolated runner, then publishes the image under exactly two
create-once tags:

```text
runner:vVERSION
runner:FULL_COMMIT_SHA
```

It does not publish a moving alias. Customer workflows use only:

```text
REGISTRY/runner@sha256:FULL_DIGEST
```

BuildKit attaches an SPDX SBOM and SLSA provenance to the image. The workflow
captures the registry digest, confirms both tags resolve to it, and uploads a
checksummed release manifest for 90 days. The manifest records the four release
checks, their commit SHAs, the reviewed pull request, and the previous
production digest.

## Rollback proof

Before publishing, the workflow pulls the previous digest and runs the full
safe and hostile external-repository validation against it. The protected
release evidence then records this exact rollback reference:

```text
REGISTRY/runner@sha256:PREVIOUS_DIGEST
```

Rollback means restoring that digest in the customer repository variable
`FRAENO_RUNNER_IMAGE`. A semantic or commit tag is never used for rollback.

## Control-plane releases

The `Release Fraeno control plane` workflow deploys the GitHub App webhook and
worker from one image built from the exact reviewed commit. It uses the private,
immutable repository:

```text
us-central1-docker.pkg.dev/deepubuntu-32f9e/fraeno-control-plane
```

Create the `control-plane-production` GitHub environment with the same five
variable names used by the runner environment. Use these values:

```text
GCP_PROJECT_ID=deepubuntu-32f9e
GCP_ARTIFACT_REGISTRY_LOCATION=us-central1
GCP_ARTIFACT_REGISTRY_REPOSITORY=fraeno-control-plane
GCP_WORKLOAD_IDENTITY_PROVIDER=projects/286435890377/locations/global/workloadIdentityPools/fraeno-github/providers/fraeno-control-plane
GCP_RELEASE_SERVICE_ACCOUNT=fraeno-control-plane-releaser@deepubuntu-32f9e.iam.gserviceaccount.com
```

The dedicated provider accepts only tokens for `deepubuntu/fraeno`, repository
ID `1313414423`, owner ID `224500479`, `refs/heads/main`, the workflow named
`Release Fraeno control plane`, and the `workflow_dispatch` event. The release
service account has no key and cannot read application secrets. Its permissions
are limited to:

- write images to `fraeno-control-plane`;
- update and inspect the two existing Cloud Run services;
- act as the existing webhook and worker runtime service accounts;
- invoke the private worker for health checks;
- mint an identity token for those checks.

The service-scoped custom role is declared in
`deploy/gcp/control-plane-release-role.yaml`. The release service account has
that role only on the webhook and worker. A second custom role in
`deploy/gcp/control-plane-operation-role.yaml` grants project-level read access
only to the asynchronous operation records needed by the command line client.
The workflow also pins the two allowed service names and both expected runtime
service accounts.

Run the workflow with:

- `version`, the exact SemVer in `pyproject.toml`;
- `commit_sha`, the full reviewed merge commit on `main`;
- `previous_webhook_revision`, the only revision currently receiving webhook
  traffic;
- `previous_worker_revision`, the only revision currently receiving worker
  traffic.

The explicit previous revisions are a concurrency guard. The workflow stops if
production changed after the operator inspected it.

The gate tests the checked-out commit and requires successful `test`,
`container`, and `ros-integration` checks on that commit. It also requires the
successful `Fraeno / robot integration` result on the reviewed pull request
tree and proves that tree is identical to the merge commit.

Before it builds or deploys the control plane, the gate requires both
`runner:vVERSION` and `runner:FULL_COMMIT_SHA` in the immutable runner
repository. Both tags must resolve to the same digest. This means the runner,
control plane, and final GitHub Release always identify the same version and
exact commit. Publish the runner from the final commit before starting the
control-plane release.

BuildKit publishes a uniquely tagged release candidate with an SPDX SBOM and
SLSA provenance. Both Cloud Run services are deployed from its digest with no
traffic first. Their tagged revision URLs must pass health checks, and the
public webhook must reject an invalid GitHub signature.

After both candidates pass, the workflow:

1. sends production traffic to both candidate revisions and tests them;
2. sends traffic back to both explicitly supplied previous revisions and tests
   them;
3. restores both candidate revisions to 100 percent traffic and tests them
   again;
4. uploads a checksummed manifest with the previous and final revisions;
5. creates immutable semantic and commit tags for the tested digest;
6. creates the GitHub Release and product version tag at the exact commit.

Evidence uploads before semantic tags are created. Any later failure in the
Google-authenticated job runs a separate cleanup step that restores both
previous revisions and removes temporary Cloud Run traffic tags. A successful
run keeps only the final candidate revisions at 100 percent traffic, but retains
the unique registry candidate tag as build evidence.

If immutable tag creation partially succeeds, start the same version and commit
again with `resume_run_id` set to the failed run. The recovery path accepts an
existing tag only after it verifies the prior run identity, downloads its
evidence, verifies the checksum, confirms the exact version and commit, confirms
rollback was proven, and matches the existing registry digest. It then repeats
the deployment and rollback proof before idempotently completing both tags.
Never use `resume_run_id` to reuse unrelated build output.

The GitHub Release runs as a separate job with only `contents: write`. It cannot
request a Google identity or change production. It starts only after the
deployment evidence artifact has uploaded successfully, and its notes link to
that workflow run, the exact runner digest, and the exact control-plane digest.
If release publication is interrupted after GitHub created it, rerunning failed
jobs verifies that the existing tag points to the exact release commit and
finishes successfully.

The cleanup policy in `deploy/gcp/control-plane-cleanup-policy.json` runs in
dry-run mode. It always keeps semantic release tags and the ten most recent
versions. Only failed candidates older than 90 days and untagged content older
than 30 days are eligible for deletion. Review the dry-run audit before ever
enabling deletion:

```bash
gcloud artifacts repositories set-cleanup-policies fraeno-control-plane \
  --project deepubuntu-32f9e \
  --location us-central1 \
  --policy deploy/gcp/control-plane-cleanup-policy.json \
  --dry-run
```

Issue 19 can close only after the runner and control-plane release workflows
both complete successfully and their evidence artifacts are verified.
