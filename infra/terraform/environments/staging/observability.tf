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

# Container Apps platform logs (console + system) reach the workspace through
# the Azure Monitor pipeline configured by this diagnostic setting, replacing
# the legacy `log-analytics` shared-key destination. Ingestion is authenticated
# by the platform, so it works while the workspace has local auth disabled.
#
# `Dedicated` destination type lands rows in the resource-specific
# `ContainerAppConsoleLogs` and `ContainerAppSystemLogs` tables (no `_CL`
# suffix), which the KQL in docs/v2/observability.md targets. Only console and
# system logs are routed: HTTP logs and platform metrics are intentionally
# omitted to respect the 0.1 GB/day cap. Native platform metrics remain
# queryable and alertable without exporting AllMetrics into the workspace.
resource "azurerm_monitor_diagnostic_setting" "container_app_environment" {
  name                           = "diag-cae-${local.name_prefix}"
  target_resource_id             = azurerm_container_app_environment.staging.id
  log_analytics_workspace_id     = azurerm_log_analytics_workspace.staging.id
  log_analytics_destination_type = "Dedicated"

  enabled_log {
    category = "ContainerAppConsoleLogs"
  }

  enabled_log {
    category = "ContainerAppSystemLogs"
  }
}

# The SDK authenticates ingestion with each workload's existing user-assigned
# identity. When the component has local auth disabled, the instrumentation key
# inside the connection string is not a usable ingestion credential; the string
# only carries endpoint/resource discovery. It is therefore a plain environment
# variable (workloads.tf), not a Container Apps secret, which removes needless
# secret state and `listSecrets` surface. It must never be logged.
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
