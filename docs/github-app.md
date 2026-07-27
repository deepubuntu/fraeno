# GitHub App setup

## Registration

Create the app under the `deepubuntu` account.

- GitHub App name: `Fraeno`, if globally available
- Homepage: the Fraeno product URL
- Webhook URL: `https://SERVICE_URL/webhooks/github`
- Webhook secret: a random value stored only in GitHub and the deployment secret store
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

## Runtime settings

The service requires:

```text
FRAENO_GITHUB_APP_ID
FRAENO_GITHUB_PRIVATE_KEY
FRAENO_GITHUB_WEBHOOK_SECRET
```

Optional settings:

```text
FRAENO_GITHUB_WORKFLOW_FILE=fraeno-validation.yml
FRAENO_STORE=firestore
FRAENO_GITHUB_API_URL=https://api.github.com
```

Never commit the PEM private key or webhook secret.

## Local service

```bash
python3 -m pip install ".[app]"
uvicorn fraeno.github_app.app:app --host 127.0.0.1 --port 8080
```

The health endpoint is `/healthz`. A configured service reports `configured: true`.

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
