# GitHub App setup

## Registration

Create the app under the `deepubuntu` account.

- GitHub App name: `Fraeno`, if globally available
- Homepage: the Fraeno product URL
- Webhook URL: `https://WEBHOOK_SERVICE_URL/webhooks/github`
- Webhook secret: a random value stored only in GitHub and Secret Manager
- SSL verification: enabled
- Installation scope during development: only `deepubuntu`

Repository permissions:

- Actions: read and write
- Checks: read and write
- Contents: read
- Pull requests: read

Subscribe to:

- Pull request
- Workflow run

Checks write permission also delivers rerun events for checks created by Fraeno.
GitHub sends `installation` and `installation_repositories` lifecycle events to
GitHub Apps automatically. Fraeno uses them to keep a metadata-only readiness
record for every installed repository.

Generate the webhook secret without a file-ending newline, then verify its exact
length before storing the same value in GitHub and Secret Manager:

```bash
openssl rand -hex 32 | tr -d '\n' > webhook-secret
test "$(wc -c < webhook-secret)" -eq 64
```

Fraeno also strips surrounding file whitespace when it loads the secret. This
prevents a trailing newline from causing every valid GitHub delivery to fail
signature verification.

## Production services

Fraeno deploys one container as two Cloud Run services.

The public `fraeno-webhook` service only verifies GitHub signatures and enqueues
durable work. It requires:

```text
FRAENO_SERVICE_MODE=webhook
FRAENO_GITHUB_WEBHOOK_SECRET
FRAENO_GCP_PROJECT
FRAENO_GCP_LOCATION
FRAENO_TASK_QUEUE
FRAENO_WORKER_URL
FRAENO_TASK_SERVICE_ACCOUNT
```

The private `fraeno-worker` service performs GitHub API work and stores
correlation state. It requires:

```text
FRAENO_SERVICE_MODE=worker
FRAENO_GITHUB_APP_ID
FRAENO_GITHUB_PRIVATE_KEY
FRAENO_GITHUB_WORKFLOW_FILE=fraeno-validation.yml
FRAENO_MAX_DELIVERY_ATTEMPTS=5
FRAENO_DELIVERY_STALE_SECONDS=900
FRAENO_RUN_STALE_SECONDS=900
FRAENO_MAX_RUN_SECONDS=7200
FRAENO_DELIVERY_RETENTION_DAYS=14
FRAENO_RUN_RETENTION_DAYS=30
FRAENO_REPOSITORY_RETENTION_DAYS=30
FRAENO_REPLAY_AUDIT_RETENTION_DAYS=90
```

The Cloud Tasks service account is the only invoker of the worker. The webhook
service can enqueue tasks but cannot invoke the worker directly. Never commit
the PEM private key or webhook secret.

The configured Cloud Tasks maximum attempts should equal
`FRAENO_MAX_DELIVERY_ATTEMPTS`. Fraeno marks the last failed attempt as a dead
letter and returns success to Cloud Tasks, which prevents an unbounded retry
loop.

## Repository readiness

Fraeno stores only operational metadata:

- installation and repository numeric IDs
- repository full name and default branch
- readiness status and a human-readable reason
- workflow run, check run, pull request, and commit identifiers
- timestamps, retry counts, and replay audit fields

Fraeno does not store repository source, webhook bodies, installation tokens,
private keys, or validation artifacts in Firestore. A repository is ready when
its trusted `.github/workflows/fraeno-validation.yml` workflow can be read.
Removing or suspending access marks the repository inactive and clears only its
local active-run locks.

## Scheduled recovery

Call `POST /internal/reconcile` every ten minutes from Cloud Scheduler. The
request must be authenticated to the private worker through Cloud Run IAM and
must carry `X-CloudScheduler: true`. The reconciler:

1. moves abandoned processing records to the dead-letter state;
2. asks GitHub for the current state of stale workflow runs;
3. finishes checks when their completion webhook was lost;
4. fails and cancels runs that exceed the configured time limit.

A separate scheduler service account needs `roles/run.invoker` on the private
worker. This is a production IAM change and should be applied only after the
exact service account and worker URL are confirmed.

