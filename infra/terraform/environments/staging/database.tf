resource "random_password" "postgres_admin" {
  length  = 32
  special = false
}

resource "azurerm_postgresql_flexible_server" "staging" {
  name                          = local.postgres_name
  resource_group_name           = azurerm_resource_group.staging.name
  location                      = azurerm_resource_group.staging.location
  version                       = "16"
  delegated_subnet_id           = azurerm_subnet.postgres.id
  private_dns_zone_id           = azurerm_private_dns_zone.postgres.id
  public_network_access_enabled = false
  zone                          = "1"
  administrator_login           = "footballai_admin"
  administrator_password        = random_password.postgres_admin.result
  sku_name                      = var.postgres_sku_name
  storage_mb                    = var.postgres_storage_mb
  backup_retention_days         = 7
  geo_redundant_backup_enabled  = false

  authentication {
    active_directory_auth_enabled = false
    password_auth_enabled         = true
  }

  depends_on = [azurerm_private_dns_zone_virtual_network_link.postgres]
  tags       = var.tags
}

resource "azurerm_postgresql_flexible_server_database" "footballai" {
  name      = local.database_name
  server_id = azurerm_postgresql_flexible_server.staging.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}
