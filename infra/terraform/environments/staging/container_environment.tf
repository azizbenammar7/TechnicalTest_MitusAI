resource "azurerm_container_app_environment" "staging" {
  name                           = "cae-${local.name_prefix}"
  location                       = azurerm_resource_group.staging.location
  resource_group_name            = azurerm_resource_group.staging.name
  log_analytics_workspace_id     = azurerm_log_analytics_workspace.staging.id
  infrastructure_subnet_id       = azurerm_subnet.container_apps.id
  internal_load_balancer_enabled = false
  zone_redundancy_enabled        = false
  tags                           = var.tags
}
