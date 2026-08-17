# Credential rotation

Fraeno rotates its webhook secret and GitHub App private key without placing
either value in GitHub, repository files, command arguments, or logs. Google
Secret Manager holds every version. Cloud Run receives exact numeric versions,
never `latest`.

The overlap is deliberately short. A staged revision can accept the new active
credential and the previous credential for at most one hour. The application
rejects the previous webhook secret after the recorded deadline. GitHub API
requests try the active private key first and use the previous key only when
GitHub returns `401` during the same window.

## One-time Google Cloud setup

The rotator service account and provider do not exist by default. From a
reviewed checkout after this change merges, first inspect the idempotent setup:

```bash
scripts/setup_credential_rotation_gcp.sh
```

It performs no write unless `--apply` is present. After reviewing every exact
resource, role, provider condition, and GitHub environment value, apply it:

```bash
scripts/setup_credential_rotation_gcp.sh --apply
```

The script verifies the fixed project number, existing services, runtime
accounts, secrets, and workload identity pool before its first write. Repeating
it updates the same service account, custom roles, provider, IAM bindings, and
environment variables instead of creating parallel resources. It does not read
secret values, add secret versions, deploy a revision, or start a rotation.

The script creates a dedicated keyless service account. It grants the
three-permission role in `deploy/gcp/credential-rotation-role.yaml` only on the
existing webhook and worker services. It grants the separate read-only Cloud
Run operation role at project scope because operation records are not children
of either service.

Grant the rotator these additional narrowly scoped permissions:

- Secret Manager Viewer and Secret Accessor on
  `fraeno-github-webhook-secret` only
- Secret Manager Viewer on `fraeno-github-private-key` only
- Service Account User on the existing webhook and worker runtime service
  accounts
- Cloud Run Invoker on `fraeno-github-worker`
- Service Account OpenID Token Creator on itself

The workflow never reads either private-key version. The private worker checks
both keys against GitHub and returns booleans only. The rotator needs metadata
access to the private-key secret so it can require exact enabled versions.

Create a dedicated workload identity provider named
`fraeno-credential-rotation`. Its condition must accept only:

- repository `deepubuntu/fraeno`
- repository ID `1313414423`
- owner ID `224500479`
- ref `refs/heads/main`
- workflow `Verify staged credential rotation`
- event `workflow_dispatch`

Create the `credential-rotation` GitHub environment with:

```text
GCP_PROJECT_ID=fraeno-prod
GCP_LOCATION=us-central1
GCP_WORKLOAD_IDENTITY_PROVIDER=projects/1001829102083/locations/global/workloadIdentityPools/fraeno-github/providers/fraeno-credential-rotation
GCP_ROTATION_SERVICE_ACCOUNT=fraeno-credential-rotator@fraeno-prod.iam.gserviceaccount.com
```

Require a reviewer for this environment when the GitHub plan supports it.

## Prepare new versions

First deploy a normal reviewed Fraeno control-plane release that contains the
overlap support. Do not begin a rotation on an older image.

Generate a new 64-character webhook secret and add it through standard input:

```bash
umask 077
openssl rand -hex 32 > webhook-secret.new
test "$(wc -c < webhook-secret.new)" -eq 65
tr -d '\n' < webhook-secret.new |
  gcloud secrets versions add fraeno-github-webhook-secret \
    --project=fraeno-prod \
    --data-file=-
```

Generate a second GitHub App private key in the GitHub App settings. Keep the
current key active. Download the new PEM once, then send it directly to Secret
Manager:

```bash
gcloud secrets versions add fraeno-github-private-key \
  --project=fraeno-prod \
  --data-file=/path/to/downloaded-private-key.pem
```

Remove both local files after confirming the exact new numeric versions. Do not
paste either value into a workflow input.

## Dry run and stage

Run `Verify staged credential rotation` in `dry-run` mode with:

- both exact new version numbers
- both exact current version numbers
- the webhook and worker revisions currently receiving 100 percent of traffic

The dry run reads metadata only. It stops if a version is disabled, a version
uses `latest`, or production changed after inspection.

Run it again in `stage` mode with confirmation
`STAGE_FRAENO_ROTATION`. The workflow:

