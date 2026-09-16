param location string
param environmentName string
param webAppName string
param consumerJobName string
param notificationJobName string
param runtimeStorageAccountName string
param containerImage string
param webExternalIngress bool
param containerRegistryServer string
param consumerCronExpression string
param notificationCronExpression string
param logAnalyticsWorkspaceId string
param publisherIdentityResourceId string
param publisherIdentityClientId string
param processorIdentityResourceId string
param processorIdentityClientId string
param eventHubsNamespace string
param eventHubName string
param consumerGroupName string
param blobServiceUrl string
param archiveContainerName string
param checkpointContainerName string
param communicationEndpoint string
param emailSenderAddress string
@secure()
param ingestApiKey string
@secure()
param writeApiKey string
@secure()
param operatorApiKey string
@secure()
param smsSender string
@secure()
param notificationWebhookSecret string
param applicationInsightsConnectionString string
param tags object

var dataVolumeName = 'sqlite-instance'
var environmentStorageName = 'sqlite-instance'
var databasePath = '/app/instance/glacier_monitor.db'
var hasRegistry = !empty(containerRegistryServer)

var webSecrets = concat(
  empty(ingestApiKey) ? [] : [
    {
      name: 'ingest-api-key'
      value: ingestApiKey
    }
  ],
  empty(writeApiKey) ? [] : [
    {
      name: 'write-api-key'
      value: writeApiKey
    }
  ],
  empty(operatorApiKey) ? [] : [
    {
      name: 'operator-api-key'
      value: operatorApiKey
    }
  ]
)

var webSecretEnvironment = concat(
  empty(ingestApiKey) ? [] : [
    {
      name: 'INGEST_API_KEY'
      secretRef: 'ingest-api-key'
    }
  ],
  empty(writeApiKey) ? [] : [
    {
      name: 'WRITE_API_KEY'
      secretRef: 'write-api-key'
    }
  ],
  empty(operatorApiKey) ? [] : [
    {
      name: 'OPERATOR_API_KEY'
      secretRef: 'operator-api-key'
    }
  ]
)

var notificationSecrets = concat(
  empty(smsSender) ? [] : [
    {
      name: 'sms-sender'
      value: smsSender
    }
  ],
  empty(notificationWebhookSecret) ? [] : [
    {
      name: 'webhook-secret'
      value: notificationWebhookSecret
    }
  ]
)

var notificationSecretEnvironment = concat(
  empty(smsSender) ? [] : [
    {
      name: 'AZURE_SMS_SENDER'
      secretRef: 'sms-sender'
    }
  ],
  empty(notificationWebhookSecret) ? [] : [
    {
      name: 'NOTIFICATION_WEBHOOK_SECRET'
      secretRef: 'webhook-secret'
    }
  ]
)

var dataVolume = [
  {
    name: dataVolumeName
    storageType: 'EmptyDir'
  }
]

var dataVolumeMount = [
  {
    volumeName: dataVolumeName
    mountPath: '/app/instance'
  }
]

resource runtimeStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: runtimeStorageAccountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    allowSharedKeyAccess: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
    supportsHttpsTrafficOnly: true
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: runtimeStorage
  name: 'default'
  properties: {
    shareDeleteRetentionPolicy: {
      enabled: true
      days: 30
    }
  }
}

resource dataShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: 'sqlite-instance'
  properties: {
    accessTier: 'TransactionOptimized'
    enabledProtocols: 'SMB'
    shareQuota: 5
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
    zoneRedundant: false
  }
}

resource environmentStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: environment
  name: environmentStorageName
  properties: {
    azureFile: {
      accountName: runtimeStorage.name
      accountKey: runtimeStorage.listKeys().keys[0].value
      accessMode: 'ReadWrite'
      shareName: dataShare.name
    }
  }
}

resource environmentDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-to-log-analytics'
  scope: environment
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

