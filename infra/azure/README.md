# HimalayaWatch Azure infrastructure without Key Vault

This deployment creates the Azure event and notification pipeline without
deploying or accessing Azure Key Vault. It is intended for tenants where Key
Vault data-plane access is blocked by network policy or Conditional Access.

If an earlier version already created a vault, Azure Resource Manager's default
incremental deployment mode leaves that resource in place. The new template
does not reference it, grant roles on it, or require access to it. Remove the
unused vault later only through your organization's approved retention and
deletion process.

Sensitive values are accepted as Bicep `@secure()` parameters and stored in the
native secret stores of Azure Container Apps and Container Apps Jobs. Azure
Resource Manager masks secure parameter values in deployment records. Event
Hubs, Storage, Azure Container Registry (ACR), and Azure Communication Services
(ACS) continue to use managed identities instead of connection strings.

> This removes the Key Vault dependency; it does not bypass tenant Conditional
> Access. The account performing the ARM deployment must still be permitted to
> use Azure Portal, Cloud Shell, Azure CLI, or an approved deployment identity.

## Resources

- Event Hubs namespace, `observations` event hub, and processor consumer group
- Storage account with private `raw-events` and `eventhub-checkpoints` containers
- Log Analytics workspace and workspace-based Application Insights
- Optional ACS resource with an Azure-managed Email Communication Services domain
- Separate publisher and processor user-assigned managed identities
- Azure Monitor diagnostic settings for Event Hubs, Blob Storage, and ACS
- Optional Container Apps environment containing:
  - a Flask web application with health probes on `GET /healthz`
  - a bounded scheduled Event Hubs consumer job
  - a scheduled notification worker job
  - a dedicated Azure Files share mounted at `/app/instance` for SQLite

The SQLite runtime remains limited to one web replica and one worker execution
at a time. Move state to an external transactional database before scaling this
prototype horizontally or treating it as highly available.

## Secret handling

The deployment accepts these secure parameters:

| Parameter | Used by | Required |
|---|---|---|
| `ingestApiKey` | Web app `INGEST_API_KEY` | Required when public ingress is enabled |
| `operatorApiKey` | Web app `OPERATOR_API_KEY` | Required when public ingress is enabled |
| `writeApiKey` | Web app `WRITE_API_KEY` compatibility fallback | Optional |
| `smsSender` | Notification job `AZURE_SMS_SENDER` | Optional; required for SMS |
| `notificationWebhookSecret` | Notification job webhook HMAC | Optional |

Do not put values for these parameters in JSON files or source control. Supply
them through secure prompts, environment-variable expansion, an approved CI/CD
secret store, or the Azure Portal secure parameter controls.

Container Apps secrets are encrypted at rest and are not returned as plaintext
by normal ARM read operations. They are nevertheless not a centralized secret
management system: rotation, auditing, sharing across applications, and
fine-grained data-plane controls are more limited than Key Vault. Replace this
configuration with an organization-approved secret manager when one becomes
available.

## Recommended: deploy from Azure Cloud Shell Bash

Upload or clone the complete project so the repository root contains
`Dockerfile`, `requirements.txt`, `app.py`, and `infra/azure`.

```bash
cd ~/himalayan-glacier-monitor
chmod +x ./infra/azure/deploy-no-keyvault.sh

export ACR_NAME="hwatchacr<unique-suffix>"
export RESOURCE_GROUP="rg-himalayawatch-prod"
export LOCATION="centralindia"
export COMMUNICATION_SERVICES_ROLE_DEFINITION_ID="/subscriptions/<subscription-id>/providers/Microsoft.Authorization/roleDefinitions/<approved-acs-role-id>"

./infra/azure/deploy-no-keyvault.sh
```

The script securely prompts for the ingestion and operator API keys. Typed
values are not echoed. Optional notification values can be supplied before
running it:

```bash
export AZURE_SMS_SENDER="+1XXXXXXXXXX"
read -rsp "Webhook HMAC secret: " NOTIFICATION_WEBHOOK_SECRET
echo
export NOTIFICATION_WEBHOOK_SECRET

./infra/azure/deploy-no-keyvault.sh
```

If an approved automation already injects secrets as environment variables:

```bash
export HIMALAYAWATCH_INGEST_API_KEY="<strong-random-value>"
export HIMALAYAWATCH_OPERATOR_API_KEY="<different-strong-random-value>"
export HIMALAYAWATCH_WRITE_API_KEY=""
export AZURE_SMS_SENDER=""
export NOTIFICATION_WEBHOOK_SECRET=""

./infra/azure/deploy-no-keyvault.sh
```

The script:

1. Creates or updates the resource group.
2. Deploys identities, Event Hubs, Storage, monitoring, and ACS.
3. Creates ACR when it does not exist and builds the project image.
4. Grants `AcrPull` to the publisher and processor identities.
5. Deploys Container Apps with native secrets.
6. Prints the application and health-check URLs.

The deploying identity still needs permission to create resources and role
assignments. If Conditional Access blocks `az account show` or the ARM
deployment itself, use an organization-approved managed device, deployment
service principal/workload identity, or Azure Portal session. Removing Key
Vault cannot bypass that tenant policy.

