variable "subscription_id" {
  description = "Azure subscription ID selected by the local Azure CLI identity."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F-]{36}$", var.subscription_id))
    error_message = "subscription_id must be an Azure subscription UUID."
  }
}

variable "location" {
  description = "Azure region for the Terraform state resources."
  type        = string
  default     = "France Central"
}

variable "tags" {
  description = "Non-sensitive tags applied to the Terraform state resources."
  type        = map(string)
  default = {
    project     = "FootballAI"
    environment = "staging"
    managed_by  = "terraform"
    purpose     = "terraform-state"
  }
}
