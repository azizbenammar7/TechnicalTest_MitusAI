# Non-secret identifiers to load into GitHub as environment/repo VARIABLES.
# Client IDs are public identity identifiers, never credentials.

output "build_client_id" {
  description = "AZURE_CLIENT_ID for the staging-build environment."
  value       = azurerm_user_assigned_identity.build.client_id
}

output "plan_client_id" {
  description = "AZURE_CLIENT_ID for the staging-plan environment."
  value       = azurerm_user_assigned_identity.plan.client_id
}

output "deploy_client_id" {
  description = "AZURE_CLIENT_ID for the staging environment."
  value       = azurerm_user_assigned_identity.deploy.client_id
}

data "azurerm_client_config" "current" {}

output "tenant_id" {
  description = "AZURE_TENANT_ID (repo variable)."
  value       = data.azurerm_client_config.current.tenant_id
}

output "subscription_id" {
  description = "AZURE_SUBSCRIPTION_ID (repo variable)."
  value       = var.subscription_id
}
