targetScope = 'resourceGroup'

@description('Azure region for resources that support regional placement.')
param location string = resourceGroup().location

@description('Short lowercase prefix used in generated resource names.')
@minLength(3)
@maxLength(12)
param namePrefix string = 'hwatch'

@description('Deployment environment label.')
@allowed([
  'dev'
  'test'
  'prod'
])
param environmentName string = 'prod'

@description('Tags applied to all supported resources.')
param tags object = {}

@description('Event Hubs namespace SKU.')
@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param eventHubsSku string = 'Standard'

@description('Event Hubs namespace throughput or processing-unit capacity.')
@minValue(1)
param eventHubsCapacity int = 1

@description('Number of Event Hub partitions. Increasing later is restricted for some SKUs.')
@minValue(2)
@maxValue(32)
param eventHubPartitionCount int = 4

@description('Event retention in days.')
@minValue(1)
@maxValue(7)
param eventHubRetentionDays int = 7

@description('Consumer group used by HimalayaWatch processors.')
param consumerGroupName string = 'himalayawatch-processors'

@description('Event hub used for normalized observation envelopes.')
param eventHubName string = 'observations'

@description('Blob container for immutable raw-event archives.')
param archiveContainerName string = 'raw-events'

@description('Blob container used by the Event Hubs checkpoint store.')
param checkpointContainerName string = 'eventhub-checkpoints'

@description('Storage account redundancy SKU.')
@allowed([
  'Standard_LRS'
  'Standard_ZRS'
  'Standard_GRS'
  'Standard_GZRS'
])
param storageSku string = 'Standard_ZRS'

@description('Soft-delete retention for blobs and containers.')
@minValue(1)
@maxValue(365)
param storageRetentionDays int = 30

@description('Log Analytics workspace retention.')
@minValue(30)
@maxValue(730)
param logRetentionDays int = 90

@description('Deploy Azure Communication Services for optional SMS delivery.')
param deployCommunicationServices bool = true

@description('Deploy an Azure-managed Email Communication Services domain and link it to ACS.')
param deployEmailService bool = true

@description('ACS data location. Global resources are deployed into this data geography.')
param communicationDataLocation string = 'United States'

@description('Optional verified ACS email sender. Empty uses the Azure-managed DoNotReply sender.')
param emailSenderAddress string = ''

@description('Optional fully qualified ACS data-plane role definition resource ID. Leave empty when no supported least-privilege role has been selected for this subscription.')
param communicationServicesRoleDefinitionId string = ''

@description('Deploy the optional Azure Container Apps runtime. Keep false until containerImage is set to a deployable image.')
param deployContainerApps bool = false

@description('Container image used by the web app and worker jobs. Required when deployContainerApps is true.')
param containerImage string = 'false'

@description('Expose the web app publicly. Keep false until strong API keys are supplied as secure deployment parameters.')
param webExternalIngress bool = false

@description('Optional private registry server, such as myregistry.azurecr.io. Leave empty for a public image.')
param containerRegistryServer string = ''

@description('Cron schedule for bounded Event Hubs consumer executions.')
param consumerCronExpression string = '*/2 * * * *'

@description('Cron schedule for notification worker executions.')
param notificationCronExpression string = '1-59/10 * * * *'

@secure()
@description('Ingestion API key stored as a native Container Apps secret. Required for public ingress.')
param ingestApiKey string = ''

@secure()
@description('Optional backward-compatible write API key stored as a native Container Apps secret.')
param writeApiKey string = ''

@secure()
@description('Operator API key stored as a native Container Apps secret. Required for public ingress.')
param operatorApiKey string = ''

@secure()
@description('Optional scientist-role API key. Empty falls back to operatorApiKey.')
param scientistApiKey string = ''

@secure()
@description('Optional authority-role API key. Empty falls back to operatorApiKey.')
param authorityApiKey string = ''

@secure()
@description('Optional ACS SMS sender identifier stored as a native Container Apps secret.')
param smsSender string = ''