1. creates one tagged zero-traffic revision for each service
2. pins the exact image and runtime account from each active reviewed revision
3. pins the new and previous Secret Manager versions
4. records one overlap window no longer than one hour
5. confirms production traffic remains on the prior revisions after each write
6. sends signed probes using both webhook secrets to a readiness-only path
7. proves an invalid signature is rejected
8. asks the private worker to validate both App keys with GitHub
9. uploads only version numbers, revision names, results, and timestamps

The readiness-only webhook path verifies size and signature but never enqueues,
stores, or processes the synthetic body. The workflow removes credential files
from the runner even when a later step fails. No secret or identity token is
included in its evidence. Tests run in a separate job without cloud identity.
The privileged job installs no Python packages and runs only reviewed
repository code plus the Python standard library before it accesses Google
Cloud or either webhook-secret version.

Its always-run cleanup creates zero-traffic restore revisions whose runtime
configuration matches the original active revisions, then removes only a tag
that still identifies the exact candidate created by that run. The immutable
candidate revisions and secret-free evidence remain available for review.
Cleanup fails if the restored revision, latest template, production traffic, or
tag state is wrong. Stage mode temporarily mutates both Cloud Run service
configurations but does not move production traffic. The normal release
workflow also refuses to deploy when a latest service template contains a
pending rotation or when its runtime specification, annotations, or labels
differ from the active production revision. The comparison ignores only the
image and release labels that the release replaces, plus Cloud Run bookkeeping
fields that are not part of the service template.

## Promote and verify live traffic

Download the staged evidence and run the enforced promotion gate immediately
before changing traffic:

```bash
python -m fraeno.credential_rotation authorize-promotion \
  --evidence credential-rotation-evidence.json \
  --checksum credential-rotation-evidence.json.sha256 \
  --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --output promotion-authorization.json
test "$(python -c \
  'import json; print(json.load(open("promotion-authorization.json"))["promotion_allowed"])')" \
  = True
```

This verifies the evidence checksum, every staged check, the exact candidate
revisions and secret versions, and the overlap deadline. It refuses evidence
whose verification time is in the future, whose overlap has expired, or whose
overlap has less than ten minutes left. Stop and run a new stage if this gate
fails. The authorization is a local, short-lived attestation. Run it again if
you do not begin the traffic change immediately.

Only after that gate passes, move both services to the exact revisions recorded
in `rotation_identity`, then change the webhook secret in the GitHub App
settings to the exact active version recorded there. Do not delete the old App
key.

Open or synchronize a harmless pull request in every installed test repository.
Record:

- the first delivery GUID signed with the new webhook secret
- every installation ID tested
- every successful Fraeno check-run ID
- the UTC verification time

Store that metadata in a local `live-verification.json`:

```json
{
  "verified_at": "2026-07-27T20:20:00Z",
  "github_delivery_guid": "123e4567-e89b-42d3-a456-426614174000",
  "active_webhook_secret_accepted": true,
  "installation_ids": [149403236],
  "check_run_ids": [90100043690]
}
```

Download the staged evidence and authorize retirement:

```bash
sha256sum --check credential-rotation-evidence.json.sha256
python -m fraeno.credential_rotation authorize-retirement \
  --evidence credential-rotation-evidence.json \
  --live-verification live-verification.json \
  --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --output retirement-authorization.json
```

The command checks structure and chronology only. It requires an
operator-supplied, well-formed GitHub delivery GUID plus positive installation
and check-run IDs, but it does not query GitHub. Before retiring anything, the
operator must separately confirm those identifiers in the GitHub App delivery
and check-run pages. Its output copies the exact staged revisions and secret
versions into `rotation_identity`, but remains an untrusted local attestation.

## Retire previous credentials

Stop if `retirement-authorization.json` does not contain
`"retirement_allowed": true`.

Create fresh Cloud Run revisions that keep only:

```text
FRAENO_GITHUB_WEBHOOK_SECRET=new exact version
FRAENO_GITHUB_PRIVATE_KEY=new exact version
```

Remove both `*_PREVIOUS` variables and both rotation timestamps. Test a second
real pull request after those revisions receive traffic.

Only then:

1. disable the previous webhook-secret version in Secret Manager
2. delete the previous GitHub App private key in GitHub
3. disable the previous private-key version in Secret Manager

Keep the secret-free staged and live evidence. Do not store the local
verification JSON if it contains any notes beyond the identifiers above.
