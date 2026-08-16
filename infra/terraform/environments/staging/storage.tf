resource "azurerm_storage_account" "staging" {
  name                              = local.storage_name
  resource_group_name               = azurerm_resource_group.staging.name
  location                          = azurerm_resource_group.staging.location
  account_tier                      = "Standard"
  account_replication_type          = "LRS"
  account_kind                      = "StorageV2"
  min_tls_version                   = "TLS1_2"
  https_traffic_only_enabled        = true
  allow_nested_items_to_be_public   = false
  shared_access_key_enabled         = false
  default_to_oauth_authentication   = true
  local_user_enabled                = false
  public_network_access_enabled     = true
  cross_tenant_replication_enabled  = false
  infrastructure_encryption_enabled = true

  blob_properties {
    versioning_enabled = true

    cors_rule {
      allowed_headers    = ["Content-Type", "x-ms-blob-type"]
      allowed_methods    = ["HEAD", "OPTIONS", "PUT"]
      allowed_origins    = [local.frontend_origin]
      exposed_headers    = ["ETag", "x-ms-request-id"]
      max_age_in_seconds = 600
    }

    delete_retention_policy {
      days = 7
    }

    container_delete_retention_policy {
      days = 7
    }
  }

  tags = var.tags
}

resource "azurerm_storage_container" "runs" {
  name                  = local.blob_container_name
  storage_account_id    = azurerm_storage_account.staging.id
  container_access_type = "private"
}
