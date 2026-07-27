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
```

The Cloud Tasks service account is the only invoker of the worker. The webhook
service can enqueue tasks but cannot invoke the worker directly. Never commit
the PEM private key or webhook secret.

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
