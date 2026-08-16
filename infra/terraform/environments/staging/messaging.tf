resource "azurerm_servicebus_namespace" "staging" {
  name                          = local.servicebus_name
  location                      = azurerm_resource_group.staging.location
  resource_group_name           = azurerm_resource_group.staging.name
  sku                           = "Basic"
  local_auth_enabled            = false
  public_network_access_enabled = true
  minimum_tls_version           = "1.2"
  tags                          = var.tags
}

resource "azurerm_servicebus_queue" "analysis" {
  name                                 = local.queue_name
  namespace_id                         = azurerm_servicebus_namespace.staging.id
  lock_duration                        = "PT5M"
  max_delivery_count                   = 5
  default_message_ttl                  = "P1D"
  dead_lettering_on_message_expiration = true
  batched_operations_enabled           = true
  requires_duplicate_detection         = false
  requires_session                     = false
  max_size_in_megabytes                = 1024
}