```bash
gcloud scheduler jobs create http fraeno-reconcile \
  --location=us-central1 \
  --schedule="*/10 * * * *" \
  --uri="$FRAENO_WORKER_URL/internal/reconcile" \
  --http-method=POST \
  --headers="X-CloudScheduler=true,Content-Type=application/json" \
  --oidc-service-account-email="$FRAENO_SCHEDULER_SERVICE_ACCOUNT" \
  --oidc-token-audience="$FRAENO_WORKER_URL"
```

## Safe replay

GitHub retains App webhook deliveries for three days. Use the GUID and numeric
delivery ID from the App's recent deliveries page. The command verifies both
identities, requires an operator and reason, defaults to read-only, and asks
GitHub to send the original signed webhook again. It never reads or stores the
original body.

```bash
FRAENO_OPERATOR="operator@example.com" \
fraeno-github-ops replay \
  --project="GCP_PROJECT_ID" \
  --delivery-guid="DELIVERY_GUID" \
  --github-delivery-id=DELIVERY_ID \
  --reason="Recover after the worker outage" \
  --confirm="DELIVERY_GUID"
```

Add `--execute` only after the read-only eligibility check succeeds.

## Retention and alerts

Every durable history record has an `expires_at` timestamp. Active-run locks do
not expire automatically because deleting a lock cannot finish a GitHub check.
The reconciler owns their cleanup. Repository readiness expires when no
lifecycle event or pull request refreshes it, and the next event rebuilds it.
Enable Firestore TTL for the four collection groups in
`deploy/gcp/retention.json` after confirming the retention periods:

```bash
for collection in \
  github_deliveries github_runs github_repositories github_replay_audit
do
  gcloud firestore fields ttls update expires_at \
    --collection-group="$collection" \
    --enable-ttl
done
```

Fraeno writes six low-cardinality JSON events to Cloud Logging. The alert files
in `deploy/gcp/alert-policies` cover signature rejection, queue delay, repeated
retries, dispatch failure, run duration, and stale checks. They use log match
conditions instead of chargeable user-defined log metrics. Every policy is
disabled and has no notification channel until an operator chooses a real
destination.

Validate the files without changing GCP:

```bash
python3 scripts/validate_gcp_config.py
```

After choosing notification channels and enabling the policies in a reviewed
copy, create each policy with:

```bash
gcloud monitoring policies create \
  --policy-from-file="deploy/gcp/alert-policies/POLICY.json"
```

## Local service

```bash
python3 -m pip install ".[app]"
FRAENO_SERVICE_MODE=webhook \
  uvicorn fraeno.github_app.app:app --host 127.0.0.1 --port 8080
```

Use `FRAENO_SERVICE_MODE=worker` to run the private worker. Both services expose
`/health`; each response identifies its service role and configuration state.
Cloud Run reserves some paths ending in `z`, so Fraeno does not use `/healthz`.

## Production checks

Before customer installation:

1. reject a deliberately invalid signature;
2. redeliver the same webhook and confirm it is deduplicated;
3. open a test pull request and observe queued, in-progress, and completed states;
4. confirm the workflow uses the base contract and no app secrets;
5. rerun the check from GitHub;
6. verify a safe update passes;
7. verify the QoS-breaking update fails;
8. require `Fraeno / robot integration` on `main`.
9. install the App on two test repositories and start one pull request in each;
10. confirm each repository keeps its own active run and check;
11. withhold one completion delivery and confirm scheduled recovery finishes it;
12. force a terminal delivery failure and verify guarded GitHub redelivery.

## Repository runner contract

Run `fraeno init --open-pr` to propose the trusted files in the customer
repository. The command sets `FRAENO_RUNNER_IMAGE` to the published runner
image including its full `@sha256:` digest. A tag alone is rejected.

The project contract must live at `.fraeno.yml` on the base branch. Observation
commands that use repository code should call the trusted copy through
`FRAENO_TRUSTED_ROOT`. The candidate can read this copy but cannot change it.

The complete setup is in `docs/onboarding.md`. The isolation model and local
verification command are in `docs/runner.md`.