@secure()
@description('Optional notification webhook HMAC secret stored as a native Container Apps secret.')
param notificationWebhookSecret string = ''

var suffix = toLower(uniqueString(subscription().subscriptionId, resourceGroup().id, namePrefix, environmentName))
var baseName = '${toLower(namePrefix)}-${environmentName}-${take(suffix, 6)}'
var storageAccountName = take(replace('${toLower(namePrefix)}${environmentName}${suffix}', '-', ''), 24)
var validatedIngestApiKey = deployContainerApps && webExternalIngress && empty(ingestApiKey)
  ? fail('ingestApiKey is required when Container Apps public ingress is enabled.')
  : ingestApiKey
var validatedOperatorApiKey = deployContainerApps && webExternalIngress && empty(operatorApiKey)
  ? fail('operatorApiKey is required when Container Apps public ingress is enabled.')
  : operatorApiKey
var commonTags = union({
  application: 'HimalayaWatch'
  environment: environmentName
  managedBy: 'Bicep'
}, tags)

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    workspaceName: '${baseName}-law'
    applicationInsightsName: '${baseName}-appi'
    logRetentionDays: logRetentionDays
    tags: commonTags
  }
}

module identities 'modules/identities.bicep' = {
  name: 'identities'
  params: {
    location: location
    publisherIdentityName: '${baseName}-publisher-id'
    processorIdentityName: '${baseName}-processor-id'
    tags: commonTags
  }
}

module messaging 'modules/event-hubs.bicep' = {
  name: 'event-hubs'
  params: {
    location: location
    namespaceName: '${baseName}-ehns'
    eventHubName: eventHubName
    consumerGroupName: consumerGroupName
    skuName: eventHubsSku
    capacity: eventHubsCapacity
    partitionCount: eventHubPartitionCount
    retentionDays: eventHubRetentionDays
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
    publisherPrincipalId: identities.outputs.publisherPrincipalId
    processorPrincipalId: identities.outputs.processorPrincipalId
    tags: commonTags
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    location: location
    storageAccountName: storageAccountName
    archiveContainerName: archiveContainerName
    checkpointContainerName: checkpointContainerName
    skuName: storageSku
    retentionDays: storageRetentionDays
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
    processorPrincipalId: identities.outputs.processorPrincipalId
    tags: commonTags
  }
}

module communication 'modules/communication-services.bicep' = if (deployCommunicationServices) {
  name: 'communication-services'
  params: {
    communicationServiceName: '${baseName}-acs'
    emailServiceName: '${baseName}-email'
    dataLocation: communicationDataLocation
    deployEmailService: deployEmailService
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
    processorPrincipalId: identities.outputs.processorPrincipalId
    communicationServicesRoleDefinitionId: communicationServicesRoleDefinitionId
    tags: commonTags
  }
}

module compute 'modules/container-apps.bicep' = if (deployContainerApps) {
  name: 'container-apps'
  params: {
    location: location
    environmentName: '${baseName}-cae'
    webAppName: take('${baseName}-web', 32)
    consumerJobName: take('${baseName}-consumer', 32)
    notificationJobName: take('${baseName}-notifications', 32)
    runtimeStorageAccountName: take(replace('${toLower(namePrefix)}${environmentName}run${suffix}', '-', ''), 24)
    containerImage: containerImage
    webExternalIngress: webExternalIngress
    containerRegistryServer: containerRegistryServer
    consumerCronExpression: consumerCronExpression
    notificationCronExpression: notificationCronExpression
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
    publisherIdentityResourceId: identities.outputs.publisherIdentityResourceId
    publisherIdentityClientId: identities.outputs.publisherClientId
    processorIdentityResourceId: identities.outputs.processorIdentityResourceId
    processorIdentityClientId: identities.outputs.processorClientId
    eventHubsNamespace: messaging.outputs.fullyQualifiedNamespace
    eventHubName: messaging.outputs.eventHubName
    consumerGroupName: messaging.outputs.consumerGroupName
    blobServiceUrl: storage.outputs.blobServiceUrl
    archiveContainerName: storage.outputs.archiveContainerName
    checkpointContainerName: storage.outputs.checkpointContainerName
    communicationEndpoint: deployCommunicationServices ? communication!.outputs.endpoint : ''
    emailSenderAddress: !empty(emailSenderAddress)
      ? emailSenderAddress
      : (deployCommunicationServices && deployEmailService
        ? communication!.outputs.emailSenderAddress
        : '')
    ingestApiKey: validatedIngestApiKey
    writeApiKey: writeApiKey
    operatorApiKey: validatedOperatorApiKey
    scientistApiKey: scientistApiKey
    authorityApiKey: authorityApiKey
    smsSender: smsSender
    notificationWebhookSecret: notificationWebhookSecret
    applicationInsightsConnectionString: monitoring.outputs.applicationInsightsConnectionString
    tags: commonTags
  }
}

