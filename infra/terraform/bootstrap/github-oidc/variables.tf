variable "subscription_id" {
  description = "Azure for Students subscription ID (selected by the operator's Azure CLI)."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F-]{36}$", var.subscription_id))
    error_message = "subscription_id must be an Azure subscription UUID."
  }
}

variable "location" {
  description = "Azure region for the OIDC identity resource group."
  type        = string
  default     = "France Central"
}

variable "oidc_resource_group" {
  description = "Dedicated resource group holding the three GitHub Actions OIDC identities."
  type        = string
  default     = "rg-footballai-gha-oidc"
}

variable "github_owner" {
  description = "GitHub repository owner login (used to build the OIDC subject)."
  type        = string
  default     = "azizbenammar7"
}

variable "github_repo" {
  description = "GitHub repository name (used to build the OIDC subject)."
  type        = string
  default     = "FootballAi"
}

# GitHub Actions environments whose OIDC tokens each identity is allowed to mint.
variable "build_environment" {
  description = "Environment name for the image-build identity."
  type        = string
  default     = "staging-build"
}

variable "plan_environment" {
  description = "Environment name for the Terraform-plan identity."
  type        = string
  default     = "staging-plan"
}

variable "deploy_environment" {
  description = "Environment name for the Terraform-apply identity (human-approval gated)."
  type        = string
  default     = "staging"
}

# Existing FootballAI staging resources these identities are scoped to (read-only
# data-source lookups; nothing here is created by this bootstrap).
variable "staging_resource_group" {
  description = "Existing FootballAI staging resource group."
  type        = string
  default     = "rg-footballai-stg"
}

variable "acr_name" {
  description = "Existing FootballAI staging Azure Container Registry name."
  type        = string
  default     = "footballaistg1a06f8"
}

variable "tfstate_resource_group" {
  description = "Existing Terraform-state resource group."
  type        = string
  default     = "rg-footballai-tfstate-stg"
}

variable "tfstate_storage_account" {
  description = "Existing Terraform-state Storage Account."
  type        = string
  default     = "footballaitfstg1a06f8"
}

variable "tfstate_container" {
  description = "Existing Terraform-state blob container (scope for state RBAC)."
  type        = string
  default     = "tfstate"
}

variable "tags" {
  description = "Non-sensitive tags applied to created resources."
  type        = map(string)
  default = {
    project     = "FootballAI"
    environment = "staging"
    managed_by  = "terraform"
    purpose     = "github-oidc-bootstrap"
  }
}
