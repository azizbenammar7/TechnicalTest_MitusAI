locals {
  # Global Azure names need uniqueness. This stable suffix contains no personal
  # data and changes only when the target subscription changes.
  unique_suffix = substr(md5(var.subscription_id), 0, 6)
  name_prefix   = "${var.project_name}-${var.environment}"

  resource_group_name = "rg-${local.name_prefix}"
  storage_name        = "fa${var.environment}${local.unique_suffix}"
  acr_name            = "${var.project_name}${var.environment}${local.unique_suffix}"
  servicebus_name     = "sb-${local.name_prefix}-${local.unique_suffix}"
  postgres_name       = "psql-${local.name_prefix}-${local.unique_suffix}"
  database_name       = "footballai"
  queue_name          = "analysis-jobs"
  blob_container_name = "footballai-runs"
  frontend_app_name   = "ca-${local.name_prefix}-frontend"
  frontend_fqdn       = "${local.frontend_app_name}.${azurerm_container_app_environment.staging.default_domain}"
  frontend_origin     = "https://${local.frontend_fqdn}"
  api_app_name        = "ca-${local.name_prefix}-api"
  # The API uses internal-only ingress, whose FQDN carries the `.internal.`
  # segment. The frontend proxies /api to this origin from inside the
  # environment, so it must target the internal name, not the external form.
  api_fqdn    = "${local.api_app_name}.internal.${azurerm_container_app_environment.staging.default_domain}"
  api_origin  = "https://${local.api_fqdn}"
  blob_origin = trimsuffix(azurerm_storage_account.staging.primary_blob_endpoint, "/")
}
