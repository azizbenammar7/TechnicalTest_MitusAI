resource "azurerm_resource_group" "staging" {
  name     = local.resource_group_name
  location = var.location
  tags     = var.tags
}

resource "azurerm_user_assigned_identity" "api" {
  name                = "id-${local.name_prefix}-api"
  location            = azurerm_resource_group.staging.location
  resource_group_name = azurerm_resource_group.staging.name
  tags                = var.tags
}

resource "azurerm_user_assigned_identity" "frontend" {
  name                = "id-${local.name_prefix}-frontend"
  location            = azurerm_resource_group.staging.location
  resource_group_name = azurerm_resource_group.staging.name
  tags                = var.tags
}

resource "azurerm_user_assigned_identity" "worker" {
  name                = "id-${local.name_prefix}-worker"
  location            = azurerm_resource_group.staging.location
  resource_group_name = azurerm_resource_group.staging.name
  tags                = var.tags
}

resource "azurerm_container_registry" "staging" {
  name                          = local.acr_name
  resource_group_name           = azurerm_resource_group.staging.name
  location                      = azurerm_resource_group.staging.location
  sku                           = "Basic"
  admin_enabled                 = false
  public_network_access_enabled = true
  tags                          = var.tags
}

resource "azurerm_log_analytics_workspace" "staging" {
  name                = "log-${local.name_prefix}"
  location            = azurerm_resource_group.staging.location
  resource_group_name = azurerm_resource_group.staging.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 0.1
  # The Container Apps environment's legacy `log-analytics` destination sends
  # console/system logs with the workspace shared key. Disabling local auth
  # silently prevented ingestion (no ContainerApp* tables were created).
  # Application telemetry still uses Entra-authenticated Azure Monitor export.
  local_authentication_enabled = true
  tags                         = var.tags
}
