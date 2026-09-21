#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/main.bicep"
PARAMETERS_FILE="${SCRIPT_DIR}/main.parameters.example.json"

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-himalayawatch-prod}"
LOCATION="${LOCATION:-centralindia}"
DEPLOYMENT_NAME="${DEPLOYMENT_NAME:-himalayawatch-prod}"
NAME_PREFIX="${NAME_PREFIX:-hwatch}"
ENVIRONMENT_NAME="${ENVIRONMENT_NAME:-prod}"
IMAGE_TAG="${IMAGE_TAG:-himalayawatch:v1}"

if [[ -z "${ACR_NAME:-}" ]]; then
  echo "Set ACR_NAME to a globally unique lowercase registry name before running this script." >&2
  echo "Example: export ACR_NAME=hwatchacr12345" >&2
  exit 1
fi

if ! [[ "${ACR_NAME}" =~ ^[a-zA-Z0-9]{5,50}$ ]]; then
  echo "ACR_NAME must contain only letters and numbers and be 5-50 characters long." >&2
  exit 1
fi

az account show --output none

if [[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]]; then
  az account set --subscription "${AZURE_SUBSCRIPTION_ID}"
fi

if [[ -z "${HIMALAYAWATCH_INGEST_API_KEY:-}" ]]; then
  read -rsp "Ingestion API key (minimum 24 characters): " HIMALAYAWATCH_INGEST_API_KEY
  echo
fi

if [[ -z "${HIMALAYAWATCH_OPERATOR_API_KEY:-}" ]]; then
  read -rsp "Operator API key (minimum 24 characters): " HIMALAYAWATCH_OPERATOR_API_KEY
  echo
fi

