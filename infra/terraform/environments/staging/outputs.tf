output "resource_group_name" {
  value = azurerm_resource_group.staging.name
}

output "location" {
  value = azurerm_resource_group.staging.location
}

output "acr_login_server" {
  value = azurerm_container_registry.staging.login_server
}

output "frontend_fqdn" {
  description = "Stable frontend Container App ingress FQDN."
  value       = var.deploy_workloads ? azurerm_container_app.frontend[0].ingress[0].fqdn : null
}

output "frontend_latest_revision_fqdn" {
  description = "Revision-specific FQDN for the frontend Container App's latest revision."
  value       = var.deploy_workloads ? azurerm_container_app.frontend[0].latest_revision_fqdn : null
}

output "api_fqdn" {
  description = "Stable internal API Container App ingress FQDN."
  value       = var.deploy_workloads ? azurerm_container_app.api[0].ingress[0].fqdn : null
}

output "api_latest_revision_fqdn" {
  description = "Revision-specific FQDN for the API Container App's latest revision."
  value       = var.deploy_workloads ? azurerm_container_app.api[0].latest_revision_fqdn : null
}

output "blob_account_name" {
  value = azurerm_storage_account.staging.name
}

output "blob_container_name" {
  value = azurerm_storage_container.runs.name
}

output "servicebus_namespace" {
  value = azurerm_servicebus_namespace.staging.name
}

output "servicebus_queue" {
  value = azurerm_servicebus_queue.analysis.name
}

output "application_insights_name" {
  description = "Workspace-based Application Insights component used for P6 application telemetry."
  value       = azurerm_application_insights.staging.name
}

output "postgres_host" {
  value = azurerm_postgresql_flexible_server.staging.fqdn
}

output "worker_job_name" {
  value = var.deploy_workloads ? azurerm_container_app_job.worker[0].name : null
}

output "migration_job_name" {
  value = var.deploy_workloads ? azurerm_container_app_job.migration[0].name : null
}
