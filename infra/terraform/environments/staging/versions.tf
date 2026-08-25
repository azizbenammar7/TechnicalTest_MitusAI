terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  backend "azurerm" {
    resource_group_name  = "rg-footballai-tfstate-stg"
    storage_account_name = "footballaitfstg1a06f8"
    container_name       = "tfstate"
    key                  = "footballai/staging.tfstate"
    use_cli              = true
    use_azuread_auth     = true
  }

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }
}
