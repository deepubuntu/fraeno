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

## Current boundary

This workflow publishes only the customer validation runner. It does not deploy
the Fraeno control plane to Cloud Run. A separate release gate must still make
both control-plane services use an image built from the exact release commit
and prove revision rollback before issue 19 can close.