resource webApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: webAppName
  location: location
  tags: tags
  dependsOn: [
    environmentStorage
  ]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${publisherIdentityResourceId}': {}
    }
  }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: webExternalIngress
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: !hasRegistry ? [] : [
        {
          server: containerRegistryServer
          identity: publisherIdentityResourceId
        }
      ]
      secrets: webSecrets
    }
    template: {
      containers: [
        {
          name: 'web'
          image: containerImage
          env: concat([
            {
              name: 'PORT'
              value: '8000'
            }
            {
              name: 'DATABASE_PATH'
              value: databasePath
            }
            {
              name: 'INGESTION_MODE'
              value: 'eventhub'
            }
            {
              name: 'AZURE_EVENTHUB_NAMESPACE'
              value: eventHubsNamespace
            }
            {
              name: 'AZURE_EVENTHUB_NAME'
              value: eventHubName
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: publisherIdentityClientId
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: applicationInsightsConnectionString
            }
          ], webSecretEnvironment)
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/healthz'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 5
              timeoutSeconds: 3
              failureThreshold: 12
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: 8000
                scheme: 'HTTP'
              }
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 3
              successThreshold: 1
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 30
              periodSeconds: 30
              timeoutSeconds: 3
              failureThreshold: 3
            }
          ]
          volumeMounts: dataVolumeMount
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
      volumes: dataVolume
    }
  }
}

resource consumerJob 'Microsoft.App/jobs@2024-03-01' = {
  name: consumerJobName
  location: location
  tags: tags
  dependsOn: [
    environmentStorage
  ]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${processorIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      replicaTimeout: 120
      replicaRetryLimit: 1
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: consumerCronExpression
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: !hasRegistry ? [] : [
        {
          server: containerRegistryServer
          identity: processorIdentityResourceId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'consumer'
          image: containerImage
          command: [
            'python'
            '-m'
            'flask'
            '--app'
            'app'
            'consume-eventhub'
            '--max-events'
            '100'
            '--wait-seconds'
            '30'
          ]
          env: [
            {
              name: 'DATABASE_PATH'
              value: databasePath
            }
            {
              name: 'AZURE_EVENTHUB_NAMESPACE'
              value: eventHubsNamespace
            }
            {
              name: 'AZURE_EVENTHUB_NAME'
              value: eventHubName
            }
            {
              name: 'AZURE_EVENTHUB_CONSUMER_GROUP'
              value: consumerGroupName
            }
            {
              name: 'AZURE_ARCHIVE_STORAGE_ACCOUNT_URL'
              value: blobServiceUrl
            }
            {
              name: 'AZURE_ARCHIVE_CONTAINER'
              value: archiveContainerName
            }
            {
              name: 'AZURE_CHECKPOINT_STORAGE_ACCOUNT_URL'
              value: blobServiceUrl
            }
            {
              name: 'AZURE_CHECKPOINT_CONTAINER'
              value: checkpointContainerName
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: processorIdentityClientId
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          volumeMounts: dataVolumeMount
        }
      ]
      volumes: dataVolume
    }
  }
}

resource notificationJob 'Microsoft.App/jobs@2024-03-01' = {
  name: notificationJobName
  location: location
  tags: tags
  dependsOn: [
    environmentStorage
  ]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${processorIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      replicaTimeout: 120
      replicaRetryLimit: 1
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: notificationCronExpression
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: !hasRegistry ? [] : [
        {
          server: containerRegistryServer
          identity: processorIdentityResourceId
        }
      ]
      secrets: notificationSecrets
    }
    template: {
      containers: [
        {
          name: 'notifications'
          image: containerImage
          command: [
            'python'
            '-m'
            'flask'
            '--app'
            'app'
            'process-notifications'
            '--limit'
            '100'
          ]
          env: concat([
            {
              name: 'DATABASE_PATH'
              value: databasePath
            }
            {
              name: 'AZURE_COMMUNICATION_ENDPOINT'
              value: communicationEndpoint
            }
            {
              name: 'AZURE_EMAIL_SENDER'
              value: emailSenderAddress
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: processorIdentityClientId
            }
          ], notificationSecretEnvironment)
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          volumeMounts: dataVolumeMount
        }
      ]
      volumes: dataVolume
    }
  }
}

output environmentResourceId string = environment.id
output webAppName string = webApp.name
output webAppUrl string = 'https://${webApp.properties.configuration.ingress.fqdn}'
output consumerJobName string = consumerJob.name
output notificationJobName string = notificationJob.name
