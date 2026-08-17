# Customer and entitlement operations

Fraeno treats one GitHub App installation as one product account. The immutable
GitHub installation ID is the customer key. Organization names and usernames
can change, so they are metadata rather than identity.

## Firestore records

`fraeno_installations/{installation_id}` is the permanent installation
registry. A record remains after suspension or removal. It includes the GitHub
account ID and current login, account type, installation status, connected
repository count, install time, recent activity, and first check time.

`fraeno_entitlements/{installation_id}` is the access decision. Supported
statuses are `trial`, `active`, `grace`, `suspended`, and `expired`. The record
also carries the plan, source, billing status, dates, operator, and reason.

`fraeno_usage/{installation_id}-{YYYY-MM}` stores monthly counts for checks
started and completed. It is shared operational capacity, not a per-customer
deployment.

## Access decision

Fraeno runs when either condition is true.

1. The installation has a current `trial`, `active`, or unexpired `grace`
   entitlement.
2. The installation login is present in
   `FRAENO_APPROVED_INSTALLATION_LOGINS`.

The environment allowlist remains only as an emergency override so a Firestore
incident does not disable the DeepUbuntu Labs production installation.

## Product metrics

- Leads are unique access-request email records in the contact Worker KV.
- Installed accounts are installation records with status `installed`.
- Activated accounts have started at least one Fraeno check.
- Monthly active accounts have started a check in the current activity window.
- Paid customers have a current entitlement with billing status `paid`.

These definitions are used by the operations console at `/admin/`.

## Manual operation

The admin console uses Fraeno's dedicated Firebase administrator claim.
Firestore rules allow administrators to read product records
and change only entitlement and audit records. Installation and usage records
remain server-written.

The guarded command line remains available when the browser console is not.

```bash
fraeno-github-ops customers --project fraeno-prod

fraeno-github-ops entitle \
  --project fraeno-prod \
  --installation-id 123456 \
  --status trial \
  --plan private_beta \
  --source manual \
  --billing-status comped \
  --actor operator@deepubuntu.com \
  --reason "Approved for the private beta" \
  --confirm 123456
```

The entitlement command is a dry run unless `--execute` is supplied.