output publisherIdentityResourceId string = identities.outputs.publisherIdentityResourceId
output publisherIdentityClientId string = identities.outputs.publisherClientId
output processorIdentityResourceId string = identities.outputs.processorIdentityResourceId
output processorIdentityClientId string = identities.outputs.processorClientId
output AZURE_EVENTHUB_NAMESPACE string = messaging.outputs.fullyQualifiedNamespace
output AZURE_EVENTHUB_NAME string = messaging.outputs.eventHubName
output AZURE_EVENTHUB_CONSUMER_GROUP string = messaging.outputs.consumerGroupName
output AZURE_ARCHIVE_STORAGE_ACCOUNT_URL string = storage.outputs.blobServiceUrl
output AZURE_ARCHIVE_CONTAINER string = storage.outputs.archiveContainerName
output AZURE_CHECKPOINT_STORAGE_ACCOUNT_URL string = storage.outputs.blobServiceUrl
output AZURE_CHECKPOINT_CONTAINER string = storage.outputs.checkpointContainerName
output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.applicationInsightsConnectionString
output APPLICATIONINSIGHTS_WORKSPACE_ID string = monitoring.outputs.logAnalyticsWorkspaceId
output AZURE_COMMUNICATION_ENDPOINT string = deployCommunicationServices ? communication!.outputs.endpoint : ''
output AZURE_EMAIL_SENDER string = !empty(emailSenderAddress)
  ? emailSenderAddress
  : (deployCommunicationServices && deployEmailService
    ? communication!.outputs.emailSenderAddress
    : '')
output CONTAINER_APP_URL string = deployContainerApps ? compute!.outputs.webAppUrl : ''
output CONSUMER_JOB_NAME string = deployContainerApps ? compute!.outputs.consumerJobName : ''
output NOTIFICATION_JOB_NAME string = deployContainerApps ? compute!.outputs.notificationJobName : ''
output appSettings object = {
  INGESTION_MODE: 'eventhub'
  AZURE_EVENTHUB_NAMESPACE: messaging.outputs.fullyQualifiedNamespace
  AZURE_EVENTHUB_NAME: messaging.outputs.eventHubName
  AZURE_EVENTHUB_CONSUMER_GROUP: messaging.outputs.consumerGroupName
  AZURE_ARCHIVE_STORAGE_ACCOUNT_URL: storage.outputs.blobServiceUrl
  AZURE_ARCHIVE_CONTAINER: storage.outputs.archiveContainerName
  AZURE_CHECKPOINT_STORAGE_ACCOUNT_URL: storage.outputs.blobServiceUrl
  AZURE_CHECKPOINT_CONTAINER: storage.outputs.checkpointContainerName
  AZURE_COMMUNICATION_ENDPOINT: deployCommunicationServices ? communication!.outputs.endpoint : ''
  AZURE_EMAIL_SENDER: !empty(emailSenderAddress)
    ? emailSenderAddress
    : (deployCommunicationServices && deployEmailService
      ? communication!.outputs.emailSenderAddress
      : '')
  APPLICATIONINSIGHTS_CONNECTION_STRING: monitoring.outputs.applicationInsightsConnectionString
}
