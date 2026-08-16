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
}
