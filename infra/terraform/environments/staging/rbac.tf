locals {
  frontend_roles = {
    acr_pull = {
      scope = azurerm_container_registry.staging.id
      role  = "AcrPull"
    }
  }

  api_roles = {
    blob_contributor = {
      scope = azurerm_storage_container.runs.id
      role  = "Storage Blob Data Contributor"
    }
    blob_delegator = {
      scope = azurerm_storage_account.staging.id
      role  = "Storage Blob Delegator"
    }
    servicebus_sender = {
      scope = azurerm_servicebus_queue.analysis.id
      role  = "Azure Service Bus Data Sender"
    }
    acr_pull = {
      scope = azurerm_container_registry.staging.id
      role  = "AcrPull"
    }
  }

  worker_roles = {
    blob_contributor = {
      scope = azurerm_storage_container.runs.id
      role  = "Storage Blob Data Contributor"
    }
    servicebus_receiver = {
      scope = azurerm_servicebus_queue.analysis.id
      role  = "Azure Service Bus Data Receiver"
    }
    acr_pull = {
      scope = azurerm_container_registry.staging.id
      role  = "AcrPull"
    }
  }
}

resource "azurerm_role_assignment" "frontend" {
  for_each = local.frontend_roles

  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = azurerm_user_assigned_identity.frontend.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "api" {
  for_each = local.api_roles

  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = azurerm_user_assigned_identity.api.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "worker" {
  for_each = local.worker_roles

  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = azurerm_user_assigned_identity.worker.principal_id
  principal_type       = "ServicePrincipal"
}
