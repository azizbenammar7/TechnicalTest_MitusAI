# =============================================================================
# GitHub Actions -> Azure OIDC bootstrap (secretless).
#
# Creates three least-privilege User-Assigned Managed Identities, each trusted
# by exactly one GitHub Actions environment via a federated credential. No app
# registration, no client secret, no AZURE_CREDENTIALS is ever produced.
#
# GitHub OIDC subject format: CLASSIC. Verified against this repository on
# 2026-08-24 via `gh api repos/OWNER/REPO/actions/oidc/customization/sub`:
#   use_immutable_subject = false, use_default = true
# => emitted sub = "repo:OWNER/REPO:environment:<name>". If the repository is
# ever switched to immutable subjects, these `subject` values must be updated to
# the "repo:OWNER@<owner_id>/REPO@<repo_id>:environment:<name>" form.
# =============================================================================

# --- Existing resources these identities are scoped to (read-only) -----------
data "azurerm_resource_group" "staging" {
  name = var.staging_resource_group
}

data "azurerm_container_registry" "staging" {
  name                = var.acr_name
  resource_group_name = var.staging_resource_group
}

data "azurerm_storage_account" "tfstate" {
  name                = var.tfstate_storage_account
  resource_group_name = var.tfstate_resource_group
}

locals {
  # Container-scoped RBAC target for Terraform remote state (read/write/lease
  # lock). Built as a string from the account ARM ID so no blob data-plane
  # access is needed to plan/apply this bootstrap.
  tfstate_container_scope = "${data.azurerm_storage_account.tfstate.id}/blobServices/default/containers/${var.tfstate_container}"

  # Classic GitHub OIDC subject prefix.
  subject_prefix = "repo:${var.github_owner}/${var.github_repo}:environment:"
}

# --- Dedicated resource group for the OIDC identities ------------------------
resource "azurerm_resource_group" "oidc" {
  name     = var.oidc_resource_group
  location = var.location
  tags     = var.tags
}

# =============================================================================
# BUILD identity — pushes immutable SHA-tagged images to ACR. Nothing else.
# =============================================================================
resource "azurerm_user_assigned_identity" "build" {
  name                = "id-footballai-gha-build"
  resource_group_name = azurerm_resource_group.oidc.name
  location            = azurerm_resource_group.oidc.location
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "build" {
  name                = "github-env-${var.build_environment}"
  resource_group_name = azurerm_resource_group.oidc.name
  parent_id           = azurerm_user_assigned_identity.build.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = "https://token.actions.githubusercontent.com"
  subject             = "${local.subject_prefix}${var.build_environment}"
}

resource "azurerm_role_assignment" "build_acr_push" {
  scope                = data.azurerm_container_registry.staging.id
  role_definition_name = "AcrPush"
  principal_id         = azurerm_user_assigned_identity.build.principal_id
  principal_type       = "ServicePrincipal"
}

# =============================================================================
# PLAN identity — read-only Terraform refresh/plan + remote state access.
# =============================================================================
resource "azurerm_user_assigned_identity" "plan" {
  name                = "id-footballai-gha-plan"
  resource_group_name = azurerm_resource_group.oidc.name
  location            = azurerm_resource_group.oidc.location
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "plan" {
  name                = "github-env-${var.plan_environment}"
  resource_group_name = azurerm_resource_group.oidc.name
  parent_id           = azurerm_user_assigned_identity.plan.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = "https://token.actions.githubusercontent.com"
  subject             = "${local.subject_prefix}${var.plan_environment}"
}

resource "azurerm_role_assignment" "plan_rg_reader" {
  scope                = data.azurerm_resource_group.staging.id
  role_definition_name = "Reader"
  principal_id         = azurerm_user_assigned_identity.plan.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "plan_state_blob" {
  scope                = local.tfstate_container_scope
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.plan.principal_id
  principal_type       = "ServicePrincipal"
}

# AcrPull lets the plan job verify the three SHA-tagged images exist and are
# linux/amd64 before planning. AcrPull is the narrowest role that grants the
# ACR data-plane read `az acr manifest`/`show-tags` need; registry `Reader`
# does not.
resource "azurerm_role_assignment" "plan_acr_pull" {
  scope                = data.azurerm_container_registry.staging.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.plan.principal_id
  principal_type       = "ServicePrincipal"
}

# =============================================================================
# DEPLOY identity — Terraform apply + migration job, scoped to the app RG only.
# Contributor deliberately EXCLUDES Microsoft.Authorization/roleAssignments/write.
# A normal image-tag deploy leaves rbac.tf role assignments unchanged (no-op),
# so this is sufficient. If a plan ever shows an RBAC change, apply will fail on
# that write — the intended STOP signal, NOT a reason to grant Owner/UAA.
# =============================================================================
resource "azurerm_user_assigned_identity" "deploy" {
  name                = "id-footballai-gha-deploy"
  resource_group_name = azurerm_resource_group.oidc.name
  location            = azurerm_resource_group.oidc.location
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "deploy" {
  name                = "github-env-${var.deploy_environment}"
  resource_group_name = azurerm_resource_group.oidc.name
  parent_id           = azurerm_user_assigned_identity.deploy.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = "https://token.actions.githubusercontent.com"
  subject             = "${local.subject_prefix}${var.deploy_environment}"
}

resource "azurerm_role_assignment" "deploy_rg_contributor" {
  scope                = data.azurerm_resource_group.staging.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.deploy.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "deploy_state_blob" {
  scope                = local.tfstate_container_scope
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.deploy.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "deploy_acr_pull" {
  scope                = data.azurerm_container_registry.staging.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.deploy.principal_id
  principal_type       = "ServicePrincipal"
}
