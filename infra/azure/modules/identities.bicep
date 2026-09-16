param location string
param publisherIdentityName string
param processorIdentityName string
param tags object

resource publisherIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: publisherIdentityName
  location: location
  tags: tags
}

resource processorIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: processorIdentityName
  location: location
  tags: tags
}

output publisherIdentityResourceId string = publisherIdentity.id
output publisherPrincipalId string = publisherIdentity.properties.principalId
output publisherClientId string = publisherIdentity.properties.clientId
output processorIdentityResourceId string = processorIdentity.id
output processorPrincipalId string = processorIdentity.properties.principalId
output processorClientId string = processorIdentity.properties.clientId
