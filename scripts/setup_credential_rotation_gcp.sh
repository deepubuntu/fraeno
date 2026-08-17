#!/usr/bin/env bash
set -Eeuo pipefail

project="fraeno-prod"
project_number="1001829102083"
location="us-central1"
repository="deepubuntu/fraeno"
environment="credential-rotation"
pool="fraeno-github"
provider="fraeno-credential-rotation"
rotator="fraeno-credential-rotator@${project}.iam.gserviceaccount.com"
webhook_service="fraeno-github-webhook"
worker_service="fraeno-github-worker"
webhook_runtime="fraeno-webhook@${project}.iam.gserviceaccount.com"
worker_runtime="fraeno-worker@${project}.iam.gserviceaccount.com"
webhook_secret="fraeno-github-webhook-secret"
private_key_secret="fraeno-github-private-key"
service_role_id="fraenoCredentialRotationOperator"
operation_role_id="fraenoCredentialRotationOperationViewer"
provider_path="projects/${project_number}/locations/global/workloadIdentityPools/${pool}/providers/${provider}"
repository_principal="principalSet://iam.googleapis.com/projects/${project_number}/locations/global/workloadIdentityPools/${pool}/attribute.repository_id/1313414423"
provider_condition="assertion.repository == 'deepubuntu/fraeno' && assertion.repository_id == '1313414423' && assertion.repository_owner_id == '224500479' && assertion.ref == 'refs/heads/main' && assertion.workflow == 'Verify staged credential rotation' && assertion.event_name == 'workflow_dispatch'"

if [[ "${1:-}" != "--apply" ]]; then
  echo "Dry run only. No Google Cloud or GitHub resource was changed."
  echo "Review this script, then run: scripts/setup_credential_rotation_gcp.sh --apply"
  echo "Project: $project"
  echo "Service account: $rotator"
  echo "Provider: $provider_path"
  echo "GitHub environment: $environment"
  exit 0
fi

for command in gcloud gh
do
  command -v "$command" >/dev/null
done
gh auth status >/dev/null

actual_project_number="$(
  gcloud projects describe "$project" --format "value(projectNumber)"
)"
test "$actual_project_number" = "$project_number"
gcloud iam workload-identity-pools describe "$pool" \
  --project "$project" \
  --location global >/dev/null
for service in "$webhook_service" "$worker_service"
do
  gcloud run services describe "$service" \
    --project "$project" \
    --region "$location" >/dev/null
done
for secret in "$webhook_secret" "$private_key_secret"
do
  gcloud secrets describe "$secret" --project "$project" >/dev/null
done
for runtime in "$webhook_runtime" "$worker_runtime"
do
  gcloud iam service-accounts describe "$runtime" \
    --project "$project" >/dev/null
done

if ! gcloud iam service-accounts describe "$rotator" \
  --project "$project" >/dev/null 2>&1
then
  gcloud iam service-accounts create fraeno-credential-rotator \
    --project "$project" \
    --display-name "Fraeno credential rotator"
fi

if gcloud iam roles describe "$service_role_id" \
  --project "$project" >/dev/null 2>&1
then
  gcloud iam roles update "$service_role_id" \
    --project "$project" \
    --file deploy/gcp/credential-rotation-role.yaml
else
  gcloud iam roles create "$service_role_id" \
    --project "$project" \
    --file deploy/gcp/credential-rotation-role.yaml
fi
if gcloud iam roles describe "$operation_role_id" \
  --project "$project" >/dev/null 2>&1
then
  gcloud iam roles update "$operation_role_id" \
    --project "$project" \
    --file deploy/gcp/credential-rotation-operation-role.yaml
else
  gcloud iam roles create "$operation_role_id" \
    --project "$project" \
    --file deploy/gcp/credential-rotation-operation-role.yaml
fi

for service in "$webhook_service" "$worker_service"
do
  gcloud run services add-iam-policy-binding "$service" \
    --project "$project" \
    --region "$location" \
    --member "serviceAccount:$rotator" \
    --role "projects/$project/roles/$service_role_id"
done
gcloud projects add-iam-policy-binding "$project" \
  --member "serviceAccount:$rotator" \
  --role "projects/$project/roles/$operation_role_id" \
  --condition=None

gcloud secrets add-iam-policy-binding "$webhook_secret" \
  --project "$project" \
  --member "serviceAccount:$rotator" \
  --role roles/secretmanager.viewer
gcloud secrets add-iam-policy-binding "$webhook_secret" \
  --project "$project" \
  --member "serviceAccount:$rotator" \
  --role roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding "$private_key_secret" \
  --project "$project" \
  --member "serviceAccount:$rotator" \
  --role roles/secretmanager.viewer

for runtime in "$webhook_runtime" "$worker_runtime"
do
  gcloud iam service-accounts add-iam-policy-binding "$runtime" \
    --project "$project" \
    --member "serviceAccount:$rotator" \
    --role roles/iam.serviceAccountUser
done
gcloud iam service-accounts add-iam-policy-binding "$rotator" \
  --project "$project" \
  --member "serviceAccount:$rotator" \
  --role roles/iam.serviceAccountOpenIdTokenCreator
gcloud run services add-iam-policy-binding "$worker_service" \
  --project "$project" \
  --region "$location" \
  --member "serviceAccount:$rotator" \
  --role roles/run.invoker

if gcloud iam workload-identity-pools providers describe "$provider" \
  --project "$project" \
  --location global \
  --workload-identity-pool "$pool" >/dev/null 2>&1
then
  gcloud iam workload-identity-pools providers update-oidc "$provider" \
    --project "$project" \
    --location global \
    --workload-identity-pool "$pool" \
    --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping \
      "google.subject=assertion.sub,attribute.repository_id=assertion.repository_id" \
    --attribute-condition "$provider_condition"
else
  gcloud iam workload-identity-pools providers create-oidc "$provider" \
    --project "$project" \
    --location global \
    --workload-identity-pool "$pool" \
    --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping \
      "google.subject=assertion.sub,attribute.repository_id=assertion.repository_id" \
    --attribute-condition "$provider_condition"
fi
gcloud iam service-accounts add-iam-policy-binding "$rotator" \
  --project "$project" \
  --member "$repository_principal" \
  --role roles/iam.workloadIdentityUser

gh api --method PUT "repos/$repository/environments/$environment" >/dev/null
gh variable set GCP_PROJECT_ID \
  --repo "$repository" \
  --env "$environment" \
  --body "$project"
gh variable set GCP_LOCATION \
  --repo "$repository" \
  --env "$environment" \
  --body "$location"
gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER \
  --repo "$repository" \
  --env "$environment" \
  --body "$provider_path"
gh variable set GCP_ROTATION_SERVICE_ACCOUNT \
  --repo "$repository" \
  --env "$environment" \
  --body "$rotator"

gcloud iam workload-identity-pools providers describe "$provider" \
  --project "$project" \
  --location global \
  --workload-identity-pool "$pool" \
  --format "yaml(name,attributeMapping,attributeCondition,state)"
gh variable list --repo "$repository" --env "$environment"
