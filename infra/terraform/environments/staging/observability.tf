resource "azurerm_application_insights" "staging" {
  name                         = "appi-${local.name_prefix}"
  location                     = azurerm_resource_group.staging.location
  resource_group_name          = azurerm_resource_group.staging.name
  workspace_id                 = azurerm_log_analytics_workspace.staging.id
  application_type             = "web"
  retention_in_days            = 30
  sampling_percentage          = 20
  local_authentication_enabled = false
  internet_ingestion_enabled   = true
  internet_query_enabled       = true
  ip_masking_enabled           = true
  tags                         = var.tags
}

# The SDK authenticates ingestion with each workload's existing user-assigned
# identity. The connection string identifies endpoints and is stored as a
# Container Apps secret as defense in depth.
resource "azurerm_role_assignment" "application_insights_api" {
  scope                            = azurerm_application_insights.staging.id
  role_definition_name             = "Monitoring Metrics Publisher"
  principal_id                     = azurerm_user_assigned_identity.api.principal_id
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "application_insights_worker" {
  scope                            = azurerm_application_insights.staging.id
  role_definition_name             = "Monitoring Metrics Publisher"
  principal_id                     = azurerm_user_assigned_identity.worker.principal_id
  skip_service_principal_aad_check = true
}
