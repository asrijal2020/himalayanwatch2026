param location string
param namespaceName string
param eventHubName string
param consumerGroupName string
param skuName string
param capacity int
param partitionCount int
param retentionDays int
param logAnalyticsWorkspaceId string
param publisherPrincipalId string
param processorPrincipalId string
param tags object

var eventHubsDataSenderRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '2b629674-e913-4c01-ae53-ef4638d8f975'
)
var eventHubsDataReceiverRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'a638d3c7-ab3a-418d-83e6-5f17a39d4fde'
)

resource eventHubsNamespace 'Microsoft.EventHub/namespaces@2022-10-01-preview' = {
  name: namespaceName
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuName
    capacity: capacity
  }
  properties: {
    disableLocalAuth: true
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    zoneRedundant: false
  }
}

resource eventHub 'Microsoft.EventHub/namespaces/eventhubs@2022-10-01-preview' = {
  parent: eventHubsNamespace
  name: eventHubName
  properties: {
    messageRetentionInDays: retentionDays
    partitionCount: partitionCount
    status: 'Active'
  }
}

resource consumerGroup 'Microsoft.EventHub/namespaces/eventhubs/consumergroups@2022-10-01-preview' = {
  parent: eventHub
  name: consumerGroupName
  properties: {
    userMetadata: 'HimalayaWatch event evaluation processors'
  }
}

resource publisherRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(eventHub.id, publisherPrincipalId, eventHubsDataSenderRoleId)
  scope: eventHub
  properties: {
    roleDefinitionId: eventHubsDataSenderRoleId
    principalId: publisherPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource processorRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(eventHub.id, processorPrincipalId, eventHubsDataReceiverRoleId)
  scope: eventHub
  properties: {
    roleDefinitionId: eventHubsDataReceiverRoleId
    principalId: processorPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-to-log-analytics'
  scope: eventHubsNamespace
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output namespaceResourceId string = eventHubsNamespace.id
output fullyQualifiedNamespace string = '${eventHubsNamespace.name}.servicebus.windows.net'
output eventHubName string = eventHub.name
output consumerGroupName string = consumerGroup.name
