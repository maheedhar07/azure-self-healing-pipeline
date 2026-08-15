// One-command infra for the self-healing pipeline pilot.
// Consumption (Y1) Function App + Storage + Key Vault + App Insights.
// Azure OpenAI is referenced (not created) so you can point at an existing deployment;
// flip `createOpenAi` to true to provision one here.
//
//   az group create -n rg-self-healing -l eastus
//   az deployment group create -g rg-self-healing -f infra/main.bicep -p namePrefix=selfheal

@description('Short prefix for resource names (lowercase, <= 11 chars).')
param namePrefix string = 'selfheal'

@description('Location for all resources.')
param location string = resourceGroup().location

@description('Name of an existing Azure OpenAI resource to grant access to.')
param openAiResourceName string = ''

var suffix = uniqueString(resourceGroup().id)
var storageName = toLower('${namePrefix}sa${suffix}')
var funcName = '${namePrefix}-func-${suffix}'
var planName = '${namePrefix}-plan'
var kvName = toLower('${namePrefix}-kv-${suffix}')
var aiName = '${namePrefix}-ai'

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: { minimumTlsVersion: 'TLS1_2', allowBlobPublicAccess: false }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: aiName
  location: location
  kind: 'web'
  properties: { Application_Type: 'web' }
}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  sku: { name: 'Y1', tier: 'Dynamic' }   // Consumption
  properties: {}
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true       // grant the function's identity 'Key Vault Secrets User'
    enableSoftDelete: true
  }
}

resource func 'Microsoft.Web/sites@2023-12-01' = {
  name: funcName
  location: location
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }   // PAT-free auth to ADO + Azure OpenAI
  properties: {
    serverFarmId: plan.id
    reserved: true
    siteConfig: {
      linuxFxVersion: 'Python|3.11'
      ftpsState: 'Disabled'
      appSettings: [
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        { name: 'AzureWebJobsStorage', value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'AZURE_OPENAI_DEPLOYMENT', value: 'gpt-4o-mini' }
        // Set these after deploy (or wire from Key Vault references):
        // AZURE_OPENAI_ENDPOINT, NOTIFY_WEBHOOK_URL, and ADO_PAT only if not using MI.
      ]
    }
  }
}

// Grant the function's managed identity access to the Azure OpenAI resource, if provided.
resource openAi 'Microsoft.CognitiveServices/accounts@2023-05-01' existing = if (!empty(openAiResourceName)) {
  name: openAiResourceName
}

resource openAiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(openAiResourceName)) {
  name: guid(func.id, aiName, 'openai-user')
  scope: openAi
  properties: {
    // 'Cognitive Services OpenAI User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
    principalId: func.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output functionAppName string = func.name
output functionPrincipalId string = func.identity.principalId