Leave `COMMUNICATION_SERVICES_ROLE_DEFINITION_ID` empty when notification
delivery is not being enabled yet. When it is enabled, use the full resource ID
of the ACS data-plane role approved by the subscription administrator.

## Manual Bash deployment

Run the base deployment first so the managed identities exist:

```bash
az deployment group create \
  --name "himalayawatch-prod" \
  --resource-group "rg-himalayawatch-prod" \
  --template-file "./infra/azure/main.bicep" \
  --parameters "@./infra/azure/main.parameters.example.json" \
  --parameters deployContainerApps=false
```

Build the image and grant both identities `AcrPull`:

```bash
acrName="hwatchacr<unique-suffix>"
imageTag="himalayawatch:v1"

az acr create \
  --resource-group "rg-himalayawatch-prod" \
  --name "$acrName" \
  --sku Basic

az acr build --registry "$acrName" --image "$imageTag" .

acrServer="$(az acr show --name "$acrName" --query loginServer -o tsv)"
acrId="$(az acr show --name "$acrName" --query id -o tsv)"

publisherIdentityId="$(az deployment group show \
  --resource-group "rg-himalayawatch-prod" \
  --name "himalayawatch-prod" \
  --query properties.outputs.publisherIdentityResourceId.value -o tsv)"
processorIdentityId="$(az deployment group show \
  --resource-group "rg-himalayawatch-prod" \
  --name "himalayawatch-prod" \
  --query properties.outputs.processorIdentityResourceId.value -o tsv)"

publisherPrincipalId="$(az identity show --ids "$publisherIdentityId" --query principalId -o tsv)"
processorPrincipalId="$(az identity show --ids "$processorIdentityId" --query principalId -o tsv)"

az role assignment create \
  --assignee-object-id "$publisherPrincipalId" \
  --assignee-principal-type ServicePrincipal \
  --role AcrPull \
  --scope "$acrId"

az role assignment create \
  --assignee-object-id "$processorPrincipalId" \
  --assignee-principal-type ServicePrincipal \
  --role AcrPull \
  --scope "$acrId"
```

Read secrets without echoing them, then deploy the runtime:

```bash
read -rsp "Ingestion API key: " ingestApiKey
echo
read -rsp "Operator API key: " operatorApiKey
echo

az deployment group create \
  --name "himalayawatch-prod" \
  --resource-group "rg-himalayawatch-prod" \
  --template-file "./infra/azure/main.bicep" \
  --parameters "@./infra/azure/main.parameters.example.json" \
  --parameters \
    deployContainerApps=true \
    containerImage="$acrServer/$imageTag" \
    containerRegistryServer="$acrServer" \
    webExternalIngress=true \
    ingestApiKey="$ingestApiKey" \
    operatorApiKey="$operatorApiKey"

unset ingestApiKey operatorApiKey
```

## Azure Portal deployment

Compile the Bicep template locally or in an approved Cloud Shell session:

```bash
az bicep build --file ./infra/azure/main.bicep
```

This produces `infra/azure/main.json`. In Azure Portal:

1. Open **Deploy a custom template**.
2. Select **Build your own template in the editor**.
3. Upload `infra/azure/main.json`, then save.
4. Select the resource group and enter ordinary configuration values.
5. For the first deployment, leave **Deploy Container Apps** disabled.
6. Build the image and grant ACR pull as described above.
7. Deploy the same template again with Container Apps enabled.
8. Enter `ingestApiKey` and `operatorApiKey` in the secure fields.
9. Enter `smsSender` and `notificationWebhookSecret` only when used.

Secure parameter values are masked. Do not paste secret values into the template
JSON or a checked-in parameters file.

## Verification

Inspect deployment outputs:

```bash
az deployment group show \
  --resource-group "rg-himalayawatch-prod" \
  --name "himalayawatch-prod" \
  --query properties.outputs
```

Verify the web application:

```bash
appUrl="$(az deployment group show \
  --resource-group "rg-himalayawatch-prod" \
  --name "himalayawatch-prod" \
  --query properties.outputs.CONTAINER_APP_URL.value -o tsv)"

curl --fail --show-error "$appUrl/healthz"
```

Trigger worker jobs manually when needed:

```bash
az containerapp job start \
  --resource-group "rg-himalayawatch-prod" \
  --name "<consumer-job-name>"

az containerapp job start \
  --resource-group "rg-himalayawatch-prod" \
  --name "<notification-job-name>"
```

## Managed identities and authorization

The deployment grants:

- `Azure Event Hubs Data Sender` to the publisher identity
- `Azure Event Hubs Data Receiver` to the processor identity
- `Storage Blob Data Contributor` to the processor identity
- Optional ACS data-plane access to the processor identity when
  `communicationServicesRoleDefinitionId` is supplied

Both identities need `AcrPull` on the selected registry. No Key Vault roles,
access policies, private endpoints, secrets, or vault network configuration are
created.

## Operational boundary

HimalayaWatch notifications are **human-authorized decision support**. They are
not an official emergency-warning system and must not bypass responsible
authorities, established warning channels, recipient consent, telecom rules, or
human review.