if (( ${#HIMALAYAWATCH_INGEST_API_KEY} < 24 )); then
  echo "The ingestion API key must be at least 24 characters." >&2
  exit 1
fi

if (( ${#HIMALAYAWATCH_OPERATOR_API_KEY} < 24 )); then
  echo "The operator API key must be at least 24 characters." >&2
  exit 1
fi

HIMALAYAWATCH_WRITE_API_KEY="${HIMALAYAWATCH_WRITE_API_KEY:-}"
HIMALAYAWATCH_SCIENTIST_API_KEY="${HIMALAYAWATCH_SCIENTIST_API_KEY:-}"
HIMALAYAWATCH_AUTHORITY_API_KEY="${HIMALAYAWATCH_AUTHORITY_API_KEY:-}"
AZURE_SMS_SENDER="${AZURE_SMS_SENDER:-}"
NOTIFICATION_WEBHOOK_SECRET="${NOTIFICATION_WEBHOOK_SECRET:-}"
COMMUNICATION_SERVICES_ROLE_DEFINITION_ID="${COMMUNICATION_SERVICES_ROLE_DEFINITION_ID:-}"

echo "Creating or updating resource group ${RESOURCE_GROUP}..."
az group create \
  --name "${RESOURCE_GROUP}" \
  --location "${LOCATION}" \
  --output none

echo "Deploying identities, messaging, storage, monitoring, and communication resources..."
az deployment group create \
  --name "${DEPLOYMENT_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --template-file "${TEMPLATE_FILE}" \
  --parameters "@${PARAMETERS_FILE}" \
  --parameters \
    location="${LOCATION}" \
    namePrefix="${NAME_PREFIX}" \
    environmentName="${ENVIRONMENT_NAME}" \
    deployContainerApps=false \
  --output none

if ! az acr show --name "${ACR_NAME}" --resource-group "${RESOURCE_GROUP}" --output none 2>/dev/null; then
  echo "Creating Azure Container Registry ${ACR_NAME}..."
  az acr create \
    --resource-group "${RESOURCE_GROUP}" \
    --name "${ACR_NAME}" \
    --sku Basic \
    --output none
fi

echo "Building ${IMAGE_TAG} from ${PROJECT_ROOT}..."
az acr build \
  --registry "${ACR_NAME}" \
  --image "${IMAGE_TAG}" \
  "${PROJECT_ROOT}"

ACR_SERVER="$(az acr show --name "${ACR_NAME}" --query loginServer --output tsv)"
ACR_ID="$(az acr show --name "${ACR_NAME}" --query id --output tsv)"

PUBLISHER_IDENTITY_ID="$(az deployment group show \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${DEPLOYMENT_NAME}" \
  --query properties.outputs.publisherIdentityResourceId.value \
  --output tsv)"
PROCESSOR_IDENTITY_ID="$(az deployment group show \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${DEPLOYMENT_NAME}" \
  --query properties.outputs.processorIdentityResourceId.value \
  --output tsv)"

PUBLISHER_PRINCIPAL_ID="$(az identity show --ids "${PUBLISHER_IDENTITY_ID}" --query principalId --output tsv)"
PROCESSOR_PRINCIPAL_ID="$(az identity show --ids "${PROCESSOR_IDENTITY_ID}" --query principalId --output tsv)"

grant_acr_pull() {
  local principal_id="$1"
  local existing
  existing="$(az role assignment list \
    --assignee-object-id "${principal_id}" \
    --scope "${ACR_ID}" \
    --role AcrPull \
    --query '[0].id' \
    --output tsv)"

  if [[ -z "${existing}" ]]; then
    az role assignment create \
      --assignee-object-id "${principal_id}" \
      --assignee-principal-type ServicePrincipal \
      --role AcrPull \
      --scope "${ACR_ID}" \
      --output none
  fi
}

echo "Granting both managed identities permission to pull the image..."
grant_acr_pull "${PUBLISHER_PRINCIPAL_ID}"
grant_acr_pull "${PROCESSOR_PRINCIPAL_ID}"

echo "Deploying Container Apps with native application secrets..."
az deployment group create \
  --name "${DEPLOYMENT_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --template-file "${TEMPLATE_FILE}" \
  --parameters "@${PARAMETERS_FILE}" \
  --parameters \
    location="${LOCATION}" \
    namePrefix="${NAME_PREFIX}" \
    environmentName="${ENVIRONMENT_NAME}" \
    deployContainerApps=true \
    containerImage="${ACR_SERVER}/${IMAGE_TAG}" \
    containerRegistryServer="${ACR_SERVER}" \
    webExternalIngress=true \
    communicationServicesRoleDefinitionId="${COMMUNICATION_SERVICES_ROLE_DEFINITION_ID}" \
    ingestApiKey="${HIMALAYAWATCH_INGEST_API_KEY}" \
    writeApiKey="${HIMALAYAWATCH_WRITE_API_KEY}" \
    operatorApiKey="${HIMALAYAWATCH_OPERATOR_API_KEY}" \
    scientistApiKey="${HIMALAYAWATCH_SCIENTIST_API_KEY}" \
    authorityApiKey="${HIMALAYAWATCH_AUTHORITY_API_KEY}" \
    smsSender="${AZURE_SMS_SENDER}" \
    notificationWebhookSecret="${NOTIFICATION_WEBHOOK_SECRET}" \
  --output none

WEB_URL="$(az deployment group show \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${DEPLOYMENT_NAME}" \
  --query properties.outputs.CONTAINER_APP_URL.value \
  --output tsv)"

unset HIMALAYAWATCH_INGEST_API_KEY
unset HIMALAYAWATCH_WRITE_API_KEY
unset HIMALAYAWATCH_OPERATOR_API_KEY
unset HIMALAYAWATCH_SCIENTIST_API_KEY
unset HIMALAYAWATCH_AUTHORITY_API_KEY
unset NOTIFICATION_WEBHOOK_SECRET

echo
echo "Deployment completed without Azure Key Vault."
echo "Application: ${WEB_URL}"
echo "Health check: ${WEB_URL}/healthz"
