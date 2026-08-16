variable "subscription_id" {
  description = "Azure subscription ID selected by the local Azure CLI or CI OIDC identity."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F-]{36}$", var.subscription_id))
    error_message = "subscription_id must be an Azure subscription UUID."
  }
}

variable "location" {
  description = "Single Azure region used by the staging environment."
  type        = string
  default     = "France Central"
}

variable "project_name" {
  description = "Lowercase project identifier used in resource names."
  type        = string
  default     = "footballai"
}

variable "environment" {
  description = "Deployment environment identifier."
  type        = string
  default     = "stg"
}

variable "tags" {
  description = "Non-sensitive tags applied to resources."
  type        = map(string)
  default = {
    project     = "FootballAI"
    environment = "staging"
    managed_by  = "terraform"
    purpose     = "portfolio"
  }
}

variable "postgres_sku_name" {
  description = "Low-cost PostgreSQL Flexible Server compute SKU."
  type        = string
  default     = "B_Standard_B1ms"
}

variable "postgres_storage_mb" {
  description = "PostgreSQL storage allocation; Azure's observed regional minimum is 32 GiB."
  type        = number
  default     = 32768
}

variable "deploy_workloads" {
  description = "Create frontend/API/worker resources only after immutable images exist in ACR."
  type        = bool
  default     = false
}

variable "image_tag" {
  description = "Immutable 40-character Git SHA used for all three workload images."
  type        = string
  default     = "0000000000000000000000000000000000000000"

  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.image_tag))
    error_message = "image_tag must be a lowercase 40-character Git SHA, never latest."
  }
}
