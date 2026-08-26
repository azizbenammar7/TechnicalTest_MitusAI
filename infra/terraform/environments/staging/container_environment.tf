resource "azurerm_container_app_environment" "staging" {
  name                = "cae-${local.name_prefix}"
  location            = azurerm_resource_group.staging.location
  resource_group_name = azurerm_resource_group.staging.name

  # Route platform logs through Azure Monitor (not the legacy `log-analytics`
  # destination). The legacy path ingests console/system logs with the
  # workspace shared key via the Azure Monitor HTTP Data Collector API, which
  # Microsoft is retiring (support ends 2026-09-14) and which silently drops
  # data when the workspace has local auth disabled. Diagnostic settings on
  # this environment forward the same categories to the workspace over the
  # Entra-authenticated Azure Monitor pipeline instead; see observability.tf.
  # `logs_destination` and the removed `log_analytics_workspace_id` are both
  # in-place updates in azurerm 4.81.0 (neither is ForceNew), so this switch
  # does not replace the environment.
  logs_destination = "azure-monitor"

  infrastructure_subnet_id       = azurerm_subnet.container_apps.id
  internal_load_balancer_enabled = false
  zone_redundancy_enabled        = false

  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
    minimum_count         = 0
    maximum_count         = 0
  }

  tags = var.tags
}
