output "resource_group_name" {
  description = "Resource group containing the isolated Terraform state backend."
  value       = azurerm_resource_group.state.name
}

output "storage_account_name" {
  description = "Storage account used by the AzureRM backend."
  value       = azurerm_storage_account.state.name
}

output "container_name" {
  description = "Private container used by the AzureRM backend."
  value       = azurerm_storage_container.state.name
}
