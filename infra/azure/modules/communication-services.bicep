param communicationServiceName string
param emailServiceName string
param dataLocation string
param deployEmailService bool
param logAnalyticsWorkspaceId string
param processorPrincipalId string
param communicationServicesRoleDefinitionId string
param tags object

resource emailService 'Microsoft.Communication/emailServices@2023-03-31' = if (deployEmailService) {
  name: emailServiceName
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
  }
}

resource emailDomain 'Microsoft.Communication/emailServices/domains@2023-03-31' = if (deployEmailService) {
  parent: emailService
  name: 'AzureManagedDomain'
  location: 'global'
  properties: {
    domainManagement: 'AzureManaged'
    userEngagementTracking: 'Disabled'
  }
}

resource communicationService 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: communicationServiceName
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
    linkedDomains: deployEmailService ? [
      emailDomain!.id
    ] : []
  }
}

resource processorRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(communicationServicesRoleDefinitionId)) {
  name: guid(communicationService.id, processorPrincipalId, communicationServicesRoleDefinitionId)
  scope: communicationService
  properties: {
    roleDefinitionId: communicationServicesRoleDefinitionId
    principalId: processorPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-to-log-analytics'
  scope: communicationService
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

output communicationServiceResourceId string = communicationService.id
output endpoint string = 'https://${communicationService.properties.hostName}'
output emailSenderAddress string = deployEmailService
  ? 'DoNotReply@${emailDomain!.properties.mailFromSenderDomain}'
  : ''
