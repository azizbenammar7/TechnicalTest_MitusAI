terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  # Reuses the existing secure Terraform-state Storage Account but keeps this
  # bootstrap in its OWN state key, isolated from footballai/staging.tfstate.
  # Authentication is Microsoft Entra via the operator's Azure CLI (no keys).
  backend "azurerm" {
    resource_group_name  = "rg-footballai-tfstate-stg"
    storage_account_name = "footballaitfstg1a06f8"
    container_name       = "tfstate"
    key                  = "footballai/github-oidc.tfstate"
    use_cli              = true
    use_azuread_auth     = true
  }

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}
